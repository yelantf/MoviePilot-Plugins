"""
新增剧集订阅的选版确认插件。

同步拦截订阅下载，定期收集 E01 候选；用户确认后回填规则并恢复追更。
"""

import copy
import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.application.torrent.download import TorrentHelper
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaSource, MediaType, MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .preview import normalize_draft_rules, parse_episode_selection, preview_resource
from .rules import is_complete_season_download, merge_candidates, resource_candidate


def _utc_now() -> str:
    """
    返回适合排序和展示的 UTC 时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity(subscribe: Any) -> str:
    """
    生成订阅身份快照，防止 ID 复用沿用旧确认。
    """
    return json.dumps(
        [
            subscribe.type, subscribe.media_source, subscribe.media_id,
            subscribe.season, subscribe.episode_group, getattr(subscribe, "activity_key", None),
        ],
        ensure_ascii=False, separators=(",", ":"),
    )


def _subscription_id(origin: Any) -> Optional[int]:
    """
    从宿主的订阅下载来源中读取订阅 ID。
    """
    if not isinstance(origin, str) or not origin.startswith("Subscribe|"):
        return None
    try:
        sid = json.loads(origin.split("|", 1)[1]).get("id")
    except (ValueError, AttributeError, TypeError):
        return None
    return sid if isinstance(sid, int) and not isinstance(sid, bool) and sid > 0 else None


def _candidate_label(candidate: dict[str, Any]) -> str:
    """
    为不同类型的首集与全集候选生成简短标签。
    """
    kind = candidate.get("kind")
    if kind == "full_season":
        return "集数覆盖全集"
    if kind == "suspected_full":
        return "疑似全集"
    if kind == "episode":
        return "分集"
    return "单集 E01"


def _candidate_coverage(candidate: dict[str, Any]) -> str:
    """
    显示候选已识别的集数范围，未知时明确标记。
    """
    episodes = candidate.get("episodes") or []
    total = candidate.get("total_episode")
    if not episodes:
        return "集数待核实"
    scope = str(episodes[0]) if len(episodes) == 1 else f"{episodes[0]}–{episodes[-1]}"
    return f"E{scope}/{total or '?'}"


def _rule_scope(subscribe: Any) -> str:
    """
    冻结预览所依赖的订阅身份、筛选规则和目标集数。
    """
    return json.dumps(
        {
            "identity": _identity(subscribe),
            "total_episode": subscribe.total_episode,
            "rules": {name: getattr(subscribe, name, None) for name in (
                "quality", "resolution", "effect", "include", "exclude", "sites", "filter_groups",
                "best_version", "best_version_full", "current_priority", "episode_priority",
            )},
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )


def _preview_table(preview: dict[str, Any]) -> dict[str, Any]:
    """
    将规则预览的通过与拒绝资源展示为安全字段表格。

    :param preview: 不含下载链接的搜索快照
    :return: 插件页面表格组件
    """
    rows = [
        {"component": "tr", "content": [
            {"component": "td", "text": "通过" if accepted else "排除"},
            {"component": "td", "text": _candidate_label(item)},
            {"component": "td", "text": _candidate_coverage(item)},
            {"component": "td", "text": item.get("site_name") or str(item.get("site_id") or "?")},
            {"component": "td", "text": item.get("title") or ""},
            {"component": "td", "text": item.get("reason") or "符合当前规则"},
        ]}
        for accepted, items in ((True, preview.get("matched") or []), (False, preview.get("rejected") or []))
        for item in items
    ]
    return {"component": "VTable", "props": {"hover": True}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "text": title}
            for title in ("结果", "类型", "覆盖", "站点", "资源", "原因")
        ]}]},
        {"component": "tbody", "content": rows},
    ]}


class SubscribeOverwatcher(_PluginBase):
    """
    管理新剧集订阅的下载确认和首集版本候选。

    下载闸门读取持久确认记录；即使新增订阅广播晚到，也按创建前的 ID 水位拦截。
    允许全集直下时保留待确认订阅的搜索资格，只放行明确覆盖整季的下载事件。
    """

    plugin_name = "剧集守门员"
    plugin_desc = "剧集订阅先预览规则再确认，可手动选择集数重新下载，并每日汇总待确认订阅。"
    plugin_icon = "icon.svg"
    plugin_version = "1.3.0"
    plugin_author = "yelantf"
    plugin_label = "订阅管理"
    plugin_config_prefix = "subscribeoverwatcher_"
    plugin_order = 30
    auth_level = 2

    def __init__(self) -> None:
        """
        创建状态锁和订阅数据访问句柄。
        """
        super().__init__()
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._enabled = False
        self._confirm_full_pack = True
        self._daily_notice = True
        self._notice_hour = 9
        self._notice_minute = 0
        self._state: dict[str, Any] = {"baseline": None, "entries": {}}
        self._subscribes = SubscribeOper()

    def init_plugin(self, config: Optional[dict[str, Any]] = None) -> None:
        """
        加载确认记录，并处理配置页提交的一次性管理操作。

        :param config: 用户保存的插件配置
        """
        config = config or {}
        with self._lock:
            saved = self.get_data("gate_state")
            if isinstance(saved, dict):
                self._state = saved
            else:
                self._state = {"baseline": None, "entries": {}}
            if not isinstance(self._state.get("entries"), dict):
                self._state["entries"] = {}
            previously_disabled = self._state.get("was_enabled") is False
            self._enabled = bool(config.get("enabled"))
            self._confirm_full_pack = bool(config.get("confirm_full_pack", True))
            self._daily_notice = bool(config.get("daily_notice", True))
            self._notice_hour = self._time_part(config.get("notice_hour", 9), 9, 23)
            self._notice_minute = self._time_part(config.get("notice_minute", 0), 0, 59)
            if self._enabled and (not isinstance(self._state.get("baseline"), int) or previously_disabled):
                self._state["baseline"] = max(
                    self._state.get("baseline") or 0,
                    max((item.id for item in self._subscribes.list()), default=0),
                )
                self._save_state()
            if self._state.get("was_enabled") is not self._enabled:
                self._state["was_enabled"] = self._enabled
                self._save_state()

        actions = [name for name in (
            "adopt", "preview", "confirm", "direct_approve", "revoke", "preview_replace", "submit_replace",
        ) if config.get(name)]
        if not self._enabled:
            if actions:
                self.update_config({
                    **config, "adopt": False, "preview": False, "confirm": False,
                    "direct_approve": False, "revoke": False,
                    "preview_replace": False, "submit_replace": False,
                    "historical_subscription": "", "candidate": "", "direct_subscription": "",
                    "revoke_subscription": "", "rule_json": "", "replace_subscription": "",
                    "replace_resource": "", "replace_episodes": "",
                })
            return
        normalized_rules = ""
        if actions:
            if len(actions) != 1:
                success, message = False, "一次只能提交一个接管、预览、批准、撤销或回刷操作"
            elif actions[0] == "adopt":
                success, message = self.adopt_subscription(config.get("historical_subscription"))
            elif actions[0] == "preview":
                success, message, normalized_rules = self.preview_candidate(
                    str(config.get("candidate") or ""), str(config.get("rule_json") or ""),
                )
            elif actions[0] == "confirm":
                success, message = self.confirm_candidate(
                    str(config.get("candidate") or ""), str(config.get("rule_json") or ""),
                )
            elif actions[0] == "direct_approve":
                success, message = self.approve_subscription(config.get("direct_subscription"))
            elif actions[0] == "revoke":
                success, message = self.revoke_approval(config.get("revoke_subscription"))
            elif actions[0] == "preview_replace":
                success, message = self.preview_replacements(config.get("replace_subscription"))
            else:
                success, message = self.submit_replacement(
                    str(config.get("replace_resource") or ""), str(config.get("replace_episodes") or ""),
                )
            with self._lock:
                self._state["last_confirmation"] = {
                    "success": success, "message": message, "at": _utc_now(),
                }
                self._save_state()
            if success:
                logger.info(f"剧集守门员：{message}")
            else:
                logger.warning(f"剧集守门员：{message}")
            updated_config = {
                **config, "adopt": False, "preview": False, "confirm": False,
                "direct_approve": False, "revoke": False, "preview_replace": False, "submit_replace": False,
                "historical_subscription": "", "direct_subscription": "", "revoke_subscription": "",
            }
            if actions == ["preview"] and success:
                updated_config["rule_json"] = normalized_rules
            elif actions != ["preview"]:
                updated_config["candidate"] = ""
                updated_config["rule_json"] = ""
            if actions == ["submit_replace"]:
                updated_config["replace_resource"] = ""
                updated_config["replace_episodes"] = ""
            self.update_config(updated_config)
        self._reconcile_pending_states()
        self._schedule_scan()

    def _save_state(self) -> None:
        """
        保存已由状态锁保护的确认和候选记录。
        """
        self.save_data("gate_state", self._state)

    @staticmethod
    def _time_part(value: Any, default: int, maximum: int) -> int:
        """将通知时间限制在小时或分钟的有效范围内。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if 0 <= number <= maximum else default

    def _is_approved(self, subscribe: Any) -> bool:
        """
        只认可与当前订阅身份完全一致的确认记录。
        """
        entry = self._state["entries"].get(str(subscribe.id)) or {}
        return entry.get("status") == "approved" and entry.get("identity") == _identity(subscribe)

    def _is_managed(self, subscribe: Any) -> bool:
        """
        新订阅按水位管理，已有记录在水位变化后继续按身份管理。
        """
        if subscribe.id > (self._state.get("baseline") or 0):
            return True
        entry = self._state["entries"].get(str(subscribe.id)) or {}
        return entry.get("identity") == _identity(subscribe)

    def _ensure_pending(self, subscribe: Any, *, force_historical: bool = False) -> bool:
        """
        纳入新剧集订阅，并按全集确认开关调整搜索资格。

        :param subscribe: 当前订阅记录
        :param force_historical: 由用户明确选择历史订阅时绕过新订阅水位
        :return: 本次是否新建待确认记录
        """
        if subscribe.type != MediaType.TV.value:
            return False
        with self._lock:
            if not force_historical and not self._is_managed(subscribe):
                return False
            if self._is_approved(subscribe):
                return False
            key = str(subscribe.id)
            entry = self._state["entries"].get(key)
            fresh = not isinstance(entry, dict) or entry.get("identity") != _identity(subscribe)
            if fresh:
                entry = {
                    "identity": _identity(subscribe),
                    "source": "historical" if subscribe.id <= self._state["baseline"] else "new",
                    "original_state": subscribe.state if subscribe.id <= self._state["baseline"] else None,
                    "status": "pending",
                    "name": subscribe.name,
                    "season": subscribe.season,
                    "candidates": [],
                    "created_at": _utc_now(),
                    "plugin_paused": False,
                    "needs_search": not self._confirm_full_pack and subscribe.state in {"N", "R", "P"},
                }
                self._state["entries"][key] = entry
                self._save_state()
            elif "plugin_paused" not in entry:
                # 旧版本创建的待确认订阅一律由插件暂停。
                entry["plugin_paused"] = subscribe.state == "S"
                self._save_state()
            if self._confirm_full_pack:
                if subscribe.state != "S":
                    self._subscribes.update(subscribe.id, {"state": "S"})
                    entry["plugin_paused"] = not entry.get("revoked", False)
                    entry["needs_search"] = False
                    self._save_state()
            elif entry.get("plugin_paused") and not entry.get("revoked"):
                if subscribe.state == "S":
                    self._subscribes.update(subscribe.id, {"state": "R"})
                    entry["needs_search"] = True
                entry["plugin_paused"] = False
                self._save_state()
            return fresh

    def adopt_subscription(self, sid_value: Any) -> tuple[bool, str]:
        """
        用户明确选择后才接管一条已有的剧集订阅。

        :param sid_value: 订阅 ID
        :return: 是否纳入管理及结果说明
        """
        try:
            sid = int(sid_value)
        except (ValueError, TypeError):
            return False, "请先选择一条历史剧集订阅"
        with self._lock:
            subscribe = self._subscribes.get(sid)
            if not subscribe or subscribe.type != MediaType.TV.value or sid > self._state["baseline"]:
                return False, "这条订阅不是可纳入的历史剧集"
            if subscribe.state not in {"N", "R", "P", "S"}:
                return False, "只支持纳入仍在订阅列表中的剧集"
            if str(sid) in self._state["entries"]:
                return False, "这条订阅已由插件管理"
            if not self._ensure_pending(subscribe, force_historical=True):
                return False, "纳入历史订阅失败"
        self._schedule_scan()
        return True, f"订阅 {sid} 已纳入管理，原有筛选规则暂未修改"

    def _reconcile_pending_states(self) -> None:
        """
        配置变化后立即调整已有待确认订阅的暂停状态。
        """
        with self._lock:
            ids = [int(key) for key, entry in self._state["entries"].items()
                   if entry.get("status") == "pending"]
        for sid in ids:
            try:
                subscribe = self._subscribes.get(sid)
                if subscribe:
                    self._ensure_pending(subscribe)
            except Exception:
                logger.exception(f"调整订阅 {sid} 的待确认状态失败，下载闸门仍按记录生效")

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Optional[Event] = None) -> None:
        """
        新订阅提交后尽快暂停，并安排首次候选扫描。

        :param event: 宿主的新增订阅广播
        """
        if not self._enabled or event is None or not isinstance(event.event_data, dict):
            return
        sid = event.event_data.get("subscribe_id")
        if not isinstance(sid, int) or isinstance(sid, bool) or sid <= self._state["baseline"]:
            return
        subscribe = self._subscribes.get(sid)
        if subscribe and self._ensure_pending(subscribe):
            self._schedule_scan()

    @eventmanager.register(ChainEventType.ResourceDownload, priority=1000)
    def intercept_download(self, event: Optional[Event] = None) -> None:
        """
        在宿主提交下载器之前拦截未批准的分集或全集资源。

        :param event: 带有可写取消标记的资源下载事件
        """
        if not self._enabled or event is None or event.event_data is None:
            return
        data = event.event_data
        sid = _subscription_id(getattr(data, "origin", None))
        if sid is None or (sid <= self._state["baseline"] and str(sid) not in self._state["entries"]):
            return
        previous_cancel = bool(data.cancel)
        previous_source = data.source
        previous_reason = data.reason
        data.cancel = True
        data.source = self.__class__.__name__
        data.reason = "剧集规格待手动确认"
        try:
            subscribe = self._subscribes.get(sid)
            if subscribe:
                with self._lock:
                    entry = self._state["entries"].get(str(sid)) or {}
                    if subscribe.type != MediaType.TV.value or not self._is_managed(subscribe):
                        released = True
                    elif self._is_approved(subscribe):
                        rejection = self._rule_rejection(subscribe, getattr(data, "context", None)) \
                            if entry.get("mode") == "candidate" else None
                        released = rejection is None
                        if rejection:
                            data.reason = f"已批准订阅的当前规则未匹配：{rejection}"
                    else:
                        released = not self._confirm_full_pack and not entry.get("revoked") \
                            and is_complete_season_download(data, subscribe)
                if released:
                    data.cancel = previous_cancel
                    data.source = previous_source
                    data.reason = previous_reason
        except Exception:
            logger.exception(f"查询订阅 {sid} 的确认状态失败，保持下载拦截")

    def _schedule_scan(self) -> None:
        """
        合并短时间内的新增事件，安排一次候选扫描。
        """
        try:
            add_plugin_once_job(
                self.__class__.__name__, "collect", self.refresh_candidates,
                "扫描待确认剧集版本", delay_seconds=15,
            )
        except RuntimeError:
            logger.warning("调度器尚未就绪，版本候选将在周期任务中扫描")

    def refresh_candidates(self) -> None:
        """
        补收新增事件、更新首集和全集候选并执行待处理的补搜。
        """
        if not self._enabled or not self._scan_lock.acquire(blocking=False):
            return
        try:
            subscribes = self._subscribes.list()
            active_ids = {str(subscribe.id) for subscribe in subscribes}
            with self._lock:
                stale_ids = set(self._state["entries"]) - active_ids
                stale_ids.update(
                    str(subscribe.id) for subscribe in subscribes
                    if (entry := self._state["entries"].get(str(subscribe.id)))
                    and entry.get("source") == "historical"
                    and entry.get("identity") != _identity(subscribe)
                )
                for sid in stale_ids:
                    self._state["entries"].pop(sid, None)
                if stale_ids:
                    self._save_state()
            for subscribe in subscribes:
                if self._is_managed(subscribe) and subscribe.type == MediaType.TV.value:
                    self._ensure_pending(subscribe)
            with self._lock:
                pending_ids = [
                    int(key) for key, entry in self._state["entries"].items()
                    if entry.get("status") == "pending"
                ]
                search_ids = [
                    int(key) for key, entry in self._state["entries"].items()
                    if entry.get("status") == "approved" and entry.get("needs_search")
                ]
                pending_search_ids = [
                    int(key) for key, entry in self._state["entries"].items()
                    if entry.get("status") == "pending" and entry.get("needs_search")
                ]
            for sid in pending_ids:
                self._scan_one(sid)
            for sid in pending_search_ids:
                self._search_pending(sid)
            for sid in search_ids:
                self._search_approved(sid)
        finally:
            self._scan_lock.release()

    def _scan_one(self, sid: int) -> None:
        """
        搜索一条待确认订阅的首集和全集，并保留历史候选。
        """
        subscribe = self._subscribes.get(sid)
        if not subscribe:
            with self._lock:
                self._state["entries"].pop(str(sid), None)
                self._save_state()
            return
        if subscribe.type != MediaType.TV.value or not subscribe.media_source or not subscribe.media_id:
            return
        try:
            contexts = SearchChain().search_by_id(
                media_source=MediaSource(subscribe.media_source),
                media_id=str(subscribe.media_id),
                mtype=MediaType.TV,
                season=subscribe.season or 1,
                cache_local=False,
            )
            now = _utc_now()
            fresh = []
            for context in contexts:
                try:
                    candidate = resource_candidate(
                        context, subscribe.season or 1, now, subscribe.total_episode,
                    )
                except (TypeError, ValueError, OverflowError):
                    logger.warning(f"订阅 {sid} 的一个 E01 搜索结果格式异常，已跳过")
                    continue
                if candidate:
                    fresh.append(candidate)
            with self._lock:
                entry = self._state["entries"].get(str(sid))
                if entry and entry.get("status") == "pending" and entry.get("identity") == _identity(subscribe):
                    entry["candidates"] = merge_candidates(entry.get("candidates") or [], fresh)
                    entry["last_scan"] = now
                    entry.pop("last_error", None)
                    self._save_state()
        except Exception:
            logger.exception(f"扫描订阅 {sid} 的 E01 候选失败")
            with self._lock:
                entry = self._state["entries"].get(str(sid))
                if entry and entry.get("status") == "pending":
                    entry["last_error"] = "搜索失败，详情见插件日志"
                    self._save_state()

    @staticmethod
    def _rule_rejection(subscribe: Any, context: Any) -> Optional[str]:
        """
        用宿主的同一基础筛选器核对站点与订阅文本规则。
        """
        torrent = getattr(context, "torrent_info", None)
        if torrent is None:
            return "资源缺少种子信息"
        sites = getattr(subscribe, "sites", None) or []
        if sites and getattr(torrent, "site", None) not in sites:
            return "不在订阅站点范围内"
        return TorrentHelper.filter_torrent_reason(torrent, SubscribeChain.get_params(subscribe))

    def _search_rule_preview(
        self, subscribe: Any, rules: dict[str, Any], *, require_episodes: bool = False,
    ) -> dict[str, Any]:
        """
        只搜索并筛选当前资源，不更新订阅或提交下载。

        :param subscribe: 待预览的订阅
        :param rules: 候选规格生成的临时规则
        :param require_episodes: 手动选集下载时要求明确识别集数
        :return: 通过、拒绝及统计信息
        """
        working = copy.copy(subscribe)
        for field, value in rules.items():
            if value is not None:
                setattr(working, field, value)
            elif field != "sites":
                setattr(working, field, None)
        contexts = SearchChain().search_by_id(
            media_source=MediaSource(subscribe.media_source),
            media_id=str(subscribe.media_id),
            mtype=MediaType.TV,
            season=subscribe.season or 1,
            cache_local=False,
        )
        now = _utc_now()
        matched: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for context in contexts:
            try:
                item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, now)
            except (TypeError, ValueError, OverflowError):
                logger.warning(f"订阅 {subscribe.id} 的一个规则预览资源格式异常，已跳过")
                continue
            if not item:
                continue
            if require_episodes and not item["episodes"]:
                reason = "未明确识别集数，不能手动选集"
            else:
                try:
                    reason = self._rule_rejection(working, context)
                except (TypeError, ValueError, AttributeError):
                    reason = "资源字段不完整，无法核对规则"
            if reason:
                item["reason"] = reason
                rejected.append(item)
            else:
                matched.append(item)
        priority = {"full_season": 3, "suspected_full": 2, "first_episode": 1, "episode": 0}
        matched.sort(key=lambda item: priority.get(item["kind"], 0), reverse=True)
        rejected.sort(key=lambda item: priority.get(item["kind"], 0), reverse=True)
        return {
            "matched": matched[:80], "rejected": rejected[:40],
            "matched_count": len(matched), "rejected_count": len(rejected),
            "searched_at": now, "searched_ts": datetime.now(timezone.utc).timestamp(),
        }

    def preview_candidate(self, choice: str, rule_json: str = "") -> tuple[bool, str, str]:
        """
        生成候选规格的只读匹配预览，保持订阅暂停和原规则不变。

        :param choice: 订阅 ID 与候选 ID
        :param rule_json: 用户调整的草稿规则 JSON
        :return: 是否成功、结果消息和规范化草稿 JSON
        """
        try:
            sid_text, candidate_id = choice.split(":", 1)
            sid = int(sid_text)
        except (ValueError, TypeError):
            return False, "请先选择一个版本候选", rule_json
        with self._lock:
            entry = self._state["entries"].get(str(sid)) or {}
            candidate = next(
                (item for item in entry.get("candidates") or [] if item.get("id") == candidate_id), None,
            )
            subscribe = self._subscribes.get(sid)
            if entry.get("status") != "pending" or not candidate or not subscribe \
                    or entry.get("identity") != _identity(subscribe):
                return False, "候选或订阅已变化，请重新扫描", rule_json
            entry.pop("preview", None)
            self._save_state()
            try:
                rules = normalize_draft_rules(candidate.get("rules") or {}, rule_json)
            except ValueError as error:
                return False, str(error), rule_json
            scope = _rule_scope(subscribe)
        try:
            preview = self._search_rule_preview(subscribe, rules)
        except Exception:
            logger.exception(f"预览订阅 {sid} 的规则匹配失败")
            return False, "搜索失败，请查看插件日志后重试", rule_json
        with self._lock:
            current = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            if not current or entry.get("status") != "pending" or _rule_scope(current) != scope:
                return False, "预览期间订阅发生变化，请重新预览", rule_json
            preview.update({"choice": choice, "rules": rules, "scope": scope})
            entry["preview"] = preview
            self._save_state()
        normalized_json = json.dumps(rules, ensure_ascii=False, indent=2)
        return True, f"当前匹配 {preview['matched_count']} 条，排除 {preview['rejected_count']} 条；订阅仍保持待确认", normalized_json

    def confirm_candidate(self, choice: str, rule_json: Optional[str] = None) -> tuple[bool, str]:
        """
        回填所选版本的规则，再恢复订阅并安排补搜。

        :param choice: 配置页传入的“订阅 ID:候选 ID”
        :param rule_json: 用户当前提交的草稿规则；变更后必须重新预览
        :return: 是否确认成功及可展示的结果
        """
        try:
            sid_text, candidate_id = choice.split(":", 1)
            sid = int(sid_text)
        except (ValueError, TypeError):
            return False, "请先选择一个版本候选"
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            if not entry or entry.get("status") != "pending":
                return False, "订阅已确认或不在待确认列表中"
            candidate = next(
                (item for item in entry.get("candidates") or [] if item.get("id") == candidate_id), None,
            )
            if not candidate:
                return False, "候选已失效，请刷新后重新选择"
            subscribe = self._subscribes.get(sid)
            if not subscribe or subscribe.type != MediaType.TV.value or entry.get("identity") != _identity(subscribe):
                return False, "订阅身份已变化，请重新扫描"
            preview = entry.get("preview") or {}
            if preview.get("choice") != choice or preview.get("scope") != _rule_scope(subscribe) \
                    or not isinstance(preview.get("searched_ts"), (int, float)) \
                    or datetime.now(timezone.utc).timestamp() - preview["searched_ts"] > 1800:
                return False, "请先为当前候选生成不超过 30 分钟的匹配预览"
            rules = preview.get("rules") or {}
            if rule_json is not None:
                try:
                    if normalize_draft_rules(candidate.get("rules") or {}, rule_json) != rules:
                        return False, "规则已修改，请重新预览后确认"
                except ValueError as error:
                    return False, str(error)
            if not preview.get("matched_count") or candidate_id not in {
                item.get("id") for item in preview.get("matched") or []
            }:
                return False, "所选版本未通过当前规则，请修改规则并重新预览"
            if candidate.get("kind") == "full_season" and sum(
                item.get("kind") == "full_season" for item in preview.get("matched") or []
            ) > 1:
                return False, "规则仍匹配多个全集版本，请缩小规则后重新预览"
            payload = {field: value for field, value in rules.items() if field != "sites" or value is not None}
            return self._approve(sid, entry, payload, candidate_id)

    def approve_subscription(self, sid_value: Any) -> tuple[bool, str]:
        """
        直接批准待确认订阅，保留现有筛选规则并补齐缺集。

        :param sid_value: 配置页选中的订阅 ID
        :return: 是否批准成功及可展示的结果
        """
        try:
            sid = int(sid_value)
        except (ValueError, TypeError):
            return False, "请先选择一条待确认订阅"
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            subscribe = self._subscribes.get(sid)
            if not entry or entry.get("status") != "pending":
                return False, "订阅已批准或不在待确认列表中"
            if not subscribe or subscribe.type != MediaType.TV.value or entry.get("identity") != _identity(subscribe):
                return False, "订阅身份已变化，请重新扫描"
            return self._approve(sid, entry, None, None)

    def _approve(
        self, sid: int, entry: dict[str, Any], payload: Optional[dict[str, Any]],
        candidate_id: Optional[str],
    ) -> tuple[bool, str]:
        """
        写入批准记录、恢复订阅并安排一次缺集补搜。

        调用方须持有状态锁，并已核实订阅身份和待确认状态。
        """
        previous = copy.deepcopy(entry)
        resume_state = "S" if entry.get("source") == "historical" and entry.get("original_state") == "S" else "R"
        try:
            if payload is not None:
                self._subscribes.update(sid, payload)
            entry.update({
                "status": "approved", "mode": "candidate" if candidate_id else "direct",
                "selected": candidate_id, "approved_at": _utc_now(),
                "needs_search": resume_state != "S", "plugin_paused": False, "revoked": False,
            })
            self._save_state()
            self._subscribes.update(sid, {"state": resume_state})
            current = self._subscribes.get(sid)
            if current and entry.get("preview"):
                entry["preview"]["applied_scope"] = _rule_scope(current)
                self._save_state()
        except Exception:
            self._state["entries"][str(sid)] = previous
            self._save_state()
            logger.exception(f"批准订阅 {sid} 时写入规则或状态失败")
            return False, "规则或状态保存失败，订阅仍保持拦截"
        if resume_state != "S":
            self._schedule_approved_search(sid)
        else:
            return True, f"订阅 {sid} 已批准；原本由用户暂停，仍保持暂停"
        if candidate_id:
            return True, f"订阅 {sid} 已回填规则并恢复，将补齐缺集后继续追更"
        return True, f"订阅 {sid} 已直接批准，原有规则保持不变，将补齐缺集后继续追更"

    def _schedule_approved_search(self, sid: int) -> None:
        """
        为批准的订阅安排一次补搜，调度器未就绪时等待周期任务。
        """
        try:
            add_plugin_once_job(
                self.__class__.__name__, f"search-{sid}", self._search_approved,
                "确认后补齐订阅缺集", delay_seconds=2, func_kwargs={"sid": sid},
            )
        except RuntimeError:
            logger.warning(f"调度器尚未就绪，订阅 {sid} 将在周期任务中补搜")

    def revoke_approval(self, sid_value: Any) -> tuple[bool, str]:
        """
        撤销仍存在的订阅批准，重新启用下载闸门。

        已回填或手动编辑的筛选规则保留；订阅暂停，未启动的插件补搜任务会移除。
        显式撤销优先于全集免确认设置，重新批准前所有订阅下载都被拦截。

        :param sid_value: 配置页选中的已批准订阅 ID
        :return: 是否撤销成功及可展示的结果
        """
        try:
            sid = int(sid_value)
        except (ValueError, TypeError):
            return False, "请先选择一条已批准订阅"
        pause_failed = False
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            subscribe = self._subscribes.get(sid)
            if not entry or entry.get("status") != "approved":
                return False, "订阅不在已批准列表中"
            if not subscribe or subscribe.type != MediaType.TV.value or entry.get("identity") != _identity(subscribe):
                return False, "订阅已结束或身份已变化，无法撤销"
            entry["status"] = "pending"
            entry.pop("selected", None)
            entry.pop("mode", None)
            entry.pop("approved_at", None)
            entry["needs_search"] = False
            entry["revoked"] = True
            entry["plugin_paused"] = False
            entry.pop("preview", None)
            entry.pop("replacement_preview", None)
            self._save_state()
            if subscribe.state != "S":
                try:
                    self._subscribes.update(sid, {"state": "S"})
                except Exception:
                    logger.exception(f"撤销订阅 {sid} 批准后暂停订阅失败，下载闸门已恢复")
                    pause_failed = True
        try:
            remove_plugin_once_job(self.__class__.__name__, f"search-{sid}")
        except RuntimeError:
            logger.warning(f"调度器尚未就绪，订阅 {sid} 的待执行补搜由下载闸门拦截")
        if pause_failed:
            return False, "批准已撤销并恢复下载拦截，但暂停订阅失败，请检查插件日志"
        return True, f"订阅 {sid} 已撤销批准，原有筛选规则保留"

    def preview_replacements(self, sid_value: Any) -> tuple[bool, str]:
        """
        按已批准订阅的当前规则搜索可供手动回刷的资源。

        :param sid_value: 已批准订阅 ID
        :return: 预览是否生成及结果说明
        """
        try:
            sid = int(sid_value)
        except (ValueError, TypeError):
            return False, "请先选择一条已批准订阅"
        with self._lock:
            subscribe = self._subscribes.get(sid)
            if not subscribe or not self._is_approved(subscribe):
                return False, "订阅未批准或身份已变化"
            entry = self._state["entries"][str(sid)]
            entry.pop("replacement_preview", None)
            self._save_state()
            scope = _rule_scope(subscribe)
        try:
            preview = self._search_rule_preview(subscribe, {}, require_episodes=True)
        except Exception:
            logger.exception(f"预览订阅 {sid} 的回刷资源失败")
            return False, "搜索失败，请查看插件日志后重试"
        with self._lock:
            current = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            if not current or not self._is_approved(current) or _rule_scope(current) != scope:
                return False, "预览期间订阅发生变化，请重新预览"
            preview["scope"] = scope
            entry["replacement_preview"] = preview
            self._save_state()
        return True, f"当前规则匹配 {preview['matched_count']} 条资源；可手动选择资源和集数重新下载"

    def submit_replacement(self, choice: str, episode_text: str) -> tuple[bool, str]:
        """
        重新搜索预览资源，核对规则与集数后提交一次显式下载。

        :param choice: 订阅 ID 与预览资源 ID
        :param episode_text: 用户明确选择的目标集数
        :return: 下载器是否接受任务及结果说明
        """
        try:
            sid_text, resource_id = choice.split(":", 1)
            sid = int(sid_text)
        except (ValueError, TypeError):
            return False, "请先选择一条预览中匹配的资源"
        with self._lock:
            subscribe = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            preview = entry.get("replacement_preview") or {}
            if not subscribe or not self._is_approved(subscribe):
                return False, "订阅未批准或身份已变化"
            try:
                episodes = parse_episode_selection(episode_text, subscribe.total_episode)
            except ValueError as error:
                return False, str(error)
            scope = _rule_scope(subscribe)
            if preview.get("scope") != scope or not isinstance(preview.get("searched_ts"), (int, float)) \
                    or datetime.now(timezone.utc).timestamp() - preview["searched_ts"] > 1800:
                return False, "当前规则或订阅已变化，请在 30 分钟内重新预览"
            if resource_id not in {item.get("id") for item in preview.get("matched") or []}:
                return False, "资源不在当前规则的预览匹配列表中"
        try:
            contexts = SearchChain().search_by_id(
                media_source=MediaSource(subscribe.media_source), media_id=str(subscribe.media_id),
                mtype=MediaType.TV, season=subscribe.season or 1, cache_local=False,
            )
        except Exception:
            logger.exception(f"重新搜索订阅 {sid} 的回刷资源失败")
            return False, "提交前重新搜索失败，未开始下载"
        candidates = []
        for context in contexts:
            try:
                item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
            except (TypeError, ValueError, OverflowError):
                logger.warning(f"订阅 {sid} 的一个回刷搜索结果格式异常，已跳过")
                continue
            if item and item["id"] == resource_id:
                candidates.append(context)
        if len(candidates) != 1:
            return False, "资源已消失或同名资源不唯一，请重新预览，未开始下载"
        context = candidates[0]
        recognized = {
            item for item in (getattr(context.meta_info, "episode_list", None) or [])
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        }
        if not episodes.issubset(recognized):
            return False, "资源未明确覆盖所选集数，未开始下载"
        with self._lock:
            current = self._subscribes.get(sid)
            if not current or not self._is_approved(current) or _rule_scope(current) != scope:
                return False, "提交前订阅或规则已变化，未开始下载"
            reason = self._rule_rejection(current, context)
            if reason:
                return False, f"资源已不匹配当前规则：{reason}；未开始下载"
            try:
                result = DownloadChain().download_single(
                    context, episodes=episodes,
                    source=SubscribeChain.get_subscribe_source_keyword(current), return_detail=True,
                )
            except Exception:
                logger.exception(f"订阅 {sid} 的手动回刷下载提交失败")
                return False, "下载提交失败，请查看插件日志"
            download_hash = result[0] if isinstance(result, tuple) else result
            if not download_hash:
                return False, "下载器未接受资源，请查看插件日志；旧文件未由插件处理"
            history = entry.setdefault("replacement_history", [])
            history.insert(0, {
                "resource_id": resource_id, "title": str(context.torrent_info.title)[:240],
                "episodes": sorted(episodes), "submitted_at": _utc_now(),
            })
            del history[20:]
            self._save_state()
        return True, f"订阅 {sid} 的 {len(episodes)} 集已提交下载；旧文件替换由 MoviePilot 整理流程决定"

    def _search_pending(self, sid: int) -> None:
        """
        允许全集直下时，为刚纳入管理的订阅补做一次搜索。
        """
        if not self._enabled or self._confirm_full_pack:
            return
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            subscribe = self._subscribes.get(sid)
            if (not entry or entry.get("status") != "pending" or not entry.get("needs_search")
                    or entry.get("revoked") or not subscribe or entry.get("identity") != _identity(subscribe)
                    or subscribe.state == "S"):
                return
        try:
            SubscribeChain().search(sid=sid, state=None, manual=True)
        except Exception:
            logger.exception(f"订阅 {sid} 的全集候选补搜失败，稍后重试")
            return
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            if entry and entry.get("status") == "pending":
                entry["needs_search"] = False
                self._save_state()

    def _search_approved(self, sid: int) -> None:
        """
        为刚确认的订阅发起一次指定目标补搜。
        """
        if not self._enabled:
            return
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            subscribe = self._subscribes.get(sid)
            if not entry or not entry.get("needs_search") or not subscribe or not self._is_approved(subscribe):
                return
        try:
            SubscribeChain().search(sid=sid, state=None, manual=True)
        except Exception:
            logger.exception(f"订阅 {sid} 的确认后补搜失败，稍后重试")
            return
        with self._lock:
            entry = self._state["entries"].get(str(sid))
            if entry and entry.get("status") == "approved":
                entry["needs_search"] = False
                self._save_state()

    def get_state(self) -> bool:
        """
        返回插件是否正在接管新剧集订阅。
        """
        return self._enabled

    def get_api(self) -> list[dict[str, Any]]:
        """
        本插件通过宿主配置页完成确认，无需额外接口。
        """
        return []

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        返回全集确认策略与互斥的历史接管、批准和撤销操作。
        """
        with self._lock:
            # 首次启用前还没有水位；先展示现有订阅，保存启用配置时再冻结水位。
            historical_choices = [
                {
                    "title": f"#{item.id} {item.name} S{int(item.season or 1):02d} · "
                             f"{'已暂停' if item.state == 'S' else '订阅中'}",
                    "value": item.id,
                }
                for item in self._subscribes.list()
                if (not self._enabled or item.id <= (self._state.get("baseline") or 0))
                and item.type == MediaType.TV.value
                and item.state in {"N", "R", "P", "S"}
                and str(item.id) not in self._state["entries"]
            ]
            choices = [
                {
                    "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                             f"{_candidate_label(item)} {_candidate_coverage(item)} · "
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · "
                             f"{item.get('quality') or '?'} {item.get('resolution') or '?'} "
                             f"{item.get('team') or ''} · {item.get('title')}",
                    "value": f"{sid}:{item['id']}",
                }
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "pending"
                for item in entry.get("candidates") or []
            ]
            pending_choices = [
                {"title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}", "value": sid}
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "pending"
            ]
            approved_choices = [
                {"title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}", "value": sid}
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "approved"
            ]
            replacement_choices = [
                {
                    "title": f"#{sid} · {_candidate_label(item)} {_candidate_coverage(item)} · "
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · {item.get('title')}",
                    "value": f"{sid}:{item['id']}",
                }
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "approved"
                for item in (entry.get("replacement_preview") or {}).get("matched") or []
            ]
        return [
            {"component": "VForm", "content": [
                {"component": "VSwitch", "props": {"model": "enabled", "label": "接管新增剧集订阅"}},
                {"component": "VSwitch", "props": {
                    "model": "confirm_full_pack", "label": "全集种子也需确认",
                }},
                {"component": "VSwitch", "props": {
                    "model": "daily_notice", "label": "每天推送待确认订阅汇总",
                    "class": "mt-4",
                }},
                {"component": "VSelect", "props": {
                    "model": "notice_hour", "label": "每日通知时间：小时",
                    "items": [{"title": f"{hour:02d}", "value": hour} for hour in range(24)],
                    "item-title": "title", "item-value": "value", "class": "mt-2",
                }},
                {"component": "VSelect", "props": {
                    "model": "notice_minute", "label": "每日通知时间：分钟",
                    "items": [{"title": f"{minute:02d}", "value": minute} for minute in range(60)],
                    "item-title": "title", "item-value": "value", "class": "mt-2",
                }},
                {"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "开启全集确认时新增剧集暂停；关闭后仍拦截分集下载，只有识别为覆盖整季且通过原订阅规则的种子可自动下载。显式撤销的订阅始终暂停。每 4 小时更新 E01 候选。",
                }},
                {"component": "VSelect", "props": {
                    "model": "historical_subscription", "label": "选择纳入管理的历史剧集",
                    "items": historical_choices, "item-title": "title", "item-value": "value", "clearable": True,
                    "no-data-text": "暂无可纳入管理的历史剧集订阅", "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "adopt", "label": "纳入历史订阅，原有筛选规则暂不修改",
                    "class": "mt-2",
                }},
                {"component": "VSelect", "props": {
                    "model": "candidate", "label": "选择一个版本（全集优先）", "items": choices,
                    "item-title": "title", "item-value": "value", "clearable": True, "class": "mt-4",
                }},
                {"component": "VTextarea", "props": {
                    "model": "rule_json", "label": "草稿规则 JSON；留空使用候选规格，可编辑后重新预览",
                    "rows": 5, "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "preview", "label": "只搜索并预览规则匹配，不恢复订阅",
                    "class": "mt-2",
                }},
                {"component": "VSwitch", "props": {
                    "model": "confirm", "label": "确认已预览规则并恢复订阅",
                }},
                {"component": "VSelect", "props": {
                    "model": "direct_subscription", "label": "选择直接批准的订阅", "items": pending_choices,
                    "item-title": "title", "item-value": "value", "clearable": True, "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "direct_approve", "label": "直接批准，不回填 E01 规则",
                    "class": "mt-2",
                }},
                {"component": "VSelect", "props": {
                    "model": "revoke_subscription", "label": "选择撤销批准的订阅", "items": approved_choices,
                    "item-title": "title", "item-value": "value", "clearable": True, "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "revoke", "label": "撤销批准并暂停，保留当前筛选规则",
                    "class": "mt-2",
                }},
                {"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "要回刷已入库集数，先预览已批准订阅的当前规则，再选预览通过的资源和集数提交下载。插件不删除旧文件；最终替换由 MoviePilot 整理流程决定。",
                }},
                {"component": "VSelect", "props": {
                    "model": "replace_subscription", "label": "选择要预览回刷资源的已批准订阅",
                    "items": approved_choices, "item-title": "title", "item-value": "value", "clearable": True,
                    "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "preview_replace", "label": "按当前订阅规则预览可回刷资源",
                    "class": "mt-2",
                }},
                {"component": "VSelect", "props": {
                    "model": "replace_resource", "label": "选择预览通过的回刷资源",
                    "items": replacement_choices, "item-title": "title", "item-value": "value", "clearable": True,
                    "class": "mt-4",
                }},
                {"component": "VTextField", "props": {
                    "model": "replace_episodes", "label": "明确选择重新下载的集数，例如 1,3-5",
                    "class": "mt-4",
                }},
                {"component": "VSwitch", "props": {
                    "model": "submit_replace", "label": "提交所选资源和集数重新下载",
                    "class": "mt-2",
                }},
            ]},
        ], {
            "enabled": False, "confirm_full_pack": True, "daily_notice": True,
            "notice_hour": 9, "notice_minute": 0,
            "historical_subscription": "", "adopt": False,
            "candidate": "", "rule_json": "", "preview": False, "confirm": False,
            "direct_subscription": "", "direct_approve": False,
            "revoke_subscription": "", "revoke": False,
            "replace_subscription": "", "preview_replace": False,
            "replace_resource": "", "replace_episodes": "", "submit_replace": False,
        }

    def get_page(self) -> list[dict[str, Any]]:
        """
        展示待确认和已批准订阅、候选规格及最近操作状态。
        """
        with self._lock:
            entries = copy.deepcopy(list(self._state["entries"].items()))
            last_confirmation = copy.deepcopy(self._state.get("last_confirmation"))
        pending = [(sid, entry) for sid, entry in entries if entry.get("status") == "pending"]
        approved = [(sid, entry) for sid, entry in entries if entry.get("status") == "approved"]
        page: list[dict[str, Any]] = []
        if last_confirmation:
            page.append({"component": "VAlert", "props": {
                "type": "success" if last_confirmation.get("success") else "error",
                "text": last_confirmation.get("message") or "",
            }})
        if not pending:
            page.append({"component": "VAlert", "props": {
                "type": "info", "text": "暂无待确认剧集。新增或手动纳入电视剧订阅后会收集全集和 E01 候选。",
            }})
        for sid, entry in pending:
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"候选 {len(entry.get('candidates') or [])} 个 · "
                        f"最近扫描 {entry.get('last_scan') or '等待中'}"
                        + (" · 已撤销，等待重新批准" if entry.get("revoked") else "")
                        + (f" · 扫描错误：{entry['last_error']}" if entry.get("last_error") else ""),
            }})
            rows = [
                {"component": "tr", "content": [
                    {"component": "td", "text": _candidate_label(item)},
                    {"component": "td", "text": _candidate_coverage(item)},
                    {"component": "td", "text": item.get("site_name") or str(item.get("site_id") or "?")},
                    {"component": "td", "text": item.get("title") or ""},
                    {"component": "td", "text": item.get("quality") or "?"},
                    {"component": "td", "text": item.get("resolution") or "?"},
                    {"component": "td", "text": item.get("effect") or "?"},
                    {"component": "td", "text": item.get("team") or "?"},
                    {"component": "td", "text": item.get("last_seen") or ""},
                ]}
                for item in entry.get("candidates") or []
            ]
            page.append({"component": "VTable", "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("类型", "覆盖", "站点", "资源", "质量", "分辨率", "特效", "制作组", "最后发现")
                ]}]},
                {"component": "tbody", "content": rows},
            ]})
            preview = entry.get("preview") or {}
            if preview:
                page.append({"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": f"规则预览于 {preview.get('searched_at') or '?'}：通过 "
                            f"{preview.get('matched_count', 0)} 条，排除 {preview.get('rejected_count', 0)} 条。"
                            "仅显示前 80 条通过和前 40 条排除资源；确认前若修改规则，需重新预览。",
                }})
                page.append(_preview_table(preview))
                if sum(item.get("kind") == "full_season" for item in preview.get("matched") or []) > 1:
                    page.append({"component": "VAlert", "props": {
                        "type": "warning", "text": "当前规则匹配多个集数覆盖全集的版本；若选择全集候选，请收窄规则并重新预览。",
                    }})
        if approved:
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "text": "已批准订阅可在配置页撤销批准；规则会保留，订阅会暂停。",
            }})
        for sid, entry in approved:
            mode = "候选规则回填" if entry.get("mode") == "candidate" else "直接批准"
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"{mode} · 批准于 {entry.get('approved_at') or '?'}",
            }})
            preview = entry.get("replacement_preview") or {}
            if preview:
                page.append({"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": f"回刷预览于 {preview.get('searched_at') or '?'}：通过 "
                            f"{preview.get('matched_count', 0)} 条，排除 {preview.get('rejected_count', 0)} 条。"
                            "选择资源和集数后会再次搜索核对。",
                }})
                page.append(_preview_table(preview))
            history = entry.get("replacement_history") or []
            if history:
                latest = history[0]
                page.append({"component": "VAlert", "props": {
                    "type": "success", "variant": "tonal",
                    "text": f"最近手动提交：{latest.get('submitted_at') or '?'} · "
                            f"{latest.get('title') or '?'} · 集数 {latest.get('episodes') or []}",
                }})
        return page

    def notify_pending_subscriptions(self) -> None:
        """每天通过 MoviePilot 通知渠道汇总仍存在的待确认剧集。"""
        if not self._enabled or not self._daily_notice:
            return
        subscriptions = {str(item.id): item for item in self._subscribes.list()}
        with self._lock:
            pending = [
                (sid, entry.get("identity"), bool(entry.get("revoked")))
                for sid, entry in self._state["entries"].items()
                if isinstance(entry, dict) and entry.get("status") == "pending"
            ]
        lines = []
        for sid, identity, revoked in sorted(pending, key=lambda item: int(item[0])):
            subscribe = subscriptions.get(sid)
            if (subscribe is None or subscribe.type != MediaType.TV.value
                    or subscribe.state not in {"N", "R", "P", "S"}
                    or identity != _identity(subscribe)):
                continue
            name = " ".join(str(subscribe.name or "未命名剧集").split())
            lines.append(
                f"{len(lines) + 1}. #{sid} {name} S{int(subscribe.season or 1):02d}"
                + ("（已撤销批准）" if revoked else "")
            )
        text = f"当前有 {len(lines)} 条待确认订阅。"
        text += "\n" + "\n".join(lines) if lines else "\n暂无待确认的剧集订阅。"
        self.post_message(
            mtype=MessageType.Subscribe,
            title="剧集守门员：待确认订阅日报",
            text=text,
        )

    def get_service(self) -> list[dict[str, Any]]:
        """注册候选扫描和每日待确认订阅通知。"""
        if not self._enabled:
            return []
        services = [{
            "id": "candidate_refresh", "name": "更新待确认剧集版本候选",
            "trigger": "interval", "func": self.refresh_candidates,
            "kwargs": {"hours": 4},
        }]
        if self._daily_notice:
            services.append({
                "id": "pending_daily_notice", "name": "推送待确认剧集日报",
                "trigger": "cron", "func": self.notify_pending_subscriptions,
                "kwargs": {"hour": self._notice_hour, "minute": self._notice_minute},
            })
        return services

    def stop_service(self) -> None:
        """
        停止插件的一次性候选扫描和确认后补搜任务。
        """
        try:
            remove_plugin_once_job(self.__class__.__name__, "collect")
        except RuntimeError:
            return
        with self._lock:
            approved = [key for key, entry in self._state["entries"].items() if entry.get("needs_search")]
        for sid in approved:
            try:
                remove_plugin_once_job(self.__class__.__name__, f"search-{sid}")
            except RuntimeError:
                return

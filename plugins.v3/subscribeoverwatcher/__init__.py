"""
新增剧集订阅的选版确认插件。

同步拦截订阅下载，定期收集 E01 候选；用户确认后回填规则并恢复追更。
"""

import copy
import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaSource, MediaType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

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


class SubscribeOverwatcher(_PluginBase):
    """
    管理新剧集订阅的下载确认和首集版本候选。

    下载闸门读取持久确认记录；即使新增订阅广播晚到，也按创建前的 ID 水位拦截。
    允许全集直下时保留待确认订阅的搜索资格，只放行明确覆盖整季的下载事件。
    """

    plugin_name = "剧集守门员"
    plugin_desc = "新增剧集订阅先确认规格；可选择全集直下、直接批准或撤销批准。"
    plugin_icon = "icon.svg"
    plugin_version = "1.1.0"
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
        self._state: dict[str, Any] = {"baseline": None, "entries": {}}
        self._subscribes = SubscribeOper()

    def init_plugin(self, config: Optional[dict[str, Any]] = None) -> None:
        """
        加载确认记录，并处理配置页提交的一次性批准或撤销动作。

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
            self._enabled = bool(config.get("enabled"))
            self._confirm_full_pack = bool(config.get("confirm_full_pack", True))
            if self._enabled and not isinstance(self._state.get("baseline"), int):
                self._state["baseline"] = max((item.id for item in self._subscribes.list()), default=0)
                self._save_state()

        actions = [name for name in ("confirm", "direct_approve", "revoke") if config.get(name)]
        if not self._enabled:
            if actions:
                self.update_config({
                    **config, "confirm": False, "direct_approve": False, "revoke": False,
                    "candidate": "", "direct_subscription": "", "revoke_subscription": "",
                })
            return
        if actions:
            if len(actions) != 1:
                success, message = False, "一次只能提交一个批准或撤销操作"
            elif actions[0] == "confirm":
                success, message = self.confirm_candidate(str(config.get("candidate") or ""))
            elif actions[0] == "direct_approve":
                success, message = self.approve_subscription(config.get("direct_subscription"))
            else:
                success, message = self.revoke_approval(config.get("revoke_subscription"))
            with self._lock:
                self._state["last_confirmation"] = {
                    "success": success, "message": message, "at": _utc_now(),
                }
                self._save_state()
            if success:
                logger.info(f"剧集守门员：{message}")
            else:
                logger.warning(f"剧集守门员：{message}")
            self.update_config({
                **config, "confirm": False, "direct_approve": False, "revoke": False,
                "candidate": "", "direct_subscription": "", "revoke_subscription": "",
            })
        self._reconcile_pending_states()
        self._schedule_scan()

    def _save_state(self) -> None:
        """
        保存已由状态锁保护的确认和候选记录。
        """
        self.save_data("gate_state", self._state)

    def _is_approved(self, subscribe: Any) -> bool:
        """
        只认可与当前订阅身份完全一致的确认记录。
        """
        entry = self._state["entries"].get(str(subscribe.id)) or {}
        return entry.get("status") == "approved" and entry.get("identity") == _identity(subscribe)

    def _ensure_pending(self, subscribe: Any) -> bool:
        """
        纳入新剧集订阅，并按全集确认开关调整搜索资格。

        :param subscribe: 当前订阅记录
        :return: 本次是否新建待确认记录
        """
        if subscribe.id <= self._state["baseline"] or subscribe.type != MediaType.TV.value:
            return False
        with self._lock:
            if self._is_approved(subscribe):
                return False
            key = str(subscribe.id)
            entry = self._state["entries"].get(key)
            fresh = not isinstance(entry, dict) or entry.get("identity") != _identity(subscribe)
            if fresh:
                entry = {
                    "identity": _identity(subscribe),
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
        if sid is None or sid <= self._state["baseline"]:
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
                    released = (
                        subscribe.type != MediaType.TV.value
                        or self._is_approved(subscribe)
                        or (not self._confirm_full_pack
                            and not entry.get("revoked")
                            and is_complete_season_download(data, subscribe))
                    )
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
                "扫描待确认剧集首集", delay_seconds=15,
            )
        except RuntimeError:
            logger.warning("调度器尚未就绪，E01 候选将在周期任务中扫描")

    def refresh_candidates(self) -> None:
        """
        补收新增事件、更新 E01 候选并执行待处理的补搜。
        """
        if not self._enabled or not self._scan_lock.acquire(blocking=False):
            return
        try:
            subscribes = self._subscribes.list()
            active_ids = {str(subscribe.id) for subscribe in subscribes}
            with self._lock:
                stale_ids = set(self._state["entries"]) - active_ids
                for sid in stale_ids:
                    self._state["entries"].pop(sid, None)
                if stale_ids:
                    self._save_state()
            for subscribe in subscribes:
                if subscribe.id > self._state["baseline"] and subscribe.type == MediaType.TV.value:
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
        搜索一条待确认订阅的 E01，并保留历史候选。
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
                    candidate = resource_candidate(context, subscribe.season or 1, now)
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

    def confirm_candidate(self, choice: str) -> tuple[bool, str]:
        """
        回填所选 E01 的规则，再恢复订阅并安排补搜。

        :param choice: 配置页传入的“订阅 ID:候选 ID”
        :return: 是否确认成功及可展示的结果
        """
        try:
            sid_text, candidate_id = choice.split(":", 1)
            sid = int(sid_text)
        except (ValueError, TypeError):
            return False, "请先选择一个 E01 候选"
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
            rules = candidate.get("rules") or {}
            payload = {field: rules.get(field) for field in ("quality", "resolution", "effect", "include")}
            if rules.get("sites"):
                payload["sites"] = rules["sites"]
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
        try:
            if payload is not None:
                self._subscribes.update(sid, payload)
            entry.update({
                "status": "approved", "mode": "candidate" if candidate_id else "direct",
                "selected": candidate_id, "approved_at": _utc_now(),
                "needs_search": True, "plugin_paused": False, "revoked": False,
            })
            self._save_state()
            self._subscribes.update(sid, {"state": "R"})
        except Exception:
            self._state["entries"][str(sid)] = previous
            self._save_state()
            logger.exception(f"批准订阅 {sid} 时写入规则或状态失败")
            return False, "规则或状态保存失败，订阅仍保持拦截"
        self._schedule_approved_search(sid)
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
        返回全集确认策略及三个互斥的一次性操作控件。
        """
        with self._lock:
            choices = [
                {
                    "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
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
        return [
            {"component": "VForm", "content": [
                {"component": "VSwitch", "props": {"model": "enabled", "label": "接管新增剧集订阅"}},
                {"component": "VSwitch", "props": {
                    "model": "confirm_full_pack", "label": "全集种子也需确认",
                }},
                {"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": "开启全集确认时新增剧集暂停；关闭后仍拦截分集下载，只有识别为覆盖整季且通过原订阅规则的种子可自动下载。显式撤销的订阅始终暂停。每 4 小时更新 E01 候选。",
                }},
                {"component": "VSelect", "props": {
                    "model": "candidate", "label": "选择一个 E01 版本", "items": choices,
                    "item-title": "title", "item-value": "value", "clearable": True,
                }},
                {"component": "VSwitch", "props": {
                    "model": "confirm", "label": "确认回填规则并恢复订阅",
                }},
                {"component": "VSelect", "props": {
                    "model": "direct_subscription", "label": "选择直接批准的订阅", "items": pending_choices,
                    "item-title": "title", "item-value": "value", "clearable": True,
                }},
                {"component": "VSwitch", "props": {
                    "model": "direct_approve", "label": "直接批准，不回填 E01 规则",
                }},
                {"component": "VSelect", "props": {
                    "model": "revoke_subscription", "label": "选择撤销批准的订阅", "items": approved_choices,
                    "item-title": "title", "item-value": "value", "clearable": True,
                }},
                {"component": "VSwitch", "props": {
                    "model": "revoke", "label": "撤销批准并暂停，保留当前筛选规则",
                }},
            ]},
        ], {
            "enabled": False, "confirm_full_pack": True, "candidate": "", "confirm": False,
            "direct_subscription": "", "direct_approve": False,
            "revoke_subscription": "", "revoke": False,
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
                "type": "info", "text": "暂无待确认剧集。新增电视剧订阅后会按当前全集确认开关收集 E01 候选。",
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
                    for title in ("站点", "资源", "质量", "分辨率", "特效", "制作组", "最后发现")
                ]}]},
                {"component": "tbody", "content": rows},
            ]})
        if approved:
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "text": "已批准订阅可在配置页撤销批准；规则会保留，订阅会暂停。",
            }})
        for sid, entry in approved:
            mode = "E01 规则回填" if entry.get("mode") == "candidate" else "直接批准"
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"{mode} · 批准于 {entry.get('approved_at') or '?'}",
            }})
        return page

    def get_service(self) -> list[dict[str, Any]]:
        """
        每 4 小时重新搜索所有待确认剧集的首集。
        """
        return [{
            "id": "candidate_refresh", "name": "更新待确认剧集 E01 候选",
            "trigger": "interval", "func": self.refresh_candidates,
            "kwargs": {"hours": 4},
        }] if self._enabled else []

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

"""
新增剧集订阅的选版确认插件。

同步拦截订阅下载，定期收集目标季候选；用户确认后回填规则并恢复追更。
"""

import copy
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from app.application.torrent.download import TorrentHelper
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import ChainEventType, EventType, MediaSource, MediaType, MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .directory import directory_choices, preview_download_path, selected_directory_root
from .preview import (
    SUBSCRIPTION_RULE_OPTIONS, candidate_default_rules, display_draft_rules, editor_draft_rules,
    normalize_draft_rules, parse_episode_selection, preview_resource,
    readable_rejection, rule_summary,
)
from .rules import (
    candidate_is_eligible, candidate_rule_names, candidate_team_pattern, is_complete_season_download, merge_candidates,
    public_page_url, resource_candidate,
    target_seasons,
)
from .rulegroups import UNFILTERED_GROUP_NAME, ensure_unfiltered_group, group_rejections, preview_group_policy
from .search import CandidateSearchChain, failure_hold, search_names


def _utc_now() -> str:
    """
    返回适合排序和展示的 UTC 时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _search_diagnostics_snapshot(diagnostics: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """每条资源的查询词仅随完整搜索缓存保存在内存，不扩大持久记录。"""
    if diagnostics is None:
        return None
    snapshot = {key: value for key, value in diagnostics.items() if key != "resource_keywords"}
    if snapshot.get("original_search"):
        snapshot["original_search"] = {key: value for key, value in snapshot["original_search"].items()
                                       if key not in {"resource_ids", "custom_words", "site_failures"}}
    return snapshot


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
    为不同类型的目标季候选生成简短标签。
    """
    kind = candidate.get("kind")
    if kind == "full_season":
        return "识别集数覆盖全集"
    if kind == "cross_season":
        return "跨季资源（含目标季）"
    if kind == "suspected_full":
        return "标题疑似整季"
    if kind == "episode":
        return "分集"
    return "单集 E01"


def _candidate_coverage(candidate: dict[str, Any]) -> str:
    """
    显示标题解析出的具体集号；不把不连续的集号写成完整范围。
    """
    if candidate.get("kind") == "cross_season":
        seasons = candidate.get("seasons") or []
        labels = "、".join(f"S{number:02d}" for number in seasons
                          if isinstance(number, int) and not isinstance(number, bool) and number >= 0)
        return f"{labels or '跨季'}（各季集数待核实）"
    episodes = sorted({episode for episode in candidate.get("episodes") or []
                       if isinstance(episode, int) and not isinstance(episode, bool) and episode > 0})
    total = candidate.get("total_episode")
    total = total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else None
    if not episodes:
        return f"未识别具体集号（目标总集数 {total or '未知'}）"
    spans = []
    first = last = episodes[0]
    for episode in episodes[1:]:
        if episode == last + 1:
            last = episode
            continue
        spans.append(f"E{first:02d}" + (f"–E{last:02d}" if first != last else ""))
        first = last = episode
    spans.append(f"E{first:02d}" + (f"–E{last:02d}" if first != last else ""))
    return f"{', '.join(spans)}/{total or '?'}"


def _size_label(size_bytes: Any, *, exact: bool = False) -> str:
    """把候选快照中的字节大小显示为便于比较的单位。"""
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        return "未知"
    size = size_bytes
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            label = f"{size} B" if unit == "B" else f"{size:.2f} {unit}"
            return f"{label}（{size_bytes:,} B）" if exact and unit != "B" else label
        size /= 1024
    return "未知"


def _size_cell(item: dict[str, Any]) -> dict[str, Any]:
    """详情表格同时显示易读大小和合并所用的精确字节数。"""
    size = item.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return {"component": "td", "text": "未知"}
    props = {"title": f"精确大小：{size:,} B"}
    if size < 1024:
        return {"component": "td", "text": f"{size} B", "props": props}
    return {"component": "td", "props": props, "content": [
        {"component": "div", "text": _size_label(size), "props": {"style": "white-space: nowrap"}},
        {"component": "div", "text": f"{size:,} B", "props": {
            "class": "text-caption text-medium-emphasis", "style": "white-space: nowrap",
        }},
    ]}


def _draft_rules(candidate: dict[str, Any], raw: str, mode: str) -> dict[str, Any]:
    """选择所选站点或所有站点，其他候选规格照常回填。"""
    if mode not in {"candidate", "all_sites"}:
        raise ValueError("站点范围无效，请重新选择")
    rules = normalize_draft_rules(candidate_default_rules(candidate), raw, candidate_rule_names(candidate))
    if mode == "all_sites":
        rules["sites"] = []
    return rules


def _rule_scope(subscribe: Any) -> str:
    """
    冻结预览所依赖的订阅身份、筛选规则和目标集数。
    """
    return json.dumps(
        {
            "identity": _identity(subscribe),
            "total_episode": subscribe.total_episode,
            "rule_groups": preview_group_policy(subscribe),
            "rules": {name: getattr(subscribe, name, None) for name in (
                "quality", "resolution", "effect", "include", "exclude", "sites", "filter_groups",
                "best_version", "best_version_full", "current_priority", "episode_priority",
                "save_path", "downloader",
                "keyword", "search_imdbid", "custom_words",
            )},
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )


def _subscription_progress(subscribe: Any) -> tuple[Optional[int], Optional[int]]:
    """按宿主订阅字段计算已完成集数；总集数未知时不猜测进度。"""
    try:
        total = int(getattr(subscribe, "total_episode", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    if total <= 0:
        return None, None

    completed = getattr(subscribe, "completed_episode", None)
    if completed is None and not getattr(subscribe, "best_version", False):
        lack = getattr(subscribe, "lack_episode", None)
        if lack is None:
            return None, total
        try:
            completed = total - int(lack)
        except (TypeError, ValueError):
            return None, total
    elif completed is None:
        start = max(1, min(int(getattr(subscribe, "start_episode", 1) or 1), total + 1))
        completed = start - 1
        if getattr(subscribe, "best_version_full", False):
            if getattr(subscribe, "current_priority", None) == 100:
                completed = total
        else:
            priorities = getattr(subscribe, "episode_priority", None) or {}
            if isinstance(priorities, dict) and priorities:
                completed += sum(
                    1 for episode, priority in priorities.items()
                    if str(episode).isdigit() and start <= int(episode) <= total and priority == 100
                )
            elif getattr(subscribe, "current_priority", None) == 100:
                completed = total
    try:
        return max(0, min(int(completed), total)), total
    except (TypeError, ValueError):
        return None, total


def _progress_label(subscribe: Any) -> str:
    """在订阅列表中显示可辨识的宿主进度口径。"""
    completed, total = _subscription_progress(subscribe)
    if completed is None:
        return "订阅进度未知"
    kind = "洗版完成" if getattr(subscribe, "best_version", False) else "已入库"
    return f"{kind} {completed}/{total} 集"


def _display_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """仅在展示时合并同标题、同大小、同集数且解析规格一致的跨站资源。"""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for index, item in enumerate(items):
        title = " ".join(str(item.get("title") or "").split()).casefold()
        size = item.get("size_bytes")
        if not title or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            key: tuple[Any, ...] = ("single", index)
        else:
            episodes = item.get("episodes")
            episode_key = tuple(sorted({number for number in episodes
                                        if isinstance(number, int) and not isinstance(number, bool) and number > 0})) \
                if isinstance(episodes, (list, tuple, set)) else ()
            key = (
                "version", title, size, item.get("kind"), episode_key, item.get("total_episode"),
                tuple(item.get("seasons") or []) if item.get("kind") == "cross_season" else (),
                item.get("quality"), item.get("resolution"), item.get("effect"), item.get("team"),
                item.get("reason"),
            )
            site = item.get("site_id") or item.get("site_name")
            if site is None or any((member.get("site_id") or member.get("site_name")) == site
                                   for member in groups.get(key, [])):
                key = ("single", index)
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def _site_cell(items: list[dict[str, Any]]) -> dict[str, Any]:
    """单站或跨站资源都在站点名上显示详情链接。"""
    content = []
    for index, item in enumerate(items):
        if index:
            content.append({"component": "span", "text": "、"})
        label = item.get("site_name") or str(item.get("site_id") or "?")
        url = public_page_url(item.get("page_url"))
        if url:
            content.append({"component": "a", "text": label, "props": {
                "href": url, "target": "_blank", "rel": "noopener noreferrer",
                "title": f"打开{label}详情页", "class": "text-primary text-decoration-underline",
            }})
        else:
            content.append({"component": "span", "text": label})
    return {"component": "td", "content": content}


def _candidate_action_cell(sid: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """合并展示的候选仍按站点分别进入配置。"""
    buttons = []
    for item in items:
        site = item.get("site_name") or str(item.get("site_id") or "未知站点")
        buttons.append({"component": "VBtn", "props": {
            "variant": "tonal", "color": "primary", "size": "small", "class": "ma-1",
            "data-candidate-id": f"{sid}:{item['id']}",
            "aria-label": f"选择{site}的版本并前往配置",
        }, "content": [{"component": "span", "text": f"配置·{site}"}]})
    return {"component": "td", "content": buttons}


def _full_season_version_count(items: list[dict[str, Any]]) -> int:
    """跨站同版本整季资源在确认和提示中只计一次。"""
    return sum(group[0].get("kind") == "full_season" for group in _display_groups(items))


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
            _site_cell(group),
            {"component": "td", "text": item.get("title") or ""},
            _size_cell(item),
            {"component": "td", "text": item.get("download_path") or item.get("download_path_error") or "—"},
            {"component": "td", "text": item.get("reason") or "符合当前规则"},
        ]}
        for accepted, items in ((True, preview.get("matched") or []), (False, preview.get("rejected") or []))
        for group in _display_groups(items)
        for item in group[:1]
    ]
    return {"component": "VTable", "props": {"hover": True}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "text": title}
            for title in ("结果", "类型", "识别集号/目标总集数", "站点/详情", "资源", "大小", "预计下载目录", "原因")
        ]}]},
        {"component": "tbody", "content": rows},
    ]}


def _config_preview(sid: str, entry: dict[str, Any], field: str) -> Optional[dict[str, Any]]:
    """向配置面板提供统计和展示表格，不重复传输完整资源列表。"""
    preview = entry.get(field)
    if not preview:
        return None
    return {
        **{key: preview.get(key) for key in (
            "choice", "rule_mode", "download_directory", "draft_json", "searched_at", "searched_ts",
            "matched_count", "rejected_count", "search_cached", "search_age_seconds", "timings",
            "search_result_count", "search_trigger", "rule_groups_disabled",
            "rule_groups_checked", "rule_groups_auto_disabled", "rule_group_notice", "rule_group_comparison",
            "effective_rules", "rejection_summary", "search_source", "search_result_at", "search_sites",
            "search_diagnostics",
            "search_keyword", "original_search_keyword", "search_keyword_notice",
            "search_keyword_changed", "search_keyword_verified",
        )},
        "subscription_id": sid, "table": _preview_table(preview),
        "matched_shown": len(preview.get("matched") or []),
        "rejected_shown": len(preview.get("rejected") or []),
    }


class SubscribeOverwatcher(_PluginBase):
    """
    管理新剧集订阅的下载确认和目标季版本候选。

    下载闸门读取持久确认记录；即使新增订阅广播晚到，也按创建前的 ID 水位拦截。
    允许全集直下时保留待确认订阅的搜索资格，只放行明确覆盖整季的下载事件。
    """

    plugin_name = "剧集守门员"
    plugin_desc = "剧集订阅先预览规则再确认，可手动选择集数重新下载，并每日汇总待确认订阅。"
    plugin_icon = "icon.svg"
    plugin_version = "1.9.2"
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
        self._queued_scan_ids: set[int] = set()
        # 完整搜索上下文只在进程内复用，按容量保留，不写入插件持久数据。
        self._preview_search_cache: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}
        self._search_cache_sequence = 0
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
        config = dict(config or {})
        cache_setting_removed = "search_cache_hours" in config
        config.pop("search_cache_hours", None)
        historical_migrated = "historical_subscription" in config \
            and not isinstance(config["historical_subscription"], list)
        if historical_migrated:
            selected = config["historical_subscription"]
            config["historical_subscription"] = [selected] if selected not in (None, "") else []
        with self._lock:
            was_active = self._enabled
            saved = self.get_data("gate_state")
            if isinstance(saved, dict):
                self._state = saved
            else:
                self._state = {"baseline": None, "entries": {}}
            if not isinstance(self._state.get("entries"), dict):
                self._state["entries"] = {}
            stale_candidates = False
            for entry in self._state["entries"].values():
                if not isinstance(entry, dict) or not isinstance(entry.get("candidates"), list):
                    continue
                kept = [item for item in entry["candidates"] if candidate_is_eligible(item)]
                if len(kept) != len(entry["candidates"]):
                    entry["candidates"] = kept
                    entry.pop("preview", None)
                    stale_candidates = True
            if stale_candidates:
                self._save_state()
            previously_disabled = self._state.get("was_enabled") is False
            self._enabled = bool(config.get("enabled"))
            if not self._enabled:
                self._preview_search_cache.clear()
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
        if config.get("operation") not in (None, "", "save"):
            actions.append(str(config["operation"]))
        if not self._enabled:
            if actions:
                with self._lock:
                    self._state["last_confirmation"] = {
                        "success": False, "message": "请先启用插件，再执行所选操作",
                        "at": _utc_now(),
                        "request_id": str(config.get("operation_request_id") or ""),
                    }
                    self._save_state()
                self.update_config({
                    **config, "operation": "save", "adopt": False, "preview": False, "confirm": False,
                    "direct_approve": False, "revoke": False,
                    "preview_replace": False, "submit_replace": False,
                    "historical_subscription": [], "candidate_subscription": "", "candidate": "",
                    "direct_subscription": "",
                    "download_directory": "",
                    "revoke_subscription": "", "rule_json": "", "replace_subscription": "",
                    "replace_resource": "", "replace_episodes": "", "operation_request_id": "",
                })
            elif historical_migrated or cache_setting_removed:
                self.update_config(config)
            return
        normalized_rules = ""
        if actions:
            if len(actions) != 1:
                success, message = False, "一次只能提交一个接管、预览、批准、撤销或回刷操作"
            elif actions[0] == "adopt":
                success, message = self.adopt_subscriptions(config.get("historical_subscription") or [])
            elif actions[0] == "preview":
                success, message, normalized_rules = self.preview_candidate(
                    str(config.get("candidate") or ""), str(config.get("rule_json") or ""),
                    str(config.get("rule_mode") or "candidate"),
                    str(config.get("download_directory") or ""),
                    refresh_search=bool(config.get("refresh_search")),
                )
            elif actions[0] == "confirm":
                success, message = self.confirm_candidate(
                    str(config.get("candidate") or ""), str(config.get("rule_json") or ""),
                    str(config.get("rule_mode") or "candidate"),
                    str(config.get("download_directory") or ""),
                )
            elif actions[0] == "direct_approve":
                success, message = self.approve_subscription(config.get("direct_subscription"))
            elif actions[0] == "revoke":
                success, message = self.revoke_approval(config.get("revoke_subscription"))
            elif actions[0] == "preview_replace":
                success, message = self.preview_replacements(
                    config.get("replace_subscription"), refresh_search=bool(config.get("refresh_search")),
                )
            elif actions[0] == "submit_replace":
                success, message = self.submit_replacement(
                    str(config.get("replace_resource") or ""), str(config.get("replace_episodes") or ""),
                )
            else:
                success, message = False, "未知操作，请重新选择"
            with self._lock:
                self._state["last_confirmation"] = {
                    "success": success, "message": message, "at": _utc_now(),
                    "request_id": str(config.get("operation_request_id") or ""),
                }
                self._save_state()
            if success:
                logger.info(f"剧集守门员：{message}")
            else:
                logger.warning(f"剧集守门员：{message}")
            updated_config = {
                **config, "operation": "save", "operation_request_id": "",
                "refresh_search": False,
                "adopt": False, "preview": False, "confirm": False,
                "direct_approve": False, "revoke": False, "preview_replace": False, "submit_replace": False,
            }
            if actions == ["preview"] and success:
                updated_config["rule_json"] = normalized_rules
            if success:
                if actions == ["adopt"]:
                    updated_config["historical_subscription"] = []
                elif actions == ["direct_approve"]:
                    updated_config["direct_subscription"] = ""
                elif actions == ["revoke"]:
                    updated_config["revoke_subscription"] = ""
                elif actions == ["confirm"]:
                    updated_config["candidate_subscription"] = ""
                    updated_config["candidate"] = ""
                    updated_config["rule_mode"] = "candidate"
                    updated_config["rule_json"] = ""
                    updated_config["download_directory"] = ""
                elif actions == ["preview_replace"]:
                    updated_config["replace_resource"] = ""
                    updated_config["replace_episodes"] = ""
                elif actions == ["submit_replace"]:
                    updated_config["replace_resource"] = ""
                    updated_config["replace_episodes"] = ""
            self.update_config(updated_config)
        elif historical_migrated or cache_setting_removed:
            self.update_config(config)
        self._reconcile_pending_states()
        if not was_active:
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
        接管一条已有的剧集订阅。

        :param sid_value: 订阅 ID
        :return: 是否纳入管理及结果说明
        """
        return self.adopt_subscriptions([sid_value])

    def adopt_subscriptions(self, sid_values: list[Any]) -> tuple[bool, str]:
        """
        一次接管多条历史剧集，先校验全部选择再逐条写入。
        """
        if not sid_values:
            return False, "请先选择历史剧集订阅"
        try:
            ids = list(dict.fromkeys(int(value) for value in sid_values))
        except (ValueError, TypeError):
            return False, "所选历史剧集订阅无效，请重新选择"
        with self._lock:
            subscriptions = []
            for sid in ids:
                subscribe = self._subscribes.get(sid)
                if not subscribe or subscribe.type != MediaType.TV.value or sid > self._state["baseline"]:
                    return False, f"订阅 {sid} 不是可纳入的历史剧集"
                if subscribe.state not in {"N", "R", "P", "S"}:
                    return False, f"订阅 {sid} 已不在可纳入的订阅列表中"
                if str(sid) in self._state["entries"]:
                    return False, f"订阅 {sid} 已由插件管理"
                subscriptions.append(subscribe)
            for subscribe in subscriptions:
                if not self._ensure_pending(subscribe, force_historical=True):
                    return False, f"订阅 {subscribe.id} 纳入管理失败，请检查已纳入列表"
        self._schedule_scan(ids)
        if len(ids) == 1:
            return True, f"订阅 {ids[0]} 已纳入管理，原有筛选规则暂未修改"
        return True, f"已纳入管理 {len(ids)} 条历史剧集订阅，原有筛选规则暂未修改"

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
            self._schedule_scan([sid])

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

    def _schedule_scan(self, subscription_ids: Optional[list[int]] = None) -> None:
        """
        合并新增订阅；启动时只补收没有扫描过或已到四小时周期的订阅。
        """
        if not self._enabled:
            return
        with self._lock:
            if subscription_ids is None:
                subscription_ids = []
                cutoff = datetime.now(timezone.utc).timestamp() - 4 * 3600
                for subscribe in self._subscribes.list():
                    if subscribe.type != MediaType.TV.value or not self._is_managed(subscribe) \
                            or self._is_approved(subscribe):
                        continue
                    entry = self._state["entries"].get(str(subscribe.id)) or {}
                    try:
                        last_scan = datetime.fromisoformat(entry.get("last_scan") or "")
                        if last_scan.tzinfo is None:
                            last_scan = last_scan.replace(tzinfo=timezone.utc)
                        if last_scan.timestamp() > cutoff:
                            continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                    subscription_ids.append(subscribe.id)
            self._queued_scan_ids.update(subscription_ids)
            if not self._queued_scan_ids:
                return
        try:
            add_plugin_once_job(
                self.__class__.__name__, "collect", self._run_scheduled_scan,
                "扫描待确认剧集版本", delay_seconds=15,
            )
        except RuntimeError:
            logger.warning("调度器尚未就绪，版本候选将在周期任务中扫描")

    def _run_scheduled_scan(self) -> None:
        """一次性任务只扫描合并后的新增目标；周期扫描正在运行时保留队列。"""
        with self._lock:
            ids = sorted(self._queued_scan_ids)
            self._queued_scan_ids.clear()
        if ids and not self.refresh_candidates(subscription_ids=ids):
            self._schedule_scan(ids)

    def refresh_candidates(self, *, subscription_ids: Optional[list[int]] = None) -> bool:
        """
        补收新增事件、更新目标季候选并执行待处理的补搜。
        """
        if not self._enabled or not self._scan_lock.acquire(blocking=False):
            return False
        try:
            subscribes = self._subscribes.list()
            active_ids = {str(subscribe.id) for subscribe in subscribes}
            active_identities = {_identity(subscribe) for subscribe in subscribes
                                 if subscribe.type == MediaType.TV.value}
            with self._lock:
                self._preview_search_cache = {
                    key: cached for key, cached in self._preview_search_cache.items()
                    if key[0] in active_identities
                }
                stale_ids = set(self._state["entries"]) - active_ids
                stale_ids.update(
                    str(subscribe.id) for subscribe in subscribes
                    if (entry := self._state["entries"].get(str(subscribe.id)))
                    and entry.get("source") == "historical"
                    and entry.get("identity") != _identity(subscribe)
                )
                for sid in stale_ids:
                    self._state["entries"].pop(sid, None)
                records_cleaned = self._prune_confirmed_records()
                if stale_ids or records_cleaned:
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
                if subscription_ids is not None:
                    selected = set(subscription_ids)
                    pending_ids = [sid for sid in pending_ids if sid in selected]
                    pending_search_ids = [sid for sid in pending_search_ids if sid in selected]
                    search_ids = [sid for sid in search_ids if sid in selected]
                self._queued_scan_ids.difference_update(pending_ids)
            for sid in pending_ids:
                self._scan_one(sid)
            for sid in pending_search_ids:
                self._search_pending(sid)
            for sid in search_ids:
                self._search_approved(sid)
        finally:
            self._scan_lock.release()
        return True

    def _prune_confirmed_records(self) -> bool:
        """扫描时清理批准满 24 小时的候选与确认预览；调用方持有状态锁。"""
        cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
        changed = False
        for entry in self._state["entries"].values():
            if entry.get("status") != "approved":
                continue
            try:
                approved_at = datetime.fromisoformat(str(entry.get("approved_at") or "").replace("Z", "+00:00"))
                if approved_at.tzinfo is None:
                    approved_at = approved_at.replace(tzinfo=timezone.utc)
                approved_ts = approved_at.timestamp()
            except (ValueError, TypeError, OverflowError):
                approved_ts = None
            if approved_ts is not None and approved_ts <= cutoff:
                for field in ("candidates", "preview"):
                    if field in entry:
                        entry.pop(field)
                        changed = True
            # 后续回刷预览从自己的生成时间计时，不能因订阅早已批准而立即清掉。
            preview_ts = (entry.get("replacement_preview") or {}).get("searched_ts")
            if isinstance(preview_ts, (int, float)) and preview_ts <= cutoff:
                entry.pop("replacement_preview", None)
                changed = True
        return changed

    def _scan_one(self, sid: int) -> None:
        """
        搜索一条待确认订阅的首集、整季及含目标季的跨季资源，并保留历史候选。
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
            diagnostics: dict[str, Any] = {}
            contexts = self._search_unfiltered(subscribe, diagnostics=diagnostics)
            now = _utc_now()
            fresh = []
            for context in contexts:
                try:
                    candidate = resource_candidate(
                        context, subscribe.season or 1, now, subscribe.total_episode,
                    )
                except (TypeError, ValueError, OverflowError):
                    logger.warning(f"订阅 {sid} 的一个目标季搜索结果格式异常，已跳过")
                    continue
                if candidate:
                    fresh.append(candidate)
            with self._lock:
                entry = self._state["entries"].get(str(sid))
                if entry and entry.get("status") == "pending" and entry.get("identity") == _identity(subscribe):
                    entry["candidates"] = merge_candidates(entry.get("candidates") or [], fresh)
                    entry["last_scan"] = now
                    entry["last_scan_count"] = len(fresh)
                    entry["search_diagnostics"] = _search_diagnostics_snapshot(diagnostics)
                    entry.pop("last_error", None)
                    self._remember_search(subscribe, contexts, [], "候选扫描", now, diagnostics)
                    self._save_state()
        except Exception:
            logger.exception(f"扫描订阅 {sid} 的目标季候选失败")
            with self._lock:
                entry = self._state["entries"].get(str(sid))
                if entry and entry.get("status") == "pending":
                    entry["last_error"] = "搜索失败，详情见插件日志"
                    self._save_state()

    def _site_hold(self, site: dict[str, Any]) -> Optional[dict[str, Any]]:
        """冷却按站点共享，保存配置和重载插件不会提前解除。"""
        with self._lock:
            pause = (self._state.get("site_holds") or {}).get(str(site.get("id")))
            return dict(pause) if pause and pause.get("until", 0) > time.time() else None

    def _hold_site(self, site: dict[str, Any], outcome: str, detail: str) -> dict[str, Any]:
        until, reason = failure_hold(outcome, detail, time.time())
        with self._lock:
            holds = self._state.setdefault("site_holds", {})
            holds = {key: value for key, value in holds.items() if value.get("until", 0) > time.time()}
            previous = holds.get(str(site.get("id"))) or {}
            pause = {"until": max(until, previous.get("until", 0)), "reason": reason,
                     "site_name": str(site.get("name") or "未知站点")[:100]}
            holds[str(site.get("id"))] = pause
            self._state["site_holds"] = holds
            self._save_state()
        logger.warning(f"站点 {site.get('name')} {reason}，插件候选搜索暂停至 "
                       f"{datetime.fromtimestamp(pause['until']).astimezone().isoformat(timespec='seconds')}")
        return pause

    def _search_unfiltered(
        self, subscribe: Any, timings: Optional[dict[str, float]] = None, *, sites: Optional[list[int]] = None,
        diagnostics: Optional[dict[str, Any]] = None,
    ) -> list[Any]:
        """优先带季查询，再用原订阅搜索验证是否需要调整关键词并补收跨季包。"""
        started = time.perf_counter()
        media = MediaChain().recognize_media(
            media_source=MediaSource(subscribe.media_source),
            media_id=str(subscribe.media_id), mtype=MediaType.TV,
        )
        if timings is not None:
            timings["recognition_seconds"] = time.perf_counter() - started
        if media is None:
            return []
        target = copy.copy(media)
        season = subscribe.season or 1
        # 用查询词限制站点返回量；媒体不绑定单季，以免宿主提前剔除跨季包。
        target.season = None
        media_source, media_id = resolve_media_identity(media=target)
        media_key = build_media_key(media_source, media_id)
        if not media_key:
            return []
        started = time.perf_counter()
        names = search_names(target, subscribe.name)
        if not names:
            return []
        site_reports = []
        resource_keywords: dict[str, list[str]] = {}
        rounds = []
        supplemental_failed = False
        original_search: dict[str, Any] = {"checked": False, "resource_ids": []}

        def search(name: str, *, focused: bool) -> list[Any]:
            keyword = f"{name} S{season:02d}" if focused else (getattr(subscribe, "keyword", None) or None)
            label = keyword or "原订阅默认关键词（剧名及别名）"
            area = "imdbid" if not focused and getattr(subscribe, "search_imdbid", False) \
                and getattr(target, "imdb_id", None) else "title"
            counts = {"identified_count": 0, "target_count": 0}

            def target_filter(contexts: list[Any]) -> list[Any]:
                retained = [context for context in contexts
                            if getattr(context, "match_status", None) in (None, "exact")
                            and target_seasons(getattr(context, "meta_info", None), season)
                            and (not sites or getattr(getattr(context, "torrent_info", None), "site", None) in sites)]
                counts.update(identified_count=len(contexts), target_count=len(retained))
                return retained

            round_started = time.perf_counter()
            chain = CandidateSearchChain(self._site_hold, self._hold_site, focused_season=season if focused else None)
            original_arguments = {}
            if not focused:
                original_arguments = {
                    "area": area,
                    "custom_words": subscribe.custom_words.split("\n") if getattr(subscribe, "custom_words", None) else None,
                }
            try:
                result = chain.process(mediainfo=copy.copy(target), keyword=keyword, rule_groups=[],
                                       sites=sites or None, candidate_filter=target_filter, **original_arguments)
            finally:
                site_reports.extend(chain.site_reports)
            for context in result:
                try:
                    item = preview_resource(context, season, subscribe.total_episode, _utc_now())
                except (TypeError, ValueError, OverflowError):
                    continue
                if not item:
                    continue
                if not focused:
                    original_search["resource_ids"].append(item["id"])
                    continue
                actual_keyword = next((report["keyword"] for report in chain.site_reports
                                       if report.get("site_id") == item.get("site_id")
                                       and report.get("count") and report.get("keyword")), keyword)
                words = resource_keywords.setdefault(item["id"], [])
                if actual_keyword not in words:
                    words.append(actual_keyword)
            rounds.append({"keyword": label, "kind": "target_season" if focused else "original_search",
                           **counts, "result_count": len(result),
                           "site_keywords": list(dict.fromkeys(report["keyword"] for report in chain.site_reports
                                                                if report.get("keyword"))),
                           "seconds": round(time.perf_counter() - round_started, 3)})
            logger.info(f"订阅 {subscribe.id} {'带季搜索' if focused else '原订阅搜索验证'}：{label}，"
                        f"媒体匹配 {counts['identified_count']} 条，目标季 {len(result)} 条")
            # 同一站失败后，由请求边界跳过后续词；记录不包含 URL、Cookie 或响应原文。
            failures = [item for item in chain.site_reports if item.get("reason")]
            if not focused:
                original_search.update(checked=True, keyword=keyword, search_imdbid=bool(getattr(subscribe, "search_imdbid", False)),
                                       custom_words=getattr(subscribe, "custom_words", None) or None,
                                       site_failures=failures)
            if failures and not result:
                logger.warning(f"订阅 {subscribe.id} 本轮有 {len(failures)} 个站点请求失败或处于冷却")
            return result

        contexts = search(names[0], focused=True)
        if not contexts and len(names) > 1:
            contexts = search(names[1], focused=True)
        # 保留宿主原关键词和别名策略，验证无需带季也能召回的版本；同时补收跨季包。
        try:
            supplemental = search(names[0], focused=False)
        except Exception:
            if not contexts:
                raise
            supplemental = []
            supplemental_failed = True
            logger.exception(f"订阅 {subscribe.id} 原订阅搜索验证失败，保留已取得的目标季资源")
        def resource_key(context: Any) -> tuple[Any, ...]:
            torrent = getattr(context, "torrent_info", None)
            meta = getattr(context, "meta_info", None)
            return (getattr(torrent, "site", None), getattr(torrent, "title", None), getattr(torrent, "size", None),
                    getattr(torrent, "description", None), getattr(torrent, "enclosure", None),
                    tuple(getattr(meta, "season_list", None) or []),
                    tuple(getattr(meta, "episode_list", None) or []))

        # 只合并不同搜索词召回的相同资源；同一轮的不唯一记录保留给提交前校验。
        focused_keys = {resource_key(context) for context in contexts}
        contexts += [context for context in supplemental if resource_key(context) not in focused_keys]
        if diagnostics is not None:
            failures = {item["site_id"]: item for item in site_reports if item.get("reason")}
            diagnostics.update(rounds=rounds, site_failures=list(failures.values()), target_season=season,
                               supplemental_failed=supplemental_failed, resource_keywords=resource_keywords,
                               original_search=original_search)
        if timings is not None:
            timings["search_seconds"] = time.perf_counter() - started
        return contexts

    def _remember_search(
        self, subscribe: Any, contexts: list[Any], sites: list[int], source: str, searched_at: str,
        diagnostics: Optional[dict[str, Any]] = None,
    ) -> None:
        """每个媒体和站点范围只保留最新一轮结果，不合并历次搜索。"""
        with self._lock:
            key = (_identity(subscribe), tuple(sorted(set(sites))))
            if key not in self._preview_search_cache and len(self._preview_search_cache) >= 64:
                oldest = min(self._preview_search_cache, key=lambda item: self._preview_search_cache[item]["at"])
                self._preview_search_cache.pop(oldest, None)
            self._search_cache_sequence += 1
            self._preview_search_cache[key] = {
                "at": time.monotonic(), "contexts": contexts, "source": source, "searched_at": searched_at,
                "sequence": self._search_cache_sequence,
                "diagnostics": diagnostics,
            }

    def _cached_search(self, subscribe: Any, sites: list[int]) -> Optional[dict[str, Any]]:
        """预览及开始前的状态检查共用同一缓存选择和站点筛选。"""
        identity = _identity(subscribe)
        selected_sites = set(sites)
        with self._lock:
            compatible = [
                entry for (media_identity, cached_sites), entry in self._preview_search_cache.items()
                if media_identity == identity and (not cached_sites or
                    (selected_sites and selected_sites.issubset(cached_sites)))
            ]
            if not compatible:
                return None
            cached = max(compatible, key=lambda item: (item["at"], item["sequence"]))
            diagnostics = copy.deepcopy(cached.get("diagnostics"))
            if sites and diagnostics:
                diagnostics["site_failures"] = [item for item in diagnostics.get("site_failures", [])
                                                if item.get("site_id") in selected_sites]
            return {**cached, "diagnostics": diagnostics, "contexts": [
                context for context in cached["contexts"]
                if not sites or getattr(getattr(context, "torrent_info", None), "site", None) in sites
            ]}

    def _preview_contexts(
        self, subscribe: Any, refresh_search: bool, timings: dict[str, float], sites: list[int],
    ) -> tuple[list[Any], bool, float, str, str, Any]:
        """单站可复用覆盖该站的扫描；全站预览不能复用不完整的单站搜索。"""
        cached = self._cached_search(subscribe, sites) if not refresh_search else None
        if cached is not None:
            return (cached["contexts"], True, time.monotonic() - cached["at"], cached["source"],
                    cached["searched_at"], cached.get("diagnostics"))
        key = (_identity(subscribe), tuple(sorted(set(sites))))
        with self._lock:
            # 强制刷新失败时不能继续使用被要求淘汰的旧搜索。
            self._preview_search_cache.pop(key, None)
        diagnostics: dict[str, Any] = {}
        contexts = self._search_unfiltered(subscribe, timings, sites=sites, diagnostics=diagnostics)
        searched_at = _utc_now()
        self._remember_search(subscribe, contexts, sites, "规则预览搜索", searched_at, diagnostics)
        return contexts, False, 0.0, "规则预览搜索", searched_at, diagnostics

    @staticmethod
    def _rule_rejection(
        subscribe: Any, context: Any, filter_params: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        用宿主的同一基础筛选器核对站点与订阅文本规则。
        """
        torrent = getattr(context, "torrent_info", None)
        if torrent is None:
            return "资源缺少种子信息"
        sites = getattr(subscribe, "sites", None) or []
        if sites and getattr(torrent, "site", None) not in sites:
            return "不在订阅站点范围内"
        return TorrentHelper.filter_torrent_reason(
            torrent, SubscribeChain.get_params(subscribe) if filter_params is None else filter_params,
        )

    def _search_rule_preview(
        self, subscribe: Any, rules: dict[str, Any], *, require_episodes: bool = False,
        download_directory: str = "",
        refresh_search: bool = False, candidate: Optional[dict[str, Any]] = None,
        auto_disable_rule_groups: bool = False,
    ) -> dict[str, Any]:
        """
        只搜索并筛选当前资源，不更新订阅或提交下载。

        :param subscribe: 待预览的订阅
        :param rules: 候选规格生成的临时规则
        :param require_episodes: 手动选集下载时要求明确识别集数
        :param download_directory: 可选的资源根目录；留空预览订阅原目录或宿主默认目录
        :return: 通过、拒绝及统计信息
        """
        started = time.perf_counter()
        timings = {"recognition_seconds": 0.0, "search_seconds": 0.0}
        working = copy.copy(subscribe)
        for field, value in rules.items():
            if value is not None:
                setattr(working, field, value)
            elif field != "sites":
                setattr(working, field, None)
        search_sites = getattr(working, "sites", None) or []
        contexts, cached, cache_age, source, result_at, diagnostics = self._preview_contexts(
            subscribe, refresh_search, timings, search_sites,
        )
        resource_keywords = (diagnostics or {}).get("resource_keywords") or {}
        candidate_keywords = resource_keywords.get((candidate or {}).get("id")) or []
        original_search = (diagnostics or {}).get("original_search") or {}
        original_keyword = getattr(subscribe, "keyword", None) or None
        original_checked = bool(original_search.get("checked")
                                and original_search.get("keyword") == original_keyword
                                and original_search.get("search_imdbid") == bool(getattr(subscribe, "search_imdbid", False))
                                and original_search.get("custom_words") == (getattr(subscribe, "custom_words", None) or None))
        original_ids = set(original_search.get("resource_ids") or []) if original_checked else set()
        original_matched = bool(candidate and candidate.get("id") in original_ids)
        site_failed = any((candidate or {}).get("site_id") is None
                          or item.get("site_id") == candidate.get("site_id")
                          for item in original_search.get("site_failures") or [])
        original_completed = original_checked and not site_failed
        search_keyword_changed = bool(candidate and not original_matched and candidate_keywords and original_completed)
        search_keyword = candidate_keywords[0] if search_keyword_changed else original_keyword
        search_keyword_verified = original_matched or search_keyword_changed
        search_keyword_notice = ""
        if candidate and original_matched:
            search_keyword_notice = "原订阅搜索已召回所选版本，确认后保留原搜索词和搜索方式；当前按原订阅搜索结果预览。"
        elif search_keyword_changed:
            search_keyword_notice = "原订阅搜索正常完成但未召回所选版本，带季查询已召回；确认后才改用该带季搜索词。"
            if getattr(subscribe, "search_imdbid", False):
                search_keyword_notice += "同时从 IMDb ID 搜索改为已验证的标题搜索。"
        elif candidate and original_completed:
            search_keyword_notice = "本轮未召回所选版本，不会修改原订阅搜索词。"
        elif candidate:
            search_keyword_notice = "未能验证当前原订阅搜索（缓存缺少记录或站点失败），不能判断必须加季号；本次保留原搜索词，请重新搜索完成验证。"
        filtering_started = time.perf_counter()
        filter_params = SubscribeChain.get_params(working)
        groups_disabled = rules.get("filter_groups") == [] \
            or getattr(working, "filter_groups", None) == [UNFILTERED_GROUP_NAME]
        policy = preview_group_policy(working, disabled=groups_disabled)
        summary = rule_summary(filter_params, getattr(working, "sites", None) or [], candidate,
                               working=working, rules=rules, group_policy=policy)
        directory_seconds = 0.0
        now = _utc_now()
        records = []
        for context in contexts:
            try:
                item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, now)
            except (TypeError, ValueError, OverflowError):
                logger.warning(f"订阅 {subscribe.id} 的一个规则预览资源格式异常，已跳过")
                continue
            if not item:
                continue
            if search_keyword_changed and search_keyword not in resource_keywords.get(item["id"], []):
                reason = "未由确认后使用的搜索词召回"
            elif candidate and not search_keyword_changed and original_completed and item["id"] not in original_ids:
                reason = "未被原订阅搜索策略召回"
            elif require_episodes and item["kind"] == "cross_season":
                reason = "跨季资源无法确定目标季集数，不能手动选集"
            elif require_episodes and not item["episodes"]:
                reason = "未明确识别集数，不能手动选集"
            else:
                try:
                    reason = self._rule_rejection(working, context, filter_params)
                except (TypeError, ValueError, AttributeError):
                    reason = "资源字段不完整，无法核对规则"
            records.append((context, item, reason))

        base_matches = [context for context, _item, reason in records if reason is None]
        group_failures = group_rejections(base_matches, policy)
        if preview_group_policy(working, disabled=groups_disabled) != policy:
            raise ValueError("预览期间规则组配置已变化，请重新预览")
        with_groups_count = len(base_matches) - len(group_failures)
        auto_disabled = bool(auto_disable_rule_groups and not groups_disabled and policy["names"]
                             and base_matches and with_groups_count == 0)
        notice = ""
        if auto_disabled:
            rules["filter_groups"] = []
            working.filter_groups = []
            groups_disabled = True
            origin = "原订阅规则组" if policy["source"] == "原订阅" else "系统默认规则组"
            notice = (f"{origin}（{'、'.join(policy['names'])}）过滤了全部 {len(base_matches)} 条可匹配资源；"
                      "已自动选择“不使用规则组”，当前展示跳过规则组的预览。确认后才会修改订阅的规则组设置。")
            summary = rule_summary(filter_params, getattr(working, "sites", None) or [], candidate,
                                   working=working, rules=rules, group_policy=policy)
        if candidate:
            summary.append({"field": "keyword", "label": "搜索词", "value": search_keyword or "默认使用剧名及别名",
                            "source": "仅带季查询命中，确认后修改" if search_keyword_changed else "保留原订阅"})
        matched: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for context, item, reason in records:
            if reason is None and not groups_disabled:
                reason = group_failures.get(id(context))
            if reason:
                item["reason"] = readable_rejection(reason, summary)
                rejected.append(item)
            else:
                directory_started = time.perf_counter()
                try:
                    item["download_path"] = preview_download_path(
                        getattr(context, "media_info", None),
                        download_directory or getattr(subscribe, "save_path", None),
                    )
                except ValueError as error:
                    item["download_path_error"] = str(error)
                finally:
                    directory_seconds += time.perf_counter() - directory_started
                matched.append(item)
        priority = {"full_season": 4, "cross_season": 3, "suspected_full": 2,
                    "first_episode": 1, "episode": 0}
        matched.sort(key=lambda item: priority.get(item["kind"], 0), reverse=True)
        rejected.sort(key=lambda item: priority.get(item["kind"], 0), reverse=True)
        timings.update({
            "filter_seconds": max(0.0, time.perf_counter() - filtering_started - directory_seconds),
            "directory_seconds": directory_seconds,
            "total_seconds": time.perf_counter() - started,
        })
        reason_counts = Counter(item["reason"] for item in rejected)
        logger.info(
            f"订阅 {subscribe.id} 规则预览：{'复用搜索' if cached else '重新搜索'}，"
            f"媒体识别 {timings['recognition_seconds']:.2f}s，搜索与解析 {timings['search_seconds']:.2f}s，"
            f"规则筛选 {timings['filter_seconds']:.2f}s，目录计算 {directory_seconds:.2f}s，"
            f"通过 {len(matched)} 条，排除 {len(rejected)} 条"
        )
        return {
            "matched": matched[:80], "rejected": rejected[:40],
            "matched_count": len(matched), "rejected_count": len(rejected),
            "searched_at": now, "searched_ts": datetime.now(timezone.utc).timestamp(),
            "search_cached": cached, "search_age_seconds": round(cache_age, 1),
            "search_result_count": len(contexts),
            "search_trigger": "cache" if cached else "manual" if refresh_search else "cache_miss",
            "rule_groups_checked": True, "rule_groups_disabled": groups_disabled,
            "rule_groups_auto_disabled": auto_disabled, "rule_group_notice": notice,
            "rule_group_comparison": {
                "names": policy["names"], "source": policy["source"],
                "with_groups_count": with_groups_count, "without_groups_count": len(base_matches),
            } if policy["names"] else None,
            "search_source": source, "search_result_at": result_at, "search_sites": search_sites,
            "search_diagnostics": _search_diagnostics_snapshot(diagnostics),
            "search_keyword": search_keyword, "original_search_keyword": getattr(subscribe, "keyword", None),
            "search_keyword_notice": search_keyword_notice,
            "search_keyword_changed": search_keyword_changed, "search_keyword_verified": search_keyword_verified,
            "timings": {key: round(value, 3) for key, value in timings.items()},
            "effective_rules": summary,
            "rejection_summary": [{"reason": reason, "count": count}
                                  for reason, count in reason_counts.most_common()],
        }

    def preview_candidate(
        self, choice: str, rule_json: str = "", rule_mode: str = "candidate",
        download_directory: str = "",
        *, refresh_search: bool = False,
    ) -> tuple[bool, str, str]:
        """
        生成候选规格的只读匹配预览，保持订阅暂停和原规则不变。

        :param choice: 订阅 ID 与候选 ID
        :param rule_json: 用户调整的草稿规则 JSON
        :param rule_mode: 选择所选站点或所有站点
        :param download_directory: 从目录卡片选择的资源根目录；留空保留订阅原目录
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
                rules = _draft_rules(candidate, rule_json, rule_mode)
                if download_directory:
                    selected_directory_root(download_directory)
            except ValueError as error:
                return False, str(error), rule_json
            scope = _rule_scope(subscribe)
        try:
            preview = self._search_rule_preview(
                subscribe, rules, download_directory=download_directory,
                refresh_search=refresh_search, candidate=candidate,
                auto_disable_rule_groups=True,
            )
        except ValueError as error:
            logger.exception(f"预览订阅 {sid} 的规则匹配失败")
            return False, str(error), rule_json
        except Exception:
            logger.exception(f"预览订阅 {sid} 的规则匹配失败")
            return False, "搜索失败，请查看插件日志后重试", rule_json
        with self._lock:
            current = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            if not current or entry.get("status") != "pending" or _rule_scope(current) != scope:
                return False, "预览期间订阅发生变化，请重新预览", rule_json
            normalized_json = json.dumps(editor_draft_rules(candidate, rules, rule_json), ensure_ascii=False, indent=2)
            preview.update({"choice": choice, "rules": rules, "scope": scope, "rule_mode": rule_mode,
                            "draft_json": normalized_json})
            preview["download_directory"] = download_directory
            entry["preview"] = preview
            self._save_state()
        selected = next((item for item in preview["matched"] if item["id"] == candidate_id), {})
        path = selected.get("download_path") or selected.get("download_path_error")
        destination = f"；预计下载目录：{path}" if path else ""
        notice = f"；{preview['rule_group_notice']}" if preview.get("rule_group_notice") else ""
        return True, f"当前匹配 {preview['matched_count']} 条，排除 {preview['rejected_count']} 条；订阅仍保持待确认{destination}{notice}", normalized_json

    def confirm_candidate(
        self, choice: str, rule_json: Optional[str] = None, rule_mode: str = "candidate",
        download_directory: str = "",
    ) -> tuple[bool, str]:
        """
        回填所选版本的规则，再恢复订阅并安排补搜。

        :param choice: 配置页传入的“订阅 ID:候选 ID”
        :param rule_json: 用户当前提交的草稿规则；变更后必须重新预览
        :param rule_mode: 必须与本次预览使用的站点范围一致
        :param download_directory: 必须与预览时选择的资源根目录一致
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
            if preview.get("download_directory", "") != download_directory:
                return False, "下载目录已修改，请重新预览后确认"
            if download_directory:
                try:
                    selected_directory_root(download_directory)
                except ValueError as error:
                    return False, str(error)
            if preview.get("choice") != choice or preview.get("scope") != _rule_scope(subscribe) \
                    or preview.get("rule_groups_checked") is not True \
                    or preview.get("rule_mode", "candidate") != rule_mode \
                    or not isinstance(preview.get("searched_ts"), (int, float)) \
                    or datetime.now(timezone.utc).timestamp() - preview["searched_ts"] > 1800:
                return False, "请先为当前候选生成不超过 30 分钟的匹配预览"
            rules = preview.get("rules") or {}
            if rule_json is not None:
                try:
                    if _draft_rules(candidate, rule_json, rule_mode) != rules:
                        return False, "规则已修改，请重新预览后确认"
                except ValueError as error:
                    return False, str(error)
            if not preview.get("matched_count") or candidate_id not in {
                item.get("id") for item in preview.get("matched") or []
            }:
                return False, "所选版本未通过当前规则，请修改规则并重新预览"
            if "search_keyword_changed" not in preview:
                return False, "请重新预览，确认是否需要调整原订阅搜索词"
            if preview["search_keyword_changed"] and (not preview.get("search_keyword") or not preview.get("search_keyword_verified")):
                return False, "请先重新搜索并预览，以验证带季搜索词"
            if candidate.get("kind") == "full_season" and _full_season_version_count(
                preview.get("matched") or []
            ) > 1:
                return False, "规则仍匹配多个全集版本，请缩小规则后重新预览"
            payload = {field: value for field, value in rules.items() if field != "sites" or value is not None}
            if preview["search_keyword_changed"]:
                payload["keyword"] = preview["search_keyword"]
                if getattr(subscribe, "search_imdbid", False):
                    payload["search_imdbid"] = False
            if download_directory:
                payload["save_path"] = download_directory
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

        :param sid: 订阅 ID
        :param entry: 待确认的插件状态
        :param payload: 要回填的规则及保存目录；空值保留订阅参数
        :param candidate_id: 确认的候选 ID；空值表示直接批准
        :return: 是否批准成功及结果消息
        """
        previous = copy.deepcopy(entry)
        if payload is not None and payload.get("filter_groups") == []:
            try:
                payload = {**payload, "filter_groups": [ensure_unfiltered_group()]}
            except Exception as error:
                logger.exception(f"订阅 {sid} 无法设置不使用规则组")
                detail = str(error) if isinstance(error, ValueError) else "请查看插件日志"
                return False, f"不使用规则组设置失败：{detail}；订阅仍保持待确认"
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
        with self._lock:
            self._preview_search_cache = {
                key: cached for key, cached in self._preview_search_cache.items()
                if key[0] != entry.get("identity")
            }
        if resume_state != "S":
            self._schedule_approved_search(sid)
        else:
            return True, f"订阅 {sid} 已批准；原本由用户暂停，仍保持暂停"
        if candidate_id:
            search_note = "已改用验证通过的带季搜索词" if payload and "keyword" in payload else "保留原搜索词"
            return True, f"订阅 {sid} 已回填规则并恢复，{search_note}，将补齐缺集后继续追更"
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

    def preview_replacements(self, sid_value: Any, *, refresh_search: bool = False) -> tuple[bool, str]:
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
            preview = self._search_rule_preview(subscribe, {}, require_episodes=True, refresh_search=refresh_search)
        except ValueError as error:
            logger.exception(f"预览订阅 {sid} 的回刷资源失败")
            return False, str(error)
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
                    or preview.get("rule_groups_checked") is not True \
                    or datetime.now(timezone.utc).timestamp() - preview["searched_ts"] > 1800:
                return False, "当前规则或订阅已变化，请在 30 分钟内重新预览"
            if resource_id not in {item.get("id") for item in preview.get("matched") or []}:
                return False, "资源不在当前规则的预览匹配列表中"
        try:
            contexts = self._search_unfiltered(subscribe)
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
        if len(target_seasons(context.meta_info, subscribe.season or 1)) != 1:
            return False, "跨季资源无法确定目标季集数，未开始下载"
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
            if reason is None:
                try:
                    reason = group_rejections([context], preview_group_policy(current)).get(id(context))
                except ValueError as error:
                    return False, f"{error}；未开始下载"
            if reason:
                return False, f"资源已不匹配当前规则：{reason}；未开始下载"
            if _rule_scope(current) != scope:
                return False, "提交前规则组配置已变化，未开始下载"
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

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        """用插件自己的配置组件展示操作结果并刷新列表。"""
        return "vue", "dist/assets"

    def get_api(self) -> list[dict[str, Any]]:
        """配置组件通过受管理员权限保护的接口提交当前表单。"""
        from fastapi import Depends
        from app.api.dependencies.auth import get_current_active_superuser

        dependencies = [Depends(get_current_active_superuser)]
        return [{
            "path": "/apply", "endpoint": self.apply_config, "methods": ["POST"],
            "auth": "bear", "dependencies": dependencies,
            "summary": "保存剧集守门员设置并执行所选操作",
        }, {
            "path": "/preview-status", "endpoint": self.preview_search_status, "methods": ["POST"],
            "auth": "bear", "dependencies": dependencies,
            "summary": "检查预览搜索缓存，不保存设置或触发搜索",
        }]

    def preview_search_status(self, config: dict[str, Any]) -> dict[str, Any]:
        """搜索前告知组件本次会复用缓存还是重新搜索；不修改任何记录。"""
        try:
            is_candidate = config.get("operation") == "preview"
            if not is_candidate and config.get("operation") != "preview_replace":
                return {"success": False, "message": "请选择预览操作"}
            if is_candidate:
                sid_text, candidate_id = str(config.get("candidate") or "").split(":", 1)
                sid = int(sid_text)
            else:
                sid = int(config.get("replace_subscription") or "")
            with self._lock:
                subscribe = self._subscribes.get(sid)
                entry = self._state["entries"].get(str(sid)) or {}
                if not subscribe or entry.get("identity") != _identity(subscribe):
                    return {"success": False, "message": "订阅身份已变化，请重新扫描"}
                if is_candidate:
                    candidate = next((item for item in entry.get("candidates") or []
                                      if item.get("id") == candidate_id), None)
                    if entry.get("status") != "pending" or not candidate:
                        return {"success": False, "message": "候选或订阅已变化，请重新扫描"}
                    rules = _draft_rules(candidate, str(config.get("rule_json") or ""),
                                         str(config.get("rule_mode") or "candidate"))
                else:
                    if not self._is_approved(subscribe):
                        return {"success": False, "message": "订阅未批准或身份已变化"}
                    rules = {}
                sites = rules.get("sites")
                if sites is None:
                    sites = getattr(subscribe, "sites", None) or []
                cached = self._cached_search(subscribe, sites)
                status = "manual" if config.get("refresh_search") else (
                    "cache_miss" if cached is None else "cached" if cached["contexts"] else "cached_empty"
                )
                paused = [pause for sid, pause in (self._state.get("site_holds") or {}).items()
                          if pause.get("until", 0) > time.time() and (not sites or sid in {str(site) for site in sites})]
                if status == "cached_empty" and paused:
                    status = "cached_paused"
                return {"success": True, "status": status, "search_sites": sites,
                        "search_result_count": len(cached["contexts"]) if cached is not None else None}
        except (ValueError, TypeError) as error:
            return {"success": False, "message": str(error) or "预览选项无效"}

    def apply_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """沿用宿主配置更新流程，并把一次性操作的实际结果返回给组件。"""
        from app.api.dependencies.plugin import get_plugin_config_command

        result = get_plugin_config_command().update(self.__class__.__name__, config)
        if not result.success:
            return {"success": False, "message": result.message or "配置保存失败"}
        if config.get("operation") in (None, "", "save"):
            return {"success": True, "message": "设置已保存"}
        with self._lock:
            outcome = copy.deepcopy(self._state.get("last_confirmation") or {})
        if outcome.get("request_id") != str(config.get("operation_request_id") or ""):
            return {"success": False, "message": "操作已提交，但未能确认结果，请查看插件详情页"}
        response = {
            "success": bool(outcome.get("success")),
            "message": outcome.get("message") or "操作未返回结果",
        }
        if response["success"] and config.get("operation") in {"preview", "preview_replace"}:
            is_candidate = config["operation"] == "preview"
            sid = str(config.get("candidate") or "").split(":", 1)[0] if is_candidate \
                else str(config.get("replace_subscription") or "")
            with self._lock:
                response["preview"] = _config_preview(
                    sid, self._state["entries"].get(sid) or {},
                    "preview" if is_candidate else "replacement_preview",
                )
        return response

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        返回全集确认策略与互斥的历史接管、批准和撤销操作。
        """
        with self._lock:
            subscriptions = self._subscribes.list()
            current = {str(item.id): item for item in subscriptions}
            last_confirmation = copy.deepcopy(self._state.get("last_confirmation"))
            previews = {}
            for field in ("preview", "replacement_preview"):
                latest = max(
                    ((sid, entry) for sid, entry in self._state["entries"].items() if entry.get(field)),
                    key=lambda row: row[1][field].get("searched_ts") or 0, default=None,
                )
                previews[field] = _config_preview(*latest, field) if latest else None
            # 首次启用前还没有水位；先展示现有订阅，保存启用配置时再冻结水位。
            historical = [
                (item, _subscription_progress(item)[0])
                for item in subscriptions
                if (not self._enabled or item.id <= (self._state.get("baseline") or 0))
                and item.type == MediaType.TV.value
                and item.state in {"N", "R", "P", "S"}
                and str(item.id) not in self._state["entries"]
            ]
            historical.sort(key=lambda row: (
                row[1] is None, row[1] if row[1] is not None else 0, row[0].id,
            ))
            historical_choices = [
                {
                    "title": f"#{item.id} {item.name} S{int(item.season or 1):02d} · "
                             f"{_progress_label(item)} · "
                             f"{'已暂停' if item.state == 'S' else '订阅中'}",
                    "value": item.id,
                }
                for item, _completed in historical
            ]
            managed_names = [
                f"#{sid} {item.name} S{int(item.season or 1):02d}"
                f"（{'待确认' if entry['status'] == 'pending' else '已批准'}）"
                for sid, entry in self._state["entries"].items()
                if isinstance(entry, dict) and entry.get("status") in {"pending", "approved"}
                and (item := current.get(sid)) is not None
                and item.type == MediaType.TV.value
                and item.state in {"N", "R", "P", "S"}
                and entry.get("identity") == _identity(item)
            ]
            managed_summary = (
                f"已纳入管理 {len(managed_names)} 条："
                + "；".join(managed_names[:8])
                + ("；其余请在插件详情页查看" if len(managed_names) > 8 else "")
                if managed_names else "尚无已纳入管理的剧集订阅。"
            )
            candidate_entries = [
                (sid, entry)
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "pending" and entry.get("candidates")
            ]
            candidate_subscription_choices = [
                {
                    "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}",
                    "value": sid,
                }
                for sid, entry in candidate_entries
            ]
            choices = [
                {
                    "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                             f"{_candidate_label(item)} {_candidate_coverage(item)} · "
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · "
                             f"{_size_label(item.get('size_bytes'), exact=True)} · "
                             f"{item.get('quality') or '?'} {item.get('resolution') or '?'} "
                             f"{item.get('team') or ''} · {item.get('title')}",
                    "value": f"{sid}:{item['id']}",
                    "subscription_id": sid,
                    "rule_names": candidate_rule_names(item),
                    "rule_patterns": item.get("rules") or {},
                    "rules": display_draft_rules(item, {
                        key: value for key, value in candidate_default_rules(item).items()
                        if key in {"quality", "resolution", "effect", "include", "exclude", "sites"}
                        and value is not None
                    }),
                    "page_url": public_page_url(item.get("page_url")),
                    "site_name": item.get("site_name") or str(item.get("site_id") or "未知站点"),
                    "site_id": item.get("site_id"),
                    "original_sites": getattr(current.get(sid), "sites", None) or [],
                    "original_filter_groups": getattr(current.get(sid), "filter_groups", None) or [],
                    "team": item.get("release_team", item.get("team")), "team_pattern": candidate_team_pattern(item),
                }
                for sid, entry in candidate_entries
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
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · "
                             f"{_size_label(item.get('size_bytes'), exact=True)} · {item.get('title')}",
                    "value": f"{sid}:{item['id']}",
                }
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "approved"
                for item in (entry.get("replacement_preview") or {}).get("matched") or []
            ]
        def field(component: str, props: dict[str, Any]) -> dict[str, Any]:
            return {"component": component, "props": {
                "density": "compact", "hide-details": True, **props,
            }}

        def switch(model: str, label: str) -> dict[str, Any]:
            return field("VSwitch", {"model": model, "label": label})

        def select(model: str, label: str, items: list[dict[str, Any]],
                   **props: Any) -> dict[str, Any]:
            return field("VSelect", {
                "model": model, "label": label, "items": items,
                "item-title": "title", "item-value": "value", **props,
            })

        def col(component: dict[str, Any], *, cols: int = 12,
                sm: Optional[int] = None, md: Optional[int] = None) -> dict[str, Any]:
            grid: dict[str, Any] = {"cols": cols}
            if sm is not None:
                grid["sm"] = sm
            if md is not None:
                grid["md"] = md
            return {"component": "VCol", "props": grid, "content": [component]}

        def row(*columns: dict[str, Any]) -> dict[str, Any]:
            return {"component": "VRow", "content": list(columns)}

        def info(message: str) -> dict[str, Any]:
            return {"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "density": "compact", "text": message,
            }}

        divider = {"component": "VDivider", "props": {"class": "my-3"}}
        return [{"component": "VForm", "content": [
            row(
                col(switch("enabled", "接管新增剧集订阅"), sm=6),
                col(switch("confirm_full_pack", "全集种子也需确认"), sm=6),
                col(switch("daily_notice", "每天推送待确认订阅汇总"), sm=6),
                col(select("notice_hour", "通知小时", [
                    {"title": f"{hour:02d}", "value": hour} for hour in range(24)
                ]), cols=6, sm=3),
                col(select("notice_minute", "通知分钟", [
                    {"title": f"{minute:02d}", "value": minute} for minute in range(60)
                ]), cols=6, sm=3),
            ),
            row(
                col(info(
                    "开启全集确认后，新剧集暂停；关闭后，分集下载仍会拦截，"
                    "仅覆盖整季且符合原规则的种子可自动下载。撤销批准后始终暂停。"
                    "目标季候选每 4 小时更新。"
                ), md=6),
                col(info(managed_summary), md=6),
            ),
            divider,
            row(
                col(select("historical_subscription", "纳入管理的历史剧集（可多选，按进度排序）",
                           historical_choices, clearable=True, multiple=True, chips=True,
                           **{"closable-chips": True,
                              "no-data-text": "暂无可纳入管理的历史剧集订阅"})),
            ),
            divider,
            row(
                col(select("candidate_subscription", "选择有版本的剧集",
                           candidate_subscription_choices, clearable=True,
                           **{"no-data-text": "暂无有版本的待确认剧集"}), md=4),
                col(select("candidate", "选择该剧集版本（全集优先）", choices, clearable=True), md=5),
                col(select("rule_mode", "站点范围", [
                    {"title": "只用所选站点", "value": "candidate"},
                    {"title": "不限站点", "value": "all_sites"},
                ]), md=3),
            ),
            row(
                col(select("download_directory", "下载目录（留空保留原目录）", directory_choices(),
                           clearable=True, **{"hide-details": False,
                           "hint": "确认只回填资源根目录；完整分类路径在预览中显示，实际下载由 MoviePilot 处理",
                           "persistent-hint": True})),
            ),
            divider,
            row(
                col(select("direct_subscription", "直接批准的待确认订阅", pending_choices,
                           clearable=True), md=6),
                col(select("revoke_subscription", "撤销批准的订阅", approved_choices,
                           clearable=True), md=6),
            ),
            divider,
            row(col(info(
                "回刷已入库集数：先预览已批准订阅的当前规则，再选择资源和集数提交。"
                "插件不删除旧文件，最终替换由 MoviePilot 整理流程决定。"
            ))),
            row(
                col(select("replace_subscription", "选择要预览回刷资源的已批准订阅",
                           approved_choices, clearable=True)),
                col(select("replace_resource", "选择预览通过的回刷资源",
                           replacement_choices, clearable=True), md=8),
                col(field("VTextField", {
                    "model": "replace_episodes", "label": "重新下载集数（如 1,3-5）",
                }), md=4),
            ),
            divider,
            row(
                col(select("operation", "本次保存执行", [
                    {"title": "只保存配置", "value": "save"},
                    {"title": "纳入所选历史剧集", "value": "adopt"},
                    {"title": "预览候选规则", "value": "preview"},
                    {"title": "确认候选规则并恢复订阅", "value": "confirm"},
                    {"title": "直接批准订阅（保留原规则）", "value": "direct_approve"},
                    {"title": "撤销批准并暂停订阅", "value": "revoke"},
                    {"title": "预览可回刷资源", "value": "preview_replace"},
                    {"title": "提交所选资源和集数重新下载", "value": "submit_replace"},
                ])),
            ),
        ]}], {
            "enabled": False, "confirm_full_pack": True, "daily_notice": True,
            "notice_hour": 9, "notice_minute": 0,
            "historical_subscription": [], "candidate_subscription": "", "candidate": "",
            "rule_mode": "candidate", "rule_json": "",
            "download_directory": "",
            "direct_subscription": "", "revoke_subscription": "",
            "replace_subscription": "", "replace_resource": "", "replace_episodes": "",
            "operation": "save", "operation_request_id": "",
            "_last_confirmation": last_confirmation,
            "_candidate_preview": previews["preview"],
            "_replacement_preview": previews["replacement_preview"],
            "_rule_options": SUBSCRIPTION_RULE_OPTIONS,
        }

    def get_page(self) -> list[dict[str, Any]]:
        """
        展示待确认和已批准订阅、候选规格及当前管理状态。
        """
        with self._lock:
            entries = copy.deepcopy(list(self._state["entries"].items()))
        current = {str(item.id): item for item in self._subscribes.list()}
        active_entries = [
            (sid, entry, subscribe)
            for sid, entry in entries
            if isinstance(entry, dict)
            and entry.get("status") in {"pending", "approved"}
            and (subscribe := current.get(sid)) is not None
            and subscribe.type == MediaType.TV.value
            and subscribe.state in {"N", "R", "P", "S"}
            and entry.get("identity") == _identity(subscribe)
        ]
        pending = [(sid, entry) for sid, entry, _subscribe in active_entries if entry["status"] == "pending"]
        approved = [(sid, entry) for sid, entry, _subscribe in active_entries if entry["status"] == "approved"]
        page: list[dict[str, Any]] = []
        managed_rows = []
        for sid, entry, subscribe in active_entries:
            managed_rows.append({"component": "tr", "content": [
                {"component": "td", "text": f"#{sid}"},
                {"component": "td", "text": f"{subscribe.name} S{int(subscribe.season or 1):02d}"},
                {"component": "td", "text": "手动纳入" if entry.get("source") == "historical" else "新增接管"},
                {"component": "td", "text": "待确认" if entry.get("status") == "pending" else "已批准"},
                {"component": "td", "text": _progress_label(subscribe)},
            ]})
        page.append({"component": "VAlert", "props": {
            "type": "info", "variant": "tonal",
            "text": (f"当前管理 {len(managed_rows)} 条剧集订阅；下表显示当前状态和订阅进度。"
                     if managed_rows else "当前没有纳入管理的剧集订阅。"),
        }})
        if managed_rows:
            page.append({"component": "VTable", "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("订阅 ID", "剧集", "接管方式", "确认状态", "订阅进度")
                ]}]},
                {"component": "tbody", "content": managed_rows},
            ]})
        if not pending:
            page.append({"component": "VAlert", "props": {
                "type": "info", "text": "暂无待确认剧集。新增或手动纳入电视剧订阅后会收集首集、整季及含目标季的跨季候选。",
            }})
        for sid, entry in pending:
            groups = _display_groups(entry.get("candidates") or [])
            page.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"候选 {len(entry.get('candidates') or [])} 条、合并展示 {len(groups)} 行（含历史扫描记录） · "
                        f"最近扫描 {entry.get('last_scan') or '等待中'}"
                        + (f" · 本轮发现候选 {entry['last_scan_count']} 条" if "last_scan_count" in entry else "")
                        + (" · 已撤销，等待重新批准" if entry.get("revoked") else "")
                        + (f" · 扫描错误：{entry['last_error']}" if entry.get("last_error") else ""),
            }})
            failures = (entry.get("search_diagnostics") or {}).get("site_failures") or []
            if failures:
                page.append({"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "最近扫描有失败或暂停的站点，结果可能不完整："
                            + "；".join(f"{item['site_name']}：{item['reason']}" for item in failures),
                }})
            rows = [
                {"component": "tr", "content": [
                    {"component": "td", "text": _candidate_label(item)},
                    {"component": "td", "text": _candidate_coverage(item)},
                    _site_cell(group),
                    {"component": "td", "text": item.get("title") or ""},
                    _size_cell(item),
                    {"component": "td", "text": item.get("quality") or "?"},
                    {"component": "td", "text": item.get("resolution") or "?"},
                    {"component": "td", "text": item.get("effect") or "?"},
                    {"component": "td", "text": item.get("team") or "?"},
                    {"component": "td", "text": max((member.get("last_seen") or "" for member in group), default="")},
                    _candidate_action_cell(sid, group),
                ]}
                for group in groups
                for item in group[:1]
            ]
            page.append({"component": "VTable", "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("类型", "识别集号/目标总集数", "站点/详情", "资源", "大小", "质量", "分辨率", "特效", "制作组", "最后发现", "配置")
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
                if preview.get("rule_group_notice"):
                    page.append({"component": "VAlert", "props": {
                        "type": "warning", "variant": "tonal", "text": preview["rule_group_notice"],
                    }})
                if preview.get("search_keyword_notice"):
                    page.append({"component": "VAlert", "props": {
                        "type": "info" if preview.get("search_keyword_verified") else "warning", "variant": "tonal",
                        "text": f"确认后的订阅搜索词：{preview.get('search_keyword') or '默认使用剧名及别名'}。"
                                + preview["search_keyword_notice"],
                    }})
                page.append(_preview_table(preview))
                if _full_season_version_count(preview.get("matched") or []) > 1:
                    page.append({"component": "VAlert", "props": {
                        "type": "warning", "text": "当前规则匹配多个集数覆盖全集的版本；若选择全集候选，请收窄规则并重新预览。",
                    }})
        if approved:
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": "已批准订阅可在配置页撤销批准；规则会保留，订阅会暂停。"
                        "候选和确认预览保留 24 小时，期满后由下一次扫描清理，批准状态继续有效。",
            }})
        for sid, entry in approved:
            mode = "候选规则回填" if entry.get("mode") == "candidate" else "直接批准"
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"{mode} · 批准于 {entry.get('approved_at') or '?'}",
            }})
            confirmed_preview = entry.get("preview") or {}
            if confirmed_preview:
                page.append({"component": "VAlert", "props": {
                    "type": "info", "variant": "tonal",
                    "text": f"确认时的规则预览（历史记录）：{confirmed_preview.get('searched_at') or '?'} · "
                            f"通过 {confirmed_preview.get('matched_count', 0)} 条，"
                            f"排除 {confirmed_preview.get('rejected_count', 0)} 条；"
                            "完整搜索缓存已释放，以下记录不代表当前资源仍然存在。",
                }})
                page.append(_preview_table(confirmed_preview))
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
            mtype=MessageType.Plugin,
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
        with self._lock:
            self._preview_search_cache.clear()
            self._queued_scan_ids.clear()
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

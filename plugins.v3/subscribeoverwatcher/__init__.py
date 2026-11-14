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
from app.application.site.sites import SitesHelper
from app.application.configuration import get_configured_system_config
from app.application.subscription.contract import build_subscribe_meta
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.types import ChainEventType, EventType, MediaSource, MediaType, MessageType, SystemConfigKey
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .directory import directory_choices, preview_download_path, selected_directory_root
from .cache import MAX_CACHE_ENTRIES, SearchCacheStore
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
from .search import CandidateSearchChain, search_names


def _utc_now() -> str:
    """
    返回适合排序和展示的 UTC 时间。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _search_diagnostics_snapshot(diagnostics: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """每条资源的查询词只随搜索缓存保存，不扩大候选和预览展示记录。"""
    if diagnostics is None:
        return None
    snapshot = {key: value for key, value in diagnostics.items() if key != "resource_keywords"}
    if snapshot.get("original_search"):
        snapshot["original_search"] = {key: value for key, value in snapshot["original_search"].items()
                                       if key not in {"resource_ids", "custom_words", "site_failures"}}
    return snapshot


def _search_site_scope(sites: list[int]) -> tuple[list[int], dict[int, str]]:
    """与宿主搜索链使用同一站点目录和默认站点配置。"""
    indexers = SitesHelper().get_indexers() or []
    names = {site["id"]: str(site.get("name") or f"站点 {site['id']}")
             for site in indexers if isinstance(site, dict)
             and isinstance(site.get("id"), int) and not isinstance(site["id"], bool)}
    if sites:
        requested = set(sites)
        if not requested.issubset(names):
            raise ValueError("所选站点已不可用，请重新选择站点")
    else:
        configured = get_configured_system_config().get(SystemConfigKey.IndexerSites) or []
        requested = set(configured) if configured else set(names)
    return sorted(requested & names.keys()), names


def _site_labels(site_ids: set[int] | list[int], names: dict[int, str]) -> list[str]:
    return [f"{names.get(site_id, '站点')}（{site_id}）" for site_id in sorted(site_ids)]


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


def _library_scope(subscribe: Any) -> str:
    """已齐集订阅的目标范围；扩大范围后允许再次接管。"""
    return json.dumps([
        subscribe.season, subscribe.start_episode or 1, subscribe.total_episode,
        bool(getattr(subscribe, "manual_total_episode", False)), bool(subscribe.best_version),
    ], separators=(",", ":"))


def _resource_key(context: Any) -> tuple[Any, ...]:
    """同一站点的详情页标识一个种子；缺少详情页时退回标题和大小。"""
    torrent = getattr(context, "torrent_info", None)
    if torrent is None:
        return ("unknown", id(context))
    site = getattr(torrent, "site", None)
    page = public_page_url(getattr(torrent, "page_url", None), getattr(torrent, "enclosure", None))
    if page:
        return ("page", site, page)
    title = " ".join(str(getattr(torrent, "title", None) or "").split()).casefold()
    if title:
        return ("title", site, title, getattr(torrent, "size", None))
    return ("unknown", id(context))


def _merge_resource_info(primary: Any, supplemental: Any) -> Any:
    """同一种子补充副标题与标签，保留主记录的标题和季集解析，不修改搜索原对象。"""
    torrent = getattr(primary, "torrent_info", None)
    other = getattr(supplemental, "torrent_info", None)
    if torrent is None or other is None:
        return primary
    descriptions = list(dict.fromkeys(
        line for value in (getattr(torrent, "description", None), getattr(other, "description", None))
        if isinstance(value, str) for line in value.splitlines() if line.strip()
    ))
    description = "\n".join(descriptions) if descriptions else getattr(torrent, "description", None)
    labels = []
    for value in (getattr(torrent, "labels", None), getattr(other, "labels", None)):
        items = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple, set)) else []
        labels.extend(label for label in items if isinstance(label, str) and label and label not in labels)
    if description == getattr(torrent, "description", None) and labels == (getattr(torrent, "labels", None) or []):
        return primary
    merged = copy.copy(primary)
    merged.torrent_info = copy.copy(torrent)
    merged.torrent_info.description = description
    merged.torrent_info.labels = labels
    return merged


def _distinct_contexts(contexts: list[Any]) -> list[Any]:
    """合并重复返回种子的副标题和标签，每个站点资源只保留一条记录。"""
    positions = {}
    unique = []
    for context in contexts:
        key = _resource_key(context)
        if key not in positions:
            positions[key] = len(unique)
            unique.append(context)
        else:
            position = positions[key]
            unique[position] = _merge_resource_info(unique[position], context)
    return unique


def _has_download_link(context: Any) -> bool:
    enclosure = getattr(getattr(context, "torrent_info", None), "enclosure", None)
    return isinstance(enclosure, str) and bool(enclosure.strip())


def _merge_history_contexts(previous: Optional[dict[str, Any]], current: list[Any]) -> list[Any]:
    """保留旧搜索资源；同一资源优先保留尚在内存中的真实下载地址。"""
    historical: dict[tuple[Any, ...], Any] = {}
    if previous:
        for context in [*(previous.get("historical_contexts") or []), *(previous.get("contexts") or [])]:
            key = _resource_key(context)
            if key not in historical or _has_download_link(context) or not _has_download_link(historical[key]):
                historical[key] = context
    for context in current:
        if _has_download_link(context):
            historical.pop(_resource_key(context), None)
    return list(historical.values())


def _keyword_selection(choice: str) -> str:
    """搜索方式只有原订阅和本轮验证过的带季词；旧的 auto 视为原订阅。"""
    if choice in ("", "auto", "original"):
        return "original"
    if choice == "seasonal":
        return "seasonal"
    raise ValueError("搜索词只能选择沿用原订阅或使用剧名＋季号搜索词")


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
        return "疑似整季（集数待核实）"
    if kind == "first_episode_bundle":
        return "含首集的多集资源"
    if kind == "episode":
        return "分集"
    return "单集 E01"


def _found_latest_scan(candidate: dict[str, Any], entry: dict[str, Any]) -> bool:
    marker = candidate.get("found_latest_scan")
    if isinstance(marker, bool):
        return marker
    return bool(entry.get("last_scan") and candidate.get("last_seen") == entry.get("last_scan"))


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
    """按所选站点范围生成草稿，其他候选规格照常回填。"""
    if mode not in {"candidate", "all_sites", "custom"}:
        raise ValueError("站点范围无效，请重新选择")
    rules = normalize_draft_rules(candidate_default_rules(candidate), raw, candidate_rule_names(candidate))
    if mode == "all_sites":
        rules["sites"] = []
    elif mode == "custom":
        if not rules.get("sites"):
            raise ValueError("自选站点至少需要选择一个站点")
        rules["sites"] = sorted(set(rules["sites"]))
    return rules


def _configured_draft_json(config: dict[str, Any]) -> str:
    """原生配置表单和自定义组件共用多选站点输入。"""
    raw = str(config.get("rule_json") or "")
    if config.get("rule_mode") != "custom" or "rule_sites" not in config:
        return raw
    sites = config["rule_sites"]
    if not isinstance(sites, list) or not sites or any(
        not isinstance(site, int) or isinstance(site, bool) or site <= 0 for site in sites
    ):
        raise ValueError("自选站点至少需要选择一个有效站点")
    try:
        rules = json.loads(raw) if raw.strip() else {}
    except (TypeError, ValueError) as error:
        raise ValueError("规则 JSON 格式错误") from error
    if not isinstance(rules, dict):
        raise ValueError("规则 JSON 格式错误")
    rules["sites"] = sites
    return json.dumps(rules, ensure_ascii=False)


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
    """按宿主订阅字段计算进度；总集数未知时不猜测。"""
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
    kind = "洗版完成" if getattr(subscribe, "best_version", False) else "订阅进度"
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
    """合并展示的候选按站点分别选中，并统一前往配置页操作。"""
    buttons = []
    for item in items:
        site = item.get("site_name") or str(item.get("site_id") or "未知站点")
        choice = f"{sid}:{item['id']}"
        buttons.append({"component": "VBtn", "props": {
            "variant": "tonal", "color": "primary", "size": "small", "class": "ma-1",
            "data-candidate-id": choice,
            "aria-label": f"选择{site}的版本并前往配置",
        }, "content": [{"component": "span", "text": f"配置·{site}"}]})
        if public_page_url(item.get("page_url")):
            buttons.append({"component": "VBtn", "props": {
                "variant": "outlined", "color": "warning", "size": "small", "class": "ma-1",
                "data-candidate-id": choice,
                "aria-label": f"选择{site}的版本并前往配置页下载",
            }, "content": [{"component": "span", "text": f"下载·{site}"}]})
    return {"component": "td", "content": buttons}


def _seeders_label(item: dict[str, Any]) -> str:
    seeders = item.get("seeders")
    return f"{seeders:,} 人" if isinstance(seeders, int) and not isinstance(seeders, bool) and seeders >= 0 else "未知"


def _availability_cell(items: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """合并版本仍按站点分别展示人数和促销，避免误用其他站点的免费状态。"""
    content = []
    for item in items:
        label = _seeders_label(item) if field == "seeders" else item.get("promotion") or "未知"
        if len(items) > 1:
            site = item.get("site_name") or str(item.get("site_id") or "未知站点")
            label = f"{site}：{label}"
        content.append({"component": "div", "text": label, "props": {"style": "white-space: nowrap"}})
    return {"component": "td", "content": content}


def _candidate_row_sort(items: list[dict[str, Any]]) -> dict[str, Any]:
    """给数据页的合并候选行提供可比较的展示字段。"""
    item = items[0]
    seeders = [value for member in items if isinstance(value := member.get("seeders"), int)
               and not isinstance(value, bool) and value >= 0]
    size = item.get("size_bytes")
    return {
        "last_seen": max((member.get("last_seen") or "" for member in items), default=""),
        "seeders": max(seeders) if seeders else None,
        "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
        "title": item.get("title") or "",
        "site": "、".join(sorted(str(member.get("site_name") or member.get("site_id") or "")
                               for member in items)),
    }


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
            _availability_cell(group, "seeders"),
            _availability_cell(group, "promotion"),
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
            for title in ("结果", "类型", "识别集号/目标总集数", "站点/详情", "资源", "大小", "做种人数", "免费/促销", "预计下载目录", "原因")
        ]}]},
        {"component": "tbody", "content": rows},
    ]}


def _config_preview(sid: str, entry: dict[str, Any], field: str) -> Optional[dict[str, Any]]:
    """向配置面板提供统计和展示表格，不重复传输完整资源列表。"""
    preview = entry.get(field)
    if not preview:
        return None
    snapshot = {
        **{key: preview.get(key) for key in (
            "choice", "rule_mode", "download_directory", "draft_json", "searched_at", "searched_ts",
            "matched_count", "rejected_count", "search_cached", "search_age_seconds", "timings",
            "search_result_count", "search_trigger", "rule_groups_disabled",
            "historical_candidates",
            "search_reused_sites", "search_searched_sites", "search_reused_site_labels",
            "search_searched_site_labels", "search_site_sources",
            "rule_groups_checked", "rule_groups_auto_disabled", "rule_group_notice", "rule_group_comparison",
            "effective_rules", "rejection_summary", "search_source", "search_result_at", "search_sites",
            "search_diagnostics",
            "search_keyword", "original_search_keyword", "search_keyword_notice", "keyword_choice",
            "search_keyword_changed", "search_keyword_verified", "keyword_choice_auto_changed",
        )},
        "subscription_id": sid, "table": _preview_table(preview),
        "matched_shown": len(preview.get("matched") or []),
        "rejected_shown": len(preview.get("rejected") or []),
    }
    # 旧预览可能把整数站点 ID 用作字典键；宿主表单接口只接受字符串 JSON 键。
    sources = snapshot.get("search_site_sources")
    if isinstance(sources, dict):
        snapshot["search_site_sources"] = {str(site_id): source for site_id, source in sources.items()}
    return snapshot


class SubscribeOverwatcher(_PluginBase):
    """
    管理新剧集订阅的下载确认和目标季版本候选。

    下载闸门读取持久确认记录；即使新增订阅广播晚到，也按创建前的 ID 水位拦截。
    允许全集直下时保留待确认订阅的搜索资格，只放行明确覆盖整季的下载事件。
    """

    plugin_name = "剧集守门员"
    plugin_desc = "剧集订阅可预览规则、一次性整包下载；订阅进度满后自动保留规则恢复。"
    plugin_icon = "icon.svg"
    plugin_version = "1.9.27"
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
        self._library_check_lock = threading.RLock()
        self._search_stop = threading.Event()
        self._queued_scan_ids: set[int] = set()
        self._queued_progress_ids: set[int] = set()
        # 搜索事实单独落盘，不把凭据或完整搜索结果放入候选和预览展示记录。
        self._preview_search_cache: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}
        self._search_cache_sequence = 0
        self._search_cache_store: Optional[SearchCacheStore] = None
        self._search_cache_loaded = False
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
        keyword_choice_saved = "keyword_choice" in config
        previous_keyword_choice = config.get("keyword_choice")
        if previous_keyword_choice in (None, "", "auto"):
            config["keyword_choice"] = "original"
        elif isinstance(previous_keyword_choice, str) and previous_keyword_choice.startswith("keyword:"):
            config["keyword_choice"] = "seasonal"
        keyword_choice_migrated = keyword_choice_saved and config["keyword_choice"] != previous_keyword_choice
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
            if "site_holds" in self._state:
                self._state.pop("site_holds", None)
                self._save_state()
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
                self._search_stop.set()
                if self._preview_search_cache:
                    logger.info(f"搜索缓存清理：插件停用，释放内存缓存 {len(self._preview_search_cache)} 组")
                self._preview_search_cache.clear()
                self._search_cache_loaded = False
                store = self._disk_search_cache()
                if store is not None:
                    try:
                        store.clear()
                    except OSError:
                        logger.warning("无法清除已停用插件的持久搜索缓存，请检查插件数据目录")
            elif self._search_stop.is_set():
                self._search_stop = threading.Event()
            if self._enabled and not self._search_cache_loaded:
                self._load_search_cache()
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
            "adopt", "preview", "confirm", "download_candidate", "direct_approve", "revoke",
            "preview_replace", "submit_replace",
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
                    "download_candidate": False, "direct_approve": False, "revoke": False,
                    "preview_replace": False, "submit_replace": False,
                    "historical_subscription": [], "scan_subscription": "", "candidate_subscription": "", "candidate": "",
                    "keyword_choice": "original",
                    "direct_subscription": "",
                    "download_directory": "",
                    "revoke_subscription": "", "rule_json": "", "replace_subscription": "",
                    "replace_resource": "", "replace_episodes": "", "operation_request_id": "",
                })
            elif historical_migrated or cache_setting_removed or keyword_choice_migrated:
                self.update_config(config)
            return
        normalized_rules = ""
        if actions:
            if len(actions) != 1:
                success, message = False, "一次只能提交一个纳入、扫描、预览、下载、批准、撤销或回刷操作"
            elif actions[0] == "adopt":
                success, message = self.adopt_subscriptions(config.get("historical_subscription") or [])
            elif actions[0] == "scan":
                success, message = self.scan_subscription(
                    config.get("scan_subscription"), selected_candidate=config.get("candidate"),
                )
            elif actions[0] == "preview":
                try:
                    draft_json = _configured_draft_json(config)
                except ValueError as error:
                    success, message = False, str(error)
                else:
                    success, message, normalized_rules = self.preview_candidate(
                        str(config.get("candidate") or ""), draft_json,
                        str(config.get("rule_mode") or "candidate"),
                        str(config.get("download_directory") or ""),
                        refresh_search=bool(config.get("refresh_search")),
                        keyword_choice=str(config.get("keyword_choice") or "original"),
                    )
            elif actions[0] == "confirm":
                try:
                    draft_json = _configured_draft_json(config)
                except ValueError as error:
                    success, message = False, str(error)
                else:
                    success, message = self.confirm_candidate(
                        str(config.get("candidate") or ""), draft_json,
                        str(config.get("rule_mode") or "candidate"),
                        str(config.get("download_directory") or ""),
                        keyword_choice=str(config.get("keyword_choice") or "original"),
                    )
            elif actions[0] == "download_candidate":
                success, message = self.download_candidate_once(
                    str(config.get("candidate") or ""),
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
                "download_candidate": False, "direct_approve": False, "revoke": False,
                "preview_replace": False, "submit_replace": False,
            }
            if actions == ["preview"] and success:
                updated_config["rule_json"] = normalized_rules
                sid = str(config.get("candidate") or "").split(":", 1)[0]
                with self._lock:
                    preview = (self._state["entries"].get(sid) or {}).get("preview") or {}
                    if preview.get("keyword_choice"):
                        updated_config["keyword_choice"] = preview["keyword_choice"]
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
                    updated_config["rule_sites"] = []
                    updated_config["keyword_choice"] = "original"
                    updated_config["rule_json"] = ""
                    updated_config["download_directory"] = ""
                elif actions == ["preview_replace"]:
                    updated_config["replace_resource"] = ""
                    updated_config["replace_episodes"] = ""
                elif actions == ["submit_replace"]:
                    updated_config["replace_resource"] = ""
                    updated_config["replace_episodes"] = ""
            self.update_config(updated_config)
        elif historical_migrated or cache_setting_removed or keyword_choice_migrated:
            self.update_config(config)
        self._reconcile_pending_states()
        if not was_active:
            self._schedule_progress_check()
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
        entry = self._state["entries"].get(str(subscribe.id)) or {}
        if (entry.get("status") == "library_complete"
                and entry.get("identity") == _identity(subscribe)
                and entry.get("library_scope") == _library_scope(subscribe)):
            return False
        if subscribe.id > (self._state.get("baseline") or 0):
            return True
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
            fresh = (not isinstance(entry, dict) or entry.get("identity") != _identity(subscribe)
                     or entry.get("status") == "library_complete")
            if fresh:
                entry = {
                    "identity": _identity(subscribe),
                    "source": "historical" if subscribe.id <= self._state["baseline"] else "new",
                    "original_state": subscribe.state,
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
        self._schedule_progress_check(ids)
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
            self._schedule_progress_check([sid])
            self._schedule_scan([sid])

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Optional[Event] = None) -> None:
        """订阅进度或状态变化后安排一次轻量的自动批准检查。"""
        if not self._enabled or event is None or not isinstance(event.event_data, dict):
            return
        data = event.event_data
        sid = data.get("subscribe_id")
        if not isinstance(sid, int) or isinstance(sid, bool):
            return
        fields = data.get("fields")
        if isinstance(fields, list) and not {"lack_episode", "total_episode", "start_episode", "state"}.intersection(fields):
            return
        with self._lock:
            entry = self._state["entries"].get(str(sid)) or {}
            if entry.get("status") != "pending" or entry.get("revoked"):
                return
        self._schedule_progress_check([sid])

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Optional[Event] = None) -> None:
        """整理完成后尽快复核同一媒体的待确认订阅进度。"""
        if not self._enabled or event is None or not isinstance(event.event_data, dict):
            return
        mediainfo = event.event_data.get("mediainfo")
        if mediainfo is None:
            return
        try:
            media_source, media_id = resolve_media_identity(media=mediainfo)
            media_key = build_media_key(media_source, media_id)
            if not media_key:
                return
            meta = event.event_data.get("meta")
            season = getattr(meta, "begin_season", None)
            with self._lock:
                ids = [
                    int(key) for key, entry in self._state["entries"].items()
                    if entry.get("status") == "pending"
                    and not entry.get("revoked")
                    and (subscribe := self._subscribes.get(int(key))) is not None
                    and build_media_key(subscribe.media_source, subscribe.media_id) == media_key
                    and (season is None or subscribe.season == season)
                ]
        except Exception:
            logger.exception("整理完成后的订阅匹配失败，留待定时进度检查")
            return
        if ids:
            self._schedule_progress_check(ids)

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

    def _schedule_progress_check(self, subscription_ids: Optional[list[int]] = None) -> None:
        """合并订阅变更、整理事件和启动时的进度检查。"""
        if not self._enabled:
            return
        with self._lock:
            if subscription_ids is None:
                subscription_ids = [
                    int(key) for key, entry in self._state["entries"].items()
                    if entry.get("status") == "pending" and not entry.get("revoked")
                ]
            self._queued_progress_ids.update(subscription_ids)
            if not self._queued_progress_ids:
                return
        try:
            add_plugin_once_job(
                self.__class__.__name__, "progress-check", self._run_scheduled_progress_check,
                "核对待确认剧集进度", delay_seconds=2,
            )
        except RuntimeError:
            logger.warning("调度器尚未就绪，订阅进度检查将在周期任务中执行")

    def _run_scheduled_progress_check(self) -> None:
        """只消费已合并的订阅 ID；未满进度交给后续变更或周期检查。"""
        with self._lock:
            ids = sorted(self._queued_progress_ids)
            self._queued_progress_ids.clear()
        if ids:
            self.approve_progress_complete(subscription_ids=ids)

    @staticmethod
    def _can_auto_approve(subscribe: Any, entry: dict[str, Any]) -> bool:
        """保留用户暂停、显式撤销和增强版待定状态的控制权。"""
        return bool(
            entry.get("status") == "pending" and not entry.get("revoked")
            and entry.get("identity") == _identity(subscribe)
            and subscribe.type == MediaType.TV.value and not subscribe.best_version
            and subscribe.state in {"N", "R", "S"}
            and entry.get("original_state") != "P"
            and not (entry.get("source") == "historical" and entry.get("original_state") == "S")
            and not (subscribe.state == "S" and not entry.get("plugin_paused"))
        )

    @staticmethod
    def _subscribe_progress_complete(subscribe: Any) -> bool:
        """按普通剧集卡片的总集数与缺失集数口径判断进度是否已满。"""
        total = subscribe.total_episode
        start = subscribe.start_episode or 1
        lack = subscribe.lack_episode
        return bool(
            isinstance(total, int) and not isinstance(total, bool) and total > 0
            and isinstance(start, int) and not isinstance(start, bool) and 1 <= start <= total
            and isinstance(lack, int) and not isinstance(lack, bool) and lack == 0
        )

    def approve_progress_complete(self, *, subscription_ids: Optional[list[int]] = None) -> int:
        """普通剧集订阅进度已满后直接批准；规则原样保留。"""
        if not self._enabled:
            return 0
        with self._lock:
            ids = subscription_ids if subscription_ids is not None else [
                int(key) for key, entry in self._state["entries"].items()
                if entry.get("status") == "pending"
            ]
        approved = 0
        for sid in dict.fromkeys(ids):
            with self._lock:
                first_entry = self._state["entries"].get(str(sid)) or {}
                first_subscribe = self._subscribes.get(sid)
            if first_subscribe and first_entry.get("status") == "pending":
                self._check_library_once(first_subscribe)
            with self._lock:
                entry = self._state["entries"].get(str(sid)) or {}
                subscribe = self._subscribes.get(sid)
                if subscribe is None or not self._can_auto_approve(subscribe, entry):
                    continue
                identity = _identity(subscribe)
                scope = (subscribe.season, subscribe.start_episode, subscribe.total_episode,
                         subscribe.lack_episode, subscribe.best_version,
                         getattr(subscribe, "manual_total_episode", False), subscribe.state)
            try:
                if not self._subscribe_progress_complete(subscribe):
                    continue
            except Exception:
                logger.exception(f"订阅 {sid} 进度检查失败，保持待确认")
                continue
            with self._lock:
                current = self._subscribes.get(sid)
                entry = self._state["entries"].get(str(sid)) or {}
                if (current is None or _identity(current) != identity
                        or (current.season, current.start_episode, current.total_episode,
                            current.lack_episode, current.best_version,
                            getattr(current, "manual_total_episode", False), current.state) != scope
                        or not self._subscribe_progress_complete(current)
                        or not self._can_auto_approve(current, entry)):
                    continue
                success, message = self._approve(sid, entry, None, None)
            if success:
                approved += 1
                logger.info(f"剧集守门员：订阅 {sid} 进度已满，自动直接批准；原规则保持不变")
            else:
                logger.warning(f"剧集守门员：订阅 {sid} 进度已满但自动批准失败：{message}")
        return approved

    def scan_subscription(self, sid_value: Any, *, selected_candidate: Any = None) -> tuple[bool, str]:
        """手动重新搜索指定的已纳入待确认剧集，不触发其他订阅或下载补搜。"""
        try:
            if isinstance(sid_value, bool):
                raise ValueError
            sid = int(sid_value)
        except (ValueError, TypeError, OverflowError):
            return False, "请选择一条已纳入管理的待确认剧集"
        with self._lock:
            subscribe = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            if not self._enabled:
                return False, "请先启用插件"
            if not subscribe or subscribe.type != MediaType.TV.value or subscribe.state not in {"N", "R", "P", "S"} \
                    or entry.get("status") != "pending" or entry.get("identity") != _identity(subscribe):
                return False, "请选择仍在管理中的待确认剧集；已批准剧集可使用回刷资源预览"
            if not subscribe.media_source or not subscribe.media_id:
                return False, "该订阅缺少媒体识别信息，无法扫描"
        if not self._scan_lock.acquire(blocking=False):
            return False, "已有候选扫描正在进行，请稍后重试"
        try:
            with self._lock:
                self._queued_scan_ids.discard(sid)
                if self._prune_confirmed_records():
                    self._save_state()
            completed = self._scan_one(sid)
            with self._lock:
                entry = self._state["entries"].get(str(sid)) or {}
                if completed and (entry.get("status") == "library_complete" or not self._subscribes.get(sid)):
                    return True, f"订阅 #{sid} 首次媒体库核对已齐集，进度已更新并退出插件管理"
                if entry.get("status") != "pending" or entry.get("identity") != _identity(subscribe):
                    return False, "扫描期间订阅发生变化，请刷新列表"
                if entry.get("last_error"):
                    return False, entry["last_error"]
                if not completed:
                    return False, "扫描未完成，请检查插件状态并刷新列表"
                # 新扫描更新了本轮搜索结果，旧规则预览不能继续作为确认依据。
                entry.pop("preview", None)
                self._save_state()
                count = entry.get("last_scan_count", 0)
                message = f"订阅 #{sid} 扫描完成，本轮发现候选 {count} 条，累计保留 {len(entry.get('candidates') or [])} 条"
                if isinstance(selected_candidate, str) and selected_candidate.startswith(f"{sid}:"):
                    candidate_id = selected_candidate.split(":", 1)[1]
                    selected = next((item for item in entry.get("candidates") or []
                                     if item.get("id") == candidate_id), None)
                    latest = self._preview_search_cache.get((_identity(subscribe), ())) or {}
                    found_now = False
                    if selected and selected.get("site_id") in (latest.get("covered_sites") or []):
                        for context in latest.get("contexts") or []:
                            try:
                                item = resource_candidate(
                                    context, subscribe.season or 1, entry["last_scan"], subscribe.total_episode,
                                )
                            except (TypeError, ValueError, OverflowError):
                                continue
                            if (item and item.get("id") == candidate_id
                                    and item.get("site_id") == selected.get("site_id")
                                    and public_page_url(item.get("page_url")) == public_page_url(selected.get("page_url"))):
                                found_now = True
                                break
                    if not found_now:
                        message += ("；所选版本本轮未再次找到，列表中显示的是历史候选；"
                                    "确认下载只使用内存中的真实链接，缺少时请手动重新扫描此剧集")
                failures = (entry.get("search_diagnostics") or {}).get("site_failures") or []
                if failures:
                    message += "；结果可能不完整：" + "；".join(
                        f"{item['site_name']}：{item['reason']}" for item in failures)
                elif not count:
                    message += "；本轮未发现可用版本，建议使用 MoviePilot 手动搜索核对站点资源"
                return True, message + "；下方版本列表已更新，预览可复用本次搜索"
        finally:
            self._scan_lock.release()

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
                self._forget_search_cache(lambda key: key[0] not in active_identities,
                                          reason="订阅删除或媒体身份变化")
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
            # 进度已满的订阅无需继续搜索站点候选。
            self.approve_progress_complete(subscription_ids=subscription_ids)
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

    def _check_library_once(self, subscribe: Any) -> bool:
        """首次候选扫描前只读媒体库缺集，并同步集数进度。"""
        with self._library_check_lock:
            return self._check_library_once_locked(subscribe)

    def _check_library_once_locked(self, subscribe: Any) -> bool:
        """同一插件实例的并发进度任务共用一次核对结果。"""
        sid = subscribe.id
        identity = _identity(subscribe)
        checked_scope = _library_scope(subscribe)
        with self._lock:
            entry = self._state["entries"].get(str(sid)) or {}
            if (entry.get("status") == "library_complete" and entry.get("identity") == identity
                    and entry.get("library_scope") == checked_scope):
                return True
            if (entry.get("status") != "pending" or entry.get("identity") != identity
                    or entry.get("library_checked") or entry.get("revoked")):
                return False
        # 洗版订阅即使文件已入库仍可能需要替换；P 状态不改变原有控制权。
        if subscribe.best_version or subscribe.state == "P" or entry.get("original_state") == "P":
            return False

        error = None
        total = None
        lack = None
        try:
            chain = SubscribeChain()
            snapshot = chain.subscription_repository.get(sid)
            source, media_id = resolve_media_identity(media=subscribe)
            if (snapshot is None or snapshot.type != MediaType.TV.value
                    or snapshot.season != subscribe.season
                    or snapshot.episode_group != subscribe.episode_group
                    or build_media_key(*resolve_media_identity(media=snapshot)) != build_media_key(source, media_id)
                    or not isinstance(subscribe.season, int) or isinstance(subscribe.season, bool)
                    or subscribe.season < 0):
                error = "订阅快照或目标季不可核对"
            else:
                meta = build_subscribe_meta(snapshot)
                media = MediaChain().recognize_media(
                    meta=meta, mtype=meta.type, media_source=source, media_id=media_id,
                    episode_group=snapshot.episode_group, cache=False,
                )
                recognized_key = build_media_key(*resolve_media_identity(media=media)) if media else None
                episodes = (getattr(media, "seasons", None) or {}).get(snapshot.season) if media else None
                if not recognized_key or recognized_key != build_media_key(source, media_id) or not episodes:
                    error = "媒体识别未返回对应目标季"
                else:
                    saved_total = snapshot.total_episode
                    if not isinstance(saved_total, int) or isinstance(saved_total, bool) or saved_total < 0:
                        saved_total = 0
                    total = saved_total if getattr(snapshot, "manual_total_episode", False) else max(saved_total, len(episodes))
                    start = snapshot.start_episode or 1
                    if (not isinstance(start, int) or isinstance(start, bool)
                            or start < 1 or total < start):
                        error = "目标集数范围无效"
                    else:
                        exists, no_exists = DownloadChain().get_no_exists_info(
                            meta=meta, mediainfo=media, totals={snapshot.season: total},
                        )
                        target = (no_exists.get(recognized_key) or {}).get(snapshot.season) if isinstance(no_exists, dict) else None
                        if target is not None:
                            missing = getattr(target, "episodes", None) or []
                            if not isinstance(missing, list):
                                error = "媒体库缺集结果无效"
                            elif missing:
                                lack = len(set(range(start, total + 1)).intersection(missing))
                            else:
                                # 宿主用空集列表表示整季缺失，不能当成已齐集。
                                lack = total - start + 1
                        elif exists is True:
                            lack = 0
                        else:
                            error = "媒体库未返回目标季缺集结果"
        except Exception:
            logger.exception(f"订阅 {sid} 首次媒体库核对失败，继续候选扫描")
            error = "媒体库核对失败，详情见插件日志"

        with self._lock:
            entry = self._state["entries"].get(str(sid)) or {}
            current = self._subscribes.get(sid)
            if (entry.get("status") != "pending" or entry.get("identity") != identity
                    or not current or _identity(current) != identity
                    or _library_scope(current) != checked_scope):
                return False
            if error:
                entry["library_checked"] = True
                entry["library_checked_at"] = _utc_now()
                entry["library_check_error"] = error
                logger.warning(f"订阅 {sid} 首次媒体库核对未完成：{error}")
                self._save_state()
                return False
            update = {}
            if current.total_episode != total:
                update["total_episode"] = total
            if current.lack_episode != lack:
                update["lack_episode"] = lack
            if lack == 0 and current.state in {"N", "R", "S"}:
                # 已齐集只退出插件管理，不调用宿主订阅完成流程。
                if entry.get("plugin_paused") and current.state == "S":
                    update["state"] = entry.get("original_state") if entry.get("original_state") in {"N", "R"} else "R"
            try:
                if update:
                    self._subscribes.update(sid, update)
            except Exception:
                logger.exception(f"订阅 {sid} 首次媒体库进度更新失败，留待下一次扫描重试")
                return False
            entry["library_checked"] = True
            entry["library_checked_at"] = _utc_now()
            entry.pop("library_check_error", None)
            if lack == 0 and current.state in {"N", "R", "S"}:
                current = self._subscribes.get(sid)
                entry.update({
                    "status": "library_complete", "library_scope": _library_scope(current),
                    "plugin_paused": False, "candidates": [],
                })
                self._queued_scan_ids.discard(sid)
                self._queued_progress_ids.discard(sid)
                self._forget_search_cache(lambda key: key[0] == identity, reason="媒体库已齐集")
                self._save_state()
                logger.info(f"订阅 {sid} 首次媒体库核对已齐集，已更新进度并退出插件管理；不触发订阅完成")
                return True
            self._save_state()
            logger.info(f"订阅 {sid} 首次媒体库核对缺失 {lack} 集，已更新进度并继续候选扫描")
        return False

    def _scan_one(self, sid: int) -> bool:
        """
        搜索一条待确认订阅的首集、含首集多集资源、整季及跨季资源，并保留历史候选。
        """
        subscribe = self._subscribes.get(sid)
        if not subscribe:
            with self._lock:
                self._state["entries"].pop(str(sid), None)
                self._save_state()
            return False
        if subscribe.type != MediaType.TV.value or not subscribe.media_source or not subscribe.media_id:
            return False
        if self._check_library_once(subscribe):
            return True
        subscribe = self._subscribes.get(sid)
        with self._lock:
            entry = self._state["entries"].get(str(sid)) or {}
            if (not subscribe or entry.get("status") != "pending"
                    or entry.get("identity") != _identity(subscribe)):
                return False
        try:
            diagnostics: dict[str, Any] = {}
            scan_sites, _names = _search_site_scope([])
            contexts = self._search_unfiltered(subscribe, sites=scan_sites, diagnostics=diagnostics)
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
                    fresh_ids = {item["id"] for item in fresh}
                    for candidate in entry["candidates"]:
                        candidate["found_latest_scan"] = candidate["id"] in fresh_ids
                    entry["last_scan"] = now
                    entry["last_scan_count"] = len(fresh)
                    entry["search_diagnostics"] = _search_diagnostics_snapshot(diagnostics)
                    entry.pop("last_error", None)
                    self._remember_search(subscribe, contexts, [], "候选扫描", now, diagnostics,
                                          covered_sites=scan_sites)
                    self._save_state()
                    return True
        except Exception:
            logger.exception(f"扫描订阅 {sid} 的目标季候选失败")
            with self._lock:
                entry = self._state["entries"].get(str(sid))
                if entry and entry.get("status") == "pending":
                    entry["last_error"] = "搜索失败，详情见插件日志"
                    self._save_state()
        return False

    def _search_unfiltered(
        self, subscribe: Any, timings: Optional[dict[str, float]] = None, *, sites: Optional[list[int]] = None,
        diagnostics: Optional[dict[str, Any]] = None,
        preserve_duplicates: bool = False,
    ) -> list[Any]:
        """搜索目标季与原订阅；下载提交前保留重复结果以检查资源唯一性。"""
        search_stop = self._search_stop
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
        focused_keywords: dict[tuple[Any, ...], list[str]] = {}
        original_keys: set[tuple[Any, ...]] = set()
        rounds = []
        supplemental_failed = False
        original_search: dict[str, Any] = {"checked": False, "resource_ids": []}
        # 只在这一条订阅的本轮中停止失败站点和复用相同查询，其他订阅独立尝试。
        failed_sites: dict[Any, dict[str, Any]] = {}
        budget_deferrals: dict[int, dict[str, Any]] = {}
        request_cache: dict[tuple, list[Any]] = {}

        def search(name: str, *, focused: bool) -> list[Any]:
            if search_stop.is_set():
                raise RuntimeError("插件搜索已停止")
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
            chain = CandidateSearchChain(focused_season=season if focused else None,
                                         failed_sites=failed_sites, budget_deferrals=budget_deferrals,
                                         request_cache=request_cache,
                                         cancelled=lambda: search_stop.is_set() or not self._enabled)
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
                key = _resource_key(context)
                if not focused:
                    original_keys.add(key)
                    continue
                actual_keyword = next((report["keyword"] for report in chain.site_reports
                                       if report.get("site_id") == item.get("site_id")
                                       and report.get("count") and report.get("keyword")), keyword)
                words = focused_keywords.setdefault(key, [])
                if actual_keyword not in words:
                    words.append(actual_keyword)
            rounds.append({"keyword": label, "kind": "target_season" if focused else "original_search",
                           **counts, "result_count": len(result),
                           "site_keywords": list(dict.fromkeys(report["keyword"] for report in chain.site_reports
                                                                if report.get("keyword"))),
                           "requests_paced": True,
                           "request_count": sum(bool(report.get("attempted")) for report in chain.site_reports),
                           "reused_request_count": sum(report.get("status") == "reused_in_round"
                                                       for report in chain.site_reports),
                           "site_waits": [{"site_id": sid,
                                           "site_name": next((report["site_name"] for report in chain.site_reports
                                                              if report.get("site_id") == sid), "未知站点"),
                                           "seconds": round(seconds, 3)}
                                          for sid, seconds in chain.site_waits.items()],
                           "seconds": round(time.perf_counter() - round_started, 3)})
            logger.info(f"订阅 {subscribe.id} {'带季搜索' if focused else '原订阅搜索验证'}：{label}，"
                        f"媒体匹配 {counts['identified_count']} 条，目标季 {len(result)} 条")
            # 只汇总这一条订阅本轮的站点问题，不重复统计同站点的后续跳过。
            failures = list({item["site_id"]: item for item in reversed(chain.site_reports)
                             if item.get("reason")}.values())
            if not focused:
                original_search.update(checked=True, keyword=keyword, search_imdbid=bool(getattr(subscribe, "search_imdbid", False)),
                                       custom_words=getattr(subscribe, "custom_words", None) or None,
                                       site_failures=failures)
            if failures and not result:
                logger.warning(f"订阅 {subscribe.id} 本轮有 {len(failures)} 个站点失败或暂缓；仅本条订阅本轮跳过后续查询")
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
        # 预览按详情页合并；下载提交则保留同轮重复记录以检查唯一性。
        if preserve_duplicates:
            def exact_record_key(context: Any) -> tuple[Any, ...]:
                torrent = getattr(context, "torrent_info", None)
                meta = getattr(context, "meta_info", None)
                return (getattr(torrent, "site", None), getattr(torrent, "title", None),
                        getattr(torrent, "size", None), getattr(torrent, "description", None),
                        getattr(torrent, "enclosure", None),
                        tuple(getattr(meta, "season_list", None) or []),
                        tuple(getattr(meta, "episode_list", None) or []))

            focused_keys = {exact_record_key(context) for context in contexts}
            contexts += [context for context in supplemental if exact_record_key(context) not in focused_keys]
        else:
            contexts = _distinct_contexts(contexts + supplemental)
        resource_keywords: dict[str, list[str]] = {}
        for context in contexts:
            try:
                item = preview_resource(context, season, subscribe.total_episode, _utc_now())
            except (TypeError, ValueError, OverflowError):
                continue
            if not item:
                continue
            key = _resource_key(context)
            words = resource_keywords.setdefault(item["id"], [])
            words.extend(word for word in focused_keywords.get(key, []) if word not in words)
            if key in original_keys and item["id"] not in original_search["resource_ids"]:
                original_search["resource_ids"].append(item["id"])
        if search_stop.is_set():
            raise RuntimeError("插件搜索已停止")
        if diagnostics is not None:
            failures = {item["site_id"]: item for item in reversed(site_reports) if item.get("reason")}
            diagnostics.update(rounds=rounds, site_failures=list(failures.values()), target_season=season,
                               supplemental_failed=supplemental_failed, resource_keywords=resource_keywords,
                               original_search=original_search)
        if timings is not None:
            timings["search_seconds"] = time.perf_counter() - started
        return contexts

    def _remember_search(
        self, subscribe: Any, contexts: list[Any], sites: list[int], source: str, searched_at: str,
        diagnostics: Optional[dict[str, Any]] = None,
        *, covered_sites: Optional[list[int]] = None, site_sources: Optional[dict[int, dict[str, Any]]] = None,
        oldest_at: Optional[float] = None,
    ) -> None:
        """每个媒体和站点范围保留本轮结果及去重的历史资源。"""
        with self._lock:
            key = (_identity(subscribe), tuple(sorted(set(sites))))
            coverage = sorted(set(covered_sites if covered_sites is not None else _search_site_scope(sites)[0]))
            historical_contexts = _merge_history_contexts(self._preview_search_cache.get(key), contexts)
            if key not in self._preview_search_cache and len(self._preview_search_cache) >= MAX_CACHE_ENTRIES:
                oldest = min(self._preview_search_cache, key=lambda item: self._preview_search_cache[item]["at"])
                self._forget_search_cache(lambda item: item == oldest, reason="超过内存缓存组数上限")
            self._search_cache_sequence += 1
            stamp = time.time()
            monotonic_stamp = time.monotonic()
            sources = site_sources or {
                site_id: {"source": source, "searched_at": searched_at,
                          "searched_ts": stamp, "at": monotonic_stamp}
                for site_id in coverage
            }
            oldest_stamp = min((item.get("searched_ts", stamp) for item in sources.values()), default=stamp)
            self._preview_search_cache[key] = {
                "at": monotonic_stamp, "contexts": contexts, "source": source, "searched_at": searched_at,
                "historical_contexts": historical_contexts,
                "sequence": self._search_cache_sequence,
                "searched_ts": stamp,
                "oldest_searched_ts": oldest_stamp,
                "oldest_at": oldest_at if oldest_at is not None else
                             min((item.get("at", monotonic_stamp) for item in sources.values()),
                                 default=monotonic_stamp),
                "diagnostics": diagnostics,
                "covered_sites": coverage,
                "site_sources": sources,
            }
            store = self._disk_search_cache()
            if store is not None:
                try:
                    removed = store.save(key, self._preview_search_cache[key])
                    for cached_key in list(self._preview_search_cache):
                        if store.filename(cached_key) in removed:
                            self._preview_search_cache.pop(cached_key, None)
                except OSError:
                    logger.warning("无法保存持久搜索缓存，本轮继续使用内存结果")

    def _disk_search_cache(self) -> Optional[SearchCacheStore]:
        if self._search_cache_store is None:
            try:
                self._search_cache_store = SearchCacheStore(self.get_data_path() / "search-cache")
            except (OSError, AttributeError):
                logger.warning("插件数据目录不可用，搜索缓存暂时仅在内存中复用")
        return self._search_cache_store

    def _load_search_cache(self) -> None:
        store = self._disk_search_cache()
        self._search_cache_loaded = True
        if store is None:
            return
        identities = {_identity(subscribe) for subscribe in self._subscribes.list()
                      if subscribe.type == MediaType.TV.value}
        try:
            restored = store.load(identities)
        except OSError:
            logger.warning("无法读取持久搜索缓存，将在需要时重新搜索")
            return
        self._preview_search_cache.update(restored)
        self._search_cache_sequence = max([self._search_cache_sequence] + [
            entry["sequence"] for entry in restored.values()
        ])
        if restored:
            logger.info(f"已恢复 {len(restored)} 组持久搜索缓存，预览无需重新搜索对应站点范围")

    def _forget_search_cache(self, predicate: Any, *, extra_keys: tuple = (), reason: str = "主动清理") -> None:
        """容量淘汰、批准、停用和删除订阅时同步移除内存与磁盘结果。"""
        store = self._disk_search_cache()
        for key in set(self._preview_search_cache).union(extra_keys):
            if predicate(key):
                self._preview_search_cache.pop(key, None)
                logger.info(f"搜索缓存清理：订阅身份 {key[0]}，站点范围 {key[1] or '默认'}；原因：{reason}")
                if store is not None:
                    try:
                        store.delete(key)
                    except OSError:
                        logger.warning("无法移除持久搜索缓存，请检查插件数据目录")

    def _cache_parts(self, subscribe: Any, required_sites: list[int]) -> tuple[list[dict[str, Any]], list[int]]:
        """为每个站点选最近的覆盖缓存；未记录覆盖站点的旧默认缓存不能证明范围。"""
        selected: dict[int, dict[str, Any]] = {}
        identity = _identity(subscribe)
        with self._lock:
            for (media_identity, cached_sites), entry in self._preview_search_cache.items():
                if media_identity != identity:
                    continue
                recorded = entry.get("covered_sites")
                covered = set(recorded) if isinstance(recorded, list) else set(cached_sites)
                for site_id in set(required_sites) & covered:
                    previous = selected.get(site_id)
                    source = (entry.get("site_sources") or {}).get(site_id) or {}
                    freshness = (source.get("searched_ts", entry["searched_ts"]),
                                 entry["at"], entry["sequence"])
                    if previous is None or freshness > previous["freshness"]:
                        selected[site_id] = {"entry": entry, "key": (media_identity, cached_sites),
                                             "freshness": freshness}
        grouped: dict[tuple, dict[str, Any]] = {}
        for site_id, choice in selected.items():
            part = grouped.setdefault(choice["key"], {**choice, "sites": []})
            part["sites"].append(site_id)
        return list(grouped.values()), sorted(set(required_sites) - selected.keys())

    @staticmethod
    def _combine_cache_parts(subscribe: Any, parts: list[dict[str, Any]]) -> dict[str, Any]:
        """按站点拼接资源与搜索词证据，避免把其他范围的结果算入预览。"""
        contexts = []
        site_sources: dict[int, dict[str, Any]] = {}
        keywords: dict[str, list[str]] = {}
        original_ids: set[str] = set()
        failures: dict[int, dict[str, Any]] = {}
        original_failures: dict[int, dict[str, Any]] = {}
        original_settings = None
        originals_compatible = True
        supplemental_failed = False
        for part in parts:
            entry = part["entry"]
            sites = set(part["sites"])
            part_contexts = [context for context in entry["contexts"]
                             if getattr(getattr(context, "torrent_info", None), "site", None) in sites]
            contexts.extend(part_contexts)
            resource_ids = set()
            for context in part_contexts:
                try:
                    item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
                except (TypeError, ValueError, OverflowError):
                    continue
                if item:
                    resource_ids.add(item["id"])
            diagnostics = entry.get("diagnostics") or {}
            for resource_id in resource_ids:
                words = keywords.setdefault(resource_id, [])
                words.extend(word for word in (diagnostics.get("resource_keywords") or {}).get(resource_id, [])
                             if word not in words)
            original = diagnostics.get("original_search") or {}
            settings = (original.get("keyword"), original.get("search_imdbid"), original.get("custom_words"))
            if not original.get("checked") or (original_settings is not None and settings != original_settings):
                originals_compatible = False
            original_settings = settings if original_settings is None else original_settings
            original_ids.update(resource_ids & set(original.get("resource_ids") or []))
            for failure in diagnostics.get("site_failures") or []:
                if failure.get("site_id") in sites:
                    failures[failure["site_id"]] = failure
            for failure in original.get("site_failures") or []:
                if failure.get("site_id") in sites:
                    original_failures[failure["site_id"]] = failure
            supplemental_failed |= bool(diagnostics.get("supplemental_failed"))
            for site_id in sites:
                site_sources[site_id] = (entry.get("site_sources") or {}).get(site_id) or {
                    "source": entry["source"], "searched_at": entry["searched_at"],
                    "searched_ts": entry["searched_ts"], "at": entry["at"],
                }
        newest = max((part["entry"] for part in parts), key=lambda entry: (entry["at"], entry["sequence"]))
        newest_source = max(site_sources.values(), key=lambda item: item["searched_ts"], default={})
        oldest_at = min((site_sources[site_id].get("at", part["entry"]["at"])
                         for part in parts for site_id in part["sites"]), default=time.monotonic())
        original_keyword, original_imdb, original_words = original_settings or (None, None, None)
        diagnostics = {
            "resource_keywords": keywords, "site_failures": list(failures.values()),
            "original_search": {
                "checked": originals_compatible and bool(parts), "keyword": original_keyword,
                "search_imdbid": original_imdb, "custom_words": original_words,
                "resource_ids": sorted(original_ids) if originals_compatible else [],
                "site_failures": list(original_failures.values()),
            },
            "supplemental_failed": supplemental_failed,
            "rounds": copy.deepcopy((parts[0]["entry"].get("diagnostics") or {}).get("rounds") or [])
            if len(parts) == 1 else [],
        }
        return {
            "contexts": contexts, "diagnostics": diagnostics, "at": oldest_at,
            "searched_at": newest_source.get("searched_at", newest["searched_at"]),
            "searched_ts": newest_source.get("searched_ts", newest["searched_ts"]),
            "source": newest["source"] if len(parts) == 1 else "多个站点范围的搜索缓存",
            "site_sources": site_sources,
        }

    def _cached_search(self, subscribe: Any, sites: list[int]) -> Optional[dict[str, Any]]:
        """从最近的覆盖缓存按站点组合结果；任一站点缺失则返回空。"""
        required, _names = _search_site_scope(sites)
        if not required:
            with self._lock:
                cached = self._preview_search_cache.get((_identity(subscribe), ()))
                return cached if cached is not None and cached.get("covered_sites") == [] else None
        parts, missing = self._cache_parts(subscribe, required)
        return None if missing else self._combine_cache_parts(subscribe, parts)

    def _preview_contexts(
        self, subscribe: Any, refresh_search: bool, timings: dict[str, float], sites: list[int],
    ) -> tuple[list[Any], bool, float, str, str, Any, dict[str, Any]]:
        """复用已覆盖站点，只向缺少缓存的站点发出搜索。"""
        required, names = _search_site_scope(sites)
        cached = self._cached_search(subscribe, sites) if not refresh_search else None
        if cached is not None:
            return (cached["contexts"], True, time.monotonic() - cached["at"], cached["source"],
                    cached["searched_at"], cached.get("diagnostics"), {
                        "reused_sites": required, "searched_sites": [],
                        "reused_site_labels": _site_labels(required, names), "searched_site_labels": [],
                        "site_sources": {site_id: {key: value for key, value in item.items() if key != "at"}
                                         for site_id, item in (cached.get("site_sources") or {}).items()},
                    })
        parts, missing = self._cache_parts(subscribe, required) if not refresh_search else ([], required)
        if not required:
            missing = []
        reused = sorted(set(required) - set(missing)) if not refresh_search else []
        query_sites = missing if required else sites
        logger.info(f"订阅 {subscribe.id} 预览缓存覆盖：复用 {_site_labels(reused, names) or '无'}；"
                    f"{'手动刷新' if refresh_search else '缺失'} {_site_labels(required if refresh_search else missing, names) or '默认范围'}；"
                    f"本轮只搜索 {_site_labels(required if refresh_search else missing, names) or '默认范围'}")
        diagnostics: dict[str, Any] = {}
        try:
            fresh_contexts = self._search_unfiltered(subscribe, timings, sites=query_sites,
                                                     diagnostics=diagnostics)
        except Exception:
            if refresh_search and self._enabled and not self._search_stop.is_set():
                # 本轮没有可确认的实时结果，旧资源仍留作历史供用户选择。
                self._remember_search(subscribe, [], sites, "主动重新搜索失败", _utc_now(),
                                      covered_sites=required)
            raise
        searched_at = _utc_now()
        stamp = time.time()
        if parts:
            fresh_entry = {"contexts": fresh_contexts, "diagnostics": diagnostics,
                           "source": "缺失站点补搜", "searched_at": searched_at,
                           "searched_ts": stamp, "at": time.monotonic(),
                           "sequence": self._search_cache_sequence + 1}
            combined = self._combine_cache_parts(subscribe, [*parts, {
                "entry": fresh_entry, "sites": missing,
            }])
            contexts = combined["contexts"]
            diagnostics = combined["diagnostics"]
            site_sources = combined["site_sources"]
            oldest_at = combined["at"]
            source = "部分站点复用缓存，其余站点补搜"
        else:
            contexts = fresh_contexts
            site_sources = None
            oldest_at = None
            source = "规则预览搜索"
        # 成功时补成当前完整范围；较宽范围已有缓存仍可供切换范围后复用。
        self._remember_search(subscribe, contexts, sites, source, searched_at, diagnostics,
                              covered_sites=required, site_sources=site_sources, oldest_at=oldest_at)
        return (contexts, False, max(0.0, time.monotonic() - oldest_at) if parts else 0.0,
                source, searched_at, diagnostics, {
                    "reused_sites": reused, "searched_sites": required if refresh_search else missing,
                    "reused_site_labels": _site_labels(reused, names),
                    "searched_site_labels": _site_labels(required if refresh_search else missing, names),
                    "site_sources": {site_id: {key: value for key, value in item.items() if key != "at"}
                                     for site_id, item in (site_sources or {}).items()},
                })

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
        keyword_choice: str = "original",
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
        requested_keyword_choice = _keyword_selection(keyword_choice)
        search_sites = getattr(working, "sites", None) or []
        contexts, cached, cache_age, source, result_at, diagnostics, cache_coverage = self._preview_contexts(
            subscribe, refresh_search, timings, search_sites,
        )
        actual_search_sites = sorted(set(cache_coverage["reused_sites"] + cache_coverage["searched_sites"]))
        # 旧版持久缓存可能含同一详情页的多份结果，预览时也要收敛它们。
        distinct = _distinct_contexts(contexts)
        id_aliases = {}
        if len(distinct) != len(contexts):
            ids_by_context = {}
            for context in contexts:
                try:
                    item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
                except (TypeError, ValueError, OverflowError):
                    continue
                if item:
                    ids_by_context[id(context)] = item["id"]
            # 保留用户正在预览的版本，避免旧缓存内另一份解析覆盖其选择值。
            selected_id = (candidate or {}).get("id")
            selected = next((context for context in contexts
                             if ids_by_context.get(id(context)) == selected_id), None) if selected_id else None
            if selected is not None:
                selected_key = _resource_key(selected)
                distinct = [_merge_resource_info(selected, context) if _resource_key(context) == selected_key else context
                            for context in distinct]
            ids_by_key = {}
            for context in distinct:
                try:
                    item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
                except (TypeError, ValueError, OverflowError):
                    continue
                if item:
                    ids_by_key[_resource_key(context)] = item["id"]
            for context in contexts:
                resource_id = ids_by_context.get(id(context))
                canonical_id = ids_by_key.get(_resource_key(context))
                if resource_id and canonical_id:
                    id_aliases[resource_id] = canonical_id
        contexts = distinct
        current_candidates = set()
        for context in contexts:
            try:
                item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
            except (TypeError, ValueError, OverflowError):
                continue
            if item:
                current_candidates.add((item["id"], item.get("site_id"), item.get("page_url")))
        with self._lock:
            saved_candidates = list((self._state["entries"].get(str(subscribe.id)) or {}).get("candidates") or [])
        historical_candidates = [
            {
                "id": item["id"], "title": item.get("title") or "",
                "site_name": item.get("site_name") or str(item.get("site_id") or "未知站点"),
                "size_label": _size_label(item.get("size_bytes"), exact=True),
                "last_seen": item.get("last_seen") or "",
                "page_url": public_page_url(item.get("page_url")),
            }
            for item in saved_candidates
            if (not actual_search_sites or item.get("site_id") in actual_search_sites)
            and (item.get("id"), item.get("site_id"), public_page_url(item.get("page_url")))
            not in current_candidates
        ]
        resource_keywords = {}
        for resource_id, words in ((diagnostics or {}).get("resource_keywords") or {}).items():
            target_id = id_aliases.get(resource_id, resource_id)
            selected_words = resource_keywords.setdefault(target_id, [])
            selected_words.extend(word for word in words if word not in selected_words)
        candidate_outside_scope = bool(candidate and actual_search_sites
                                       and candidate.get("site_id") not in actual_search_sites)
        verification_ids = [(candidate or {}).get("id")]
        if candidate_outside_scope:
            # 候选只是规则模板；改选其他站点时，以该范围内符合规则的资源验证搜索词。
            verification_ids = []
            verification_params = SubscribeChain.get_params(working)
            for context in contexts:
                try:
                    item = preview_resource(context, subscribe.season or 1, subscribe.total_episode, _utc_now())
                    if item and self._rule_rejection(working, context, verification_params) is None:
                        verification_ids.append(item["id"])
                except (TypeError, ValueError, AttributeError, OverflowError):
                    continue
        candidate_keywords = next((resource_keywords.get(resource_id) for resource_id in verification_ids
                                   if resource_keywords.get(resource_id)), [])
        seasonal_keyword = candidate_keywords[0] if candidate_keywords else None
        if requested_keyword_choice == "seasonal" and candidate and not seasonal_keyword:
            if candidate_outside_scope:
                raise ValueError("剧名＋季号搜索词未在当前站点范围召回符合规则的资源，请重新搜索或沿用原订阅")
            raise ValueError("剧名＋季号搜索词未在最新搜索中召回这个版本，请重新扫描或沿用原订阅")
        original_search = (diagnostics or {}).get("original_search") or {}
        original_keyword = getattr(subscribe, "keyword", None) or None
        original_checked = bool(original_search.get("checked")
                                and original_search.get("keyword") == original_keyword
                                and original_search.get("search_imdbid") == bool(getattr(subscribe, "search_imdbid", False))
                                and original_search.get("custom_words") == (getattr(subscribe, "custom_words", None) or None))
        original_ids = {id_aliases.get(resource_id, resource_id)
                        for resource_id in original_search.get("resource_ids") or []} if original_checked else set()
        original_matched = bool(candidate and any(resource_id in original_ids for resource_id in verification_ids))
        failed_site_ids = {item.get("site_id") for item in original_search.get("site_failures") or []}
        site_failed = bool(set(actual_search_sites) & failed_site_ids) if candidate_outside_scope else bool(
            candidate and failed_site_ids and (candidate.get("site_id") is None
                                             or candidate.get("site_id") in failed_site_ids)
        )
        original_completed = original_checked and not site_failed
        keyword_choice_auto_changed = bool(candidate and requested_keyword_choice == "original"
                                           and not original_matched and original_completed and seasonal_keyword)
        effective_keyword_choice = "seasonal" if keyword_choice_auto_changed else requested_keyword_choice
        verified_resource = "当前站点范围内符合规则的资源" if candidate_outside_scope else "所选版本"
        search_keyword_notice = ""
        if effective_keyword_choice == "seasonal":
            search_keyword = seasonal_keyword
            search_keyword_changed = seasonal_keyword != original_keyword or bool(getattr(subscribe, "search_imdbid", False))
            search_keyword_verified = True
            search_keyword_notice = (
                f"原订阅搜索正常完成但未召回{verified_resource}；预览已自动切换为剧名＋季号搜索词。确认后才写入订阅。"
                if keyword_choice_auto_changed else
                f"已选择本轮召回{verified_resource}的剧名＋季号搜索词；预览只计入该词召回的资源，确认后才写入订阅。"
            )
            if getattr(subscribe, "search_imdbid", False):
                search_keyword_notice += "确认时会改用标题搜索。"
        else:
            search_keyword = original_keyword
            search_keyword_changed = False
            search_keyword_verified = original_matched
            if candidate and original_matched:
                search_keyword_notice = f"原订阅搜索已召回{verified_resource}，确认后保留原搜索词和搜索方式；当前按原订阅搜索结果预览。"
            elif candidate and original_completed:
                search_keyword_notice = f"原订阅和带季搜索均未召回{verified_resource}；不会修改搜索词，请重新搜索。"
            elif candidate:
                search_keyword_notice = "未能验证当前原订阅搜索（缓存缺少记录或站点失败），不能判断必须加季号；本次保留原搜索词，请重新搜索，或手动选择剧名＋季号搜索词并重新预览。"
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
            if effective_keyword_choice == "seasonal" and search_keyword not in resource_keywords.get(item["id"], []):
                reason = "未由确认后使用的搜索词召回"
            elif candidate and effective_keyword_choice == "original" and item["id"] not in original_ids:
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
                            "source": "预览自动切换" if keyword_choice_auto_changed else
                            "手动选择剧名＋季号" if effective_keyword_choice == "seasonal" else "保留原订阅"})
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
        priority = {"full_season": 5, "cross_season": 4, "suspected_full": 3,
                    "first_episode_bundle": 2, "first_episode": 1, "episode": 0}
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
            "historical_candidates": historical_candidates,
            "search_trigger": "cache" if cached else "manual" if refresh_search else
                              "partial_cache" if cache_coverage["reused_sites"] else "cache_miss",
            "search_reused_sites": cache_coverage["reused_sites"],
            "search_searched_sites": cache_coverage["searched_sites"],
            "search_reused_site_labels": cache_coverage["reused_site_labels"],
            "search_searched_site_labels": cache_coverage["searched_site_labels"],
            "search_site_sources": {str(site_id): source
                                    for site_id, source in cache_coverage["site_sources"].items()},
            "rule_groups_checked": True, "rule_groups_disabled": groups_disabled,
            "rule_groups_auto_disabled": auto_disabled, "rule_group_notice": notice,
            "rule_group_comparison": {
                "names": policy["names"], "source": policy["source"],
                "with_groups_count": with_groups_count, "without_groups_count": len(base_matches),
            } if policy["names"] else None,
            "search_source": source, "search_result_at": result_at, "search_sites": actual_search_sites,
            "search_diagnostics": _search_diagnostics_snapshot(diagnostics),
            "search_keyword": search_keyword, "original_search_keyword": getattr(subscribe, "keyword", None),
            "keyword_choice": effective_keyword_choice,
            "keyword_choice_auto_changed": keyword_choice_auto_changed,
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
        keyword_choice: str = "original",
    ) -> tuple[bool, str, str]:
        """
        生成候选规格的只读匹配预览，保持订阅暂停和原规则不变。

        :param choice: 订阅 ID 与候选 ID
        :param rule_json: 用户调整的草稿规则 JSON
        :param rule_mode: 选择候选站点、自选站点或不限站点
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
                keyword_choice=keyword_choice,
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
        *, keyword_choice: str = "original",
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
        try:
            selected_keyword_choice = _keyword_selection(keyword_choice)
        except ValueError as error:
            return False, str(error)
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
                    or preview.get("keyword_choice", "original") != selected_keyword_choice \
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
            candidate_in_scope = not preview.get("search_sites") or candidate.get("site_id") in preview["search_sites"]
            if not preview.get("matched_count") or (candidate_in_scope and candidate_id not in {
                item.get("id") for item in preview.get("matched") or []
            }):
                if candidate_in_scope:
                    return False, "所选版本未通过当前规则，请修改规则并重新预览"
                return False, "当前站点范围没有通过规则的资源，请修改规则并重新预览"
            if "search_keyword_changed" not in preview:
                return False, "请重新预览，确认是否需要调整原订阅搜索词"
            if preview["search_keyword_changed"] and (not preview.get("search_keyword") or not preview.get("search_keyword_verified")):
                return False, "请先重新搜索并预览，以验证剧名＋季号搜索词"
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

    def download_candidate_once(self, choice: str, download_directory: str = "") -> tuple[bool, str]:
        """只用内存中所选种子的真实下载链接整包提交，不在下载时搜索。"""
        try:
            sid_text, candidate_id = choice.split(":", 1)
            sid = int(sid_text)
        except (ValueError, TypeError):
            return False, "请先选择一条候选资源"
        with self._lock:
            subscribe = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            candidate = next((item for item in entry.get("candidates") or []
                              if item.get("id") == candidate_id), None)
            if (not self._enabled or not subscribe or subscribe.type != MediaType.TV.value
                    or entry.get("status") != "pending" or entry.get("identity") != _identity(subscribe)
                    or not candidate):
                return False, "候选或待确认订阅已变化，请重新选择"
            site_id = candidate.get("site_id")
            page_url = public_page_url(candidate.get("page_url"))
            if not isinstance(site_id, int) or isinstance(site_id, bool) or site_id <= 0 or not page_url:
                return False, "候选缺少可核对的站点详情链接，不能直接下载"
            try:
                save_path = selected_directory_root(download_directory) if download_directory \
                    else getattr(subscribe, "save_path", None) or None
                _search_site_scope([site_id])
            except ValueError as error:
                return False, str(error)
            parts, _missing = self._cache_parts(subscribe, [site_id])
            selected_cache = parts[0]["entry"] if parts else None
            identity = _identity(subscribe)
            relevant_caches = sorted(
                ((key, cached) for key, cached in self._preview_search_cache.items()
                 if key[0] == identity and (
                     site_id in (cached.get("covered_sites") if isinstance(cached.get("covered_sites"), list)
                                 else key[1])
                     or any(getattr(getattr(context, "torrent_info", None), "site", None) == site_id
                            for context in cached.get("historical_contexts") or [])
                 )),
                key=lambda pair: (pair[1]["searched_ts"], pair[1]["at"], pair[1]["sequence"]),
                reverse=True,
            )
            contexts = [context for _key, cached in relevant_caches
                        for context in [*(cached.get("contexts") or []),
                                        *(cached.get("historical_contexts") or [])]]
            target_scope = (subscribe.season, subscribe.total_episode, subscribe.start_episode)

        def matching_contexts(results: list[Any]) -> list[Any]:
            matches: dict[tuple[Any, ...], Any] = {}
            for result in results:
                try:
                    item = preview_resource(result, subscribe.season or 1, subscribe.total_episode, _utc_now())
                except (TypeError, ValueError, OverflowError):
                    continue
                if (item and item["id"] == candidate_id and item.get("site_id") == site_id
                        and item.get("page_url") == page_url):
                    key = _resource_key(result)
                    if key not in matches or (_has_download_link(result) and not _has_download_link(matches[key])):
                        matches[key] = result
            return list(matches.values())

        matches = matching_contexts(contexts)
        if len(matches) != 1:
            return False, ("内存中没有唯一匹配的所选种子；请点击“重新扫描此剧集”手动扫描，"
                           "完成后重新核对版本再下载")
        if not _has_download_link(matches[0]):
            return False, ("内存中的所选种子没有真实下载链接；请点击“重新扫描此剧集”手动扫描，"
                           "完成后重新核对版本再下载。站点详情链接不能直接用于下载")
        context = matches[0]
        media = getattr(context, "media_info", None)
        if media is None:
            return False, "资源缺少媒体信息，未提交下载"
        media_source, media_id = resolve_media_identity(media=media)
        expected_media_key = build_media_key(subscribe.media_source, subscribe.media_id)
        if not expected_media_key or build_media_key(media_source, media_id) != expected_media_key:
            return False, "资源的媒体身份与订阅不符，未提交下载"
        try:
            preview_download_path(media, save_path)
        except ValueError as error:
            return False, f"下载目录不可用：{error}；未提交下载"

        with self._lock:
            current = self._subscribes.get(sid)
            entry = self._state["entries"].get(str(sid)) or {}
            selected = next((item for item in entry.get("candidates") or []
                             if item.get("id") == candidate_id), None)
            if (not self._enabled or self._search_stop.is_set() or not current or current.type != MediaType.TV.value
                    or entry.get("status") != "pending" or entry.get("identity") != _identity(current)
                    or not selected or public_page_url(selected.get("page_url")) != page_url
                    or (current.season, current.total_episode, current.start_episode) != target_scope
                    or (not download_directory and (getattr(current, "save_path", None) or None) != save_path)):
                return False, "提交前订阅或候选已变化，未提交下载"
            latest_parts, _missing = self._cache_parts(current, [site_id])
            latest_cache = latest_parts[0]["entry"] if latest_parts else None
            if latest_cache is not selected_cache or any(
                self._preview_search_cache.get(key) is not cached for key, cached in relevant_caches
            ):
                return False, "提交前所选站点的搜索结果已变化，请重新选择后下载"
            if download_directory:
                try:
                    selected_directory_root(download_directory)
                except ValueError as error:
                    return False, f"{error}；未提交下载"
            try:
                result = DownloadChain().download_single(
                    context, episodes=None, source="Manual", save_path=save_path, return_detail=True,
                )
            except Exception:
                logger.exception(f"订阅 {sid} 的候选整包下载提交失败")
                return False, "下载提交失败，请查看 MoviePilot 日志"
            download_hash = result[0] if isinstance(result, tuple) else result
            if not download_hash:
                return False, "下载器未接受所选种子，请查看 MoviePilot 日志"
            history = entry.setdefault("direct_download_history", [])
            history.insert(0, {
                "candidate_id": candidate_id, "title": str(context.torrent_info.title)[:240],
                "site_name": str(candidate.get("site_name") or site_id)[:80],
                "size_bytes": candidate.get("size_bytes"), "submitted_at": _utc_now(),
            })
            del history[20:]
            self._save_state()
        return True, (f"已提交整包下载：{context.torrent_info.title}；"
                      "订阅仍待确认，进度满后将保留原规则自动恢复")

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
            self._forget_search_cache(lambda key: key[0] == entry.get("identity"), reason="订阅批准")
        if resume_state != "S":
            self._schedule_approved_search(sid)
        else:
            return True, f"订阅 {sid} 已批准；原本由用户暂停，仍保持暂停"
        if candidate_id:
            search_note = "已改用验证通过的搜索词" if payload and "keyword" in payload else "保留原搜索词"
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
            contexts = self._search_unfiltered(subscribe, preserve_duplicates=True)
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
                    rules = _draft_rules(candidate, _configured_draft_json(config),
                                         str(config.get("rule_mode") or "candidate"))
                else:
                    if not self._is_approved(subscribe):
                        return {"success": False, "message": "订阅未批准或身份已变化"}
                    rules = {}
                sites = rules.get("sites")
                if sites is None:
                    sites = getattr(subscribe, "sites", None) or []
                required, names = _search_site_scope(sites)
                cached = self._cached_search(subscribe, sites)
                parts, missing = self._cache_parts(subscribe, required) if cached is None else ([], [])
                reused = sorted(set(required) - set(missing)) if cached is None else required
                status = "manual" if config.get("refresh_search") else (
                    "partial_cache" if cached is None and reused else
                    "cache_miss" if cached is None else "cached" if cached["contexts"] else "cached_empty"
                )
                result = {"success": True, "status": status, "search_sites": sites if sites else required,
                          "search_result_count": len(cached["contexts"]) if cached is not None else None}
                if cached is None or config.get("refresh_search"):
                    result["missing_sites"] = [
                        {"id": site_id, "name": names.get(site_id, f"站点 {site_id}")}
                        for site_id in (required if config.get("refresh_search") else missing)
                    ]
                    result["reused_sites"] = [
                        {"id": site_id, "name": names.get(site_id, f"站点 {site_id}")}
                        for site_id in ([] if config.get("refresh_search") else reused)
                    ]
                return result
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
        try:
            indexers = SitesHelper().get_indexers() or []
        except Exception:
            logger.exception("无法读取可选站点列表")
            indexers = []
        site_choices = sorted(({
            "title": str(site.get("name") or f"站点 {site['id']}"), "value": site["id"],
        } for site in indexers if isinstance(site, dict)
            and isinstance(site.get("id"), int) and not isinstance(site["id"], bool)
            and site["id"] > 0 and site.get("is_active", True)),
            key=lambda site: site["title"].casefold())
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
            verified_keywords = {}
            verified_keywords_all_sites = {}
            for sid, entry in candidate_entries:
                subscribe = current.get(sid)
                if subscribe is None:
                    continue
                full_cache = self._cached_search(subscribe, [])
                full_keywords = ((full_cache or {}).get("diagnostics") or {}).get("resource_keywords") or {}
                keywords_by_site = {}
                for item in entry.get("candidates") or []:
                    site = item.get("site_id")
                    if site not in keywords_by_site:
                        try:
                            cached = self._cached_search(subscribe, [site]) if isinstance(site, int) else None
                        except ValueError:
                            # 历史候选的站点可能已移除；仍需允许配置页展示这条候选。
                            cached = None
                        keywords_by_site[site] = ((cached or {}).get("diagnostics") or {}).get("resource_keywords") or {}
                    verified_keywords[(sid, item["id"])] = [word for word in
                        keywords_by_site[site].get(item["id"], []) if isinstance(word, str) and word]
                    verified_keywords_all_sites[(sid, item["id"])] = [word for word in
                        full_keywords.get(item["id"], []) if isinstance(word, str) and word]
            choices = [
                {
                    "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                             f"{'本轮发现' if _found_latest_scan(item, entry) else '历史保留'} · "
                             f"{_candidate_label(item)} {_candidate_coverage(item)} · "
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · "
                             f"{_size_label(item.get('size_bytes'), exact=True)} · "
                             f"做种 {_seeders_label(item)} · 免费/促销：{item.get('promotion') or '未知'} · "
                             f"{item.get('quality') or '?'} {item.get('resolution') or '?'} "
                             f"{item.get('team') or ''} · {item.get('title')}",
                    "value": f"{sid}:{item['id']}",
                    "subscription_id": sid,
                    "kind": item.get("kind"),
                    "coverage": _candidate_coverage(item),
                    "size_label": _size_label(item.get("size_bytes"), exact=True),
                    "resource_title": item.get("title") or "",
                    "found_latest_scan": _found_latest_scan(item, entry),
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
                    "verified_keywords": verified_keywords.get((sid, item["id"]), []),
                    "verified_keywords_all_sites": verified_keywords_all_sites.get((sid, item["id"]), []),
                    "seeders_label": _seeders_label(item), "promotion": item.get("promotion") or "未知",
                    "original_sites": getattr(current.get(sid), "sites", None) or [],
                    "original_filter_groups": getattr(current.get(sid), "filter_groups", None) or [],
                    "team": item.get("release_team", item.get("team")), "team_pattern": candidate_team_pattern(item),
                }
                for sid, entry in candidate_entries
                for item in entry.get("candidates") or []
            ]
            keyword_choices = [
                {"title": "沿用原订阅搜索方式", "value": "original"},
                {"title": "使用剧名＋季号搜索词", "value": "seasonal"},
            ]
            pending_choices = [
                {"title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}", "value": sid}
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "pending"
            ]
            scan_choices = [item for item in pending_choices
                            if (subscribe := current.get(item["value"])) is not None
                            and subscribe.type == MediaType.TV.value and subscribe.state in {"N", "R", "P", "S"}
                            and self._state["entries"][item["value"]].get("identity") == _identity(subscribe)]
            approved_choices = [
                {"title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}", "value": sid}
                for sid, entry in self._state["entries"].items()
                if entry.get("status") == "approved"
            ]
            replacement_choices = [
                {
                    "title": f"#{sid} · {_candidate_label(item)} {_candidate_coverage(item)} · "
                             f"{item.get('site_name') or item.get('site_id') or '未知站点'} · "
                             f"{_size_label(item.get('size_bytes'), exact=True)} · "
                             f"做种 {_seeders_label(item)} · 免费/促销：{item.get('promotion') or '未知'} · {item.get('title')}",
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
                col(select("scan_subscription", "手动扫描已纳入的待确认剧集（无候选也可选）",
                           scan_choices, clearable=True,
                           **{"no-data-text": "暂无已纳入管理的待确认剧集"})),
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
                    {"title": "自选站点", "value": "custom"},
                    {"title": "不限站点", "value": "all_sites"},
                ]), md=3),
            ),
            row(col(select("rule_sites", "自选站点（可多选；仅自选站点范围生效）",
                           site_choices, clearable=True, multiple=True, chips=True,
                           **{"closable-chips": True}))),
            row(col(select("keyword_choice", "确认后的订阅搜索词", keyword_choices))),
            row(
                col(select("download_directory", "下载目录（留空保留原目录）", directory_choices(),
                           clearable=True, **{"hide-details": False,
                           "hint": "确认只回填资源根目录；完整分类路径在预览中显示，实际下载由 MoviePilot 处理",
                           "persistent-hint": True})),
            ),
            row(col(info(
                "一次性下载会提交所选种子的全部文件，包括跨季合集的所有季；不应用规则草稿，"
                "不批准或恢复订阅。只使用内存中的种子下载链接；缺少时请手动点击“重新扫描此剧集”，确认下载不会自动搜索。"
            ))),
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
                    {"title": "扫描所选已纳入剧集", "value": "scan"},
                    {"title": "预览候选规则", "value": "preview"},
                    {"title": "确认候选规则并恢复订阅", "value": "confirm"},
                    {"title": "一次性下载所选候选种子的全部文件", "value": "download_candidate"},
                    {"title": "直接批准订阅（保留原规则）", "value": "direct_approve"},
                    {"title": "撤销批准并暂停订阅", "value": "revoke"},
                    {"title": "预览可回刷资源", "value": "preview_replace"},
                    {"title": "提交所选资源和集数重新下载", "value": "submit_replace"},
                ])),
            ),
        ]}], {
            "enabled": False, "confirm_full_pack": True, "daily_notice": True,
            "notice_hour": 9, "notice_minute": 0,
            "historical_subscription": [], "scan_subscription": "", "candidate_subscription": "", "candidate": "",
            "rule_mode": "candidate", "rule_sites": [], "rule_json": "", "keyword_choice": "original",
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
            "text": (f"当前管理 {len(managed_rows)} 条剧集订阅；展开管理概览可查看状态和订阅进度。"
                     if managed_rows else "当前没有纳入管理的剧集订阅。"),
        }})
        if managed_rows:
            page.append({"component": "VTable", "_overview": {"title": f"管理概览 · {len(managed_rows)} 条"},
                         "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("订阅 ID", "剧集", "接管方式", "确认状态", "订阅进度")
                ]}]},
                {"component": "tbody", "content": managed_rows},
            ]})
        if not pending:
            page.append({"component": "VAlert", "props": {
                "type": "info", "text": "暂无待确认剧集。新增或手动纳入电视剧订阅后会收集首集、含首集多集资源、整季及含目标季的跨季候选。",
            }})
        for sid, entry in pending:
            groups = _display_groups(entry.get("candidates") or [])
            section_nodes = []
            section_nodes.append({"component": "VAlert", "props": {
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
                section_nodes.append({"component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "本条订阅最近扫描有失败或暂缓的站点，仅本轮停止后续查询，结果可能不完整："
                            + "；".join(f"{item['site_name']}：{item['reason']}" for item in failures),
                }})
            rows = [
                {"component": "tr", "_sort": _candidate_row_sort(group), "content": [
                    {"component": "td", "text": _candidate_label(item)},
                    {"component": "td", "text": _candidate_coverage(item)},
                    _site_cell(group),
                    {"component": "td", "text": item.get("title") or ""},
                    _size_cell(item),
                    _availability_cell(group, "seeders"),
                    _availability_cell(group, "promotion"),
                    {"component": "td", "text": item.get("quality") or "?"},
                    {"component": "td", "text": item.get("resolution") or "?"},
                    {"component": "td", "text": item.get("effect") or "?"},
                    {"component": "td", "text": item.get("team") or "?"},
                    {"component": "td", "text": (
                        max((member.get("last_seen") or "" for member in group), default="") + " · "
                        + ("本轮发现" if all(_found_latest_scan(member, entry) for member in group)
                           else "历史保留" if all(not _found_latest_scan(member, entry) for member in group)
                           else "本轮及历史")
                    )},
                    _candidate_action_cell(sid, group),
                ]}
                for group in groups
                for item in group[:1]
            ]
            section_nodes.append({"component": "VTable", "_candidate_table": True,
                                  "props": {"hover": True}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": title}
                    for title in ("类型", "识别集号/目标总集数", "站点/详情", "资源", "大小", "做种人数", "免费/促销", "质量", "分辨率", "特效", "制作组", "最后发现/扫描状态", "操作")
                ]}]},
                {"component": "tbody", "content": rows},
            ]})
            direct_history = entry.get("direct_download_history") or []
            if direct_history:
                latest = direct_history[0]
                section_nodes.append({"component": "VAlert", "props": {
                    "type": "success", "variant": "tonal",
                    "text": f"最近一次性整包下载提交：{latest.get('submitted_at') or '?'} · "
                            f"{latest.get('site_name') or '?'} · {latest.get('title') or '?'} · "
                            f"{_size_label(latest.get('size_bytes'), exact=True)}。订阅仍待确认。",
                }})
            section = {"id": sid, "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}",
                       "status": "待确认", "candidate_count": len(entry.get("candidates") or []),
                       "row_count": len(groups), "warning": bool(failures or entry.get("last_error"))}
            page.extend({**node, "_subscription": section} for node in section_nodes)
        if approved:
            page.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": "已批准订阅可在配置页撤销批准；规则会保留，订阅会暂停。",
            }})
        for sid, entry in approved:
            mode = "候选规则回填" if entry.get("mode") == "candidate" else "直接批准"
            section_nodes = []
            section_nodes.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal",
                "text": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d} · "
                        f"{mode} · 批准于 {entry.get('approved_at') or '?'}",
            }})
            direct_history = entry.get("direct_download_history") or []
            if direct_history:
                latest = direct_history[0]
                section_nodes.append({"component": "VAlert", "props": {
                    "type": "success", "variant": "tonal",
                    "text": f"此前一次性整包下载提交：{latest.get('submitted_at') or '?'} · "
                            f"{latest.get('site_name') or '?'} · {latest.get('title') or '?'}。",
                }})
            history = entry.get("replacement_history") or []
            if history:
                latest = history[0]
                section_nodes.append({"component": "VAlert", "props": {
                    "type": "success", "variant": "tonal",
                    "text": f"最近手动提交：{latest.get('submitted_at') or '?'} · "
                            f"{latest.get('title') or '?'} · 集数 {latest.get('episodes') or []}",
                }})
            section = {"id": sid, "title": f"#{sid} {entry.get('name')} S{int(entry.get('season') or 1):02d}",
                       "status": "已批准", "candidate_count": 0, "row_count": 0, "warning": False}
            page.extend({**node, "_subscription": section} for node in section_nodes)
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
        """注册订阅进度检查、候选扫描和每日待确认订阅通知。"""
        if not self._enabled:
            return []
        services = [{
            "id": "candidate_refresh", "name": "更新待确认剧集版本候选",
            "trigger": "interval", "func": self.refresh_candidates,
            "kwargs": {"hours": 4},
        }, {
            "id": "subscription_progress_check", "name": "检查待确认剧集进度",
            "trigger": "interval", "func": self.approve_progress_complete,
            "kwargs": {"minutes": 15},
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
        停止插件的一次性进度检查、候选扫描和确认后补搜任务。
        """
        with self._lock:
            self._search_stop.set()
            if self._preview_search_cache:
                logger.info(f"搜索缓存清理：停止服务，释放内存缓存 {len(self._preview_search_cache)} 组；磁盘记录保留")
            self._preview_search_cache.clear()
            self._search_cache_loaded = False
            self._queued_scan_ids.clear()
            self._queued_progress_ids.clear()
        try:
            remove_plugin_once_job(self.__class__.__name__, "collect")
            remove_plugin_once_job(self.__class__.__name__, "progress-check")
        except RuntimeError:
            return
        with self._lock:
            approved = [key for key, entry in self._state["entries"].items() if entry.get("needs_search")]
        for sid in approved:
            try:
                remove_plugin_once_job(self.__class__.__name__, f"search-{sid}")
            except RuntimeError:
                return

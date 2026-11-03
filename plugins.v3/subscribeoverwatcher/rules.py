"""
从首集搜索结果提取可保存的候选信息和订阅规则。

只保存展示与回填所需的字段，不保存种子链接、站点 Cookie 或完整搜索上下文。
"""

import hashlib
import re
from typing import Any, Optional


def _short(value: Any, limit: int = 100) -> str:
    """
    将搜索结果中的展示字段限制到固定长度。
    """
    return str(value or "").strip()[:limit]


def _matching_pattern(value: Any, title: str, patterns: tuple[tuple[str, str], ...]) -> Optional[str]:
    """
    将识别到的规格转换成可匹配所选资源标题的正则。
    """
    raw = _short(value, 80)
    if not raw:
        return None
    for marker, pattern in patterns:
        if re.search(marker, raw, re.I) and re.search(pattern, title, re.I):
            return pattern
    escaped = re.escape(raw)
    return escaped if re.search(escaped, title, re.I) else None


def _effect_pattern(value: Any, title: str) -> Optional[str]:
    """
    优先选择首集标题中可验证的特效标识。
    """
    return _matching_pattern(
        value,
        title,
        (
            (r"Dolby.?Vision|DoVi|\bDV\b", r"Dolby[ ._-]?Vision|DoVi|(?:^|[ ._\[-])DV(?:$|[ ._\]-])"),
            (r"HDR10\+|HDR10Plus", r"HDR10\+|HDR10Plus"),
            (r"HDR", r"HDR"),
        ),
    )


def _is_single_first_episode(meta: Any, season: int) -> bool:
    """
    仅接纳目标季中明确识别为单独 E01 的资源。
    """
    if meta is None:
        return False
    begin_season = getattr(meta, "begin_season", None)
    end_season = getattr(meta, "end_season", None)
    if begin_season is None and season != 1:
        return False
    if begin_season not in (None, season) or end_season not in (None, season):
        return False
    if getattr(meta, "end_episode", None) not in (None, 1):
        return False
    episodes = getattr(meta, "episode_list", None) or []
    if episodes:
        return set(episodes) == {1}
    return getattr(meta, "begin_episode", None) == 1 and getattr(meta, "end_episode", None) in (None, 1)


def _episode_numbers(meta: Any) -> list[int]:
    """
    只保留识别结果中明确的正整数集数。
    """
    episodes = getattr(meta, "episode_list", None) or []
    return sorted({item for item in episodes if isinstance(item, int) and not isinstance(item, bool) and item > 0})


def _single_target_season(meta: Any, season: int) -> bool:
    """
    排除跨季包与目标季不一致的候选。
    """
    seasons = getattr(meta, "season_list", None) or [1]
    return seasons == [season] and getattr(meta, "begin_season", None) in (None, season) \
        and getattr(meta, "end_season", None) in (None, season)


def _candidate_kind(meta: Any, title: str, season: int, total_episode: Optional[int]) -> Optional[str]:
    """
    区分识别集数覆盖全集、疑似全集和单集 E01。
    """
    if not _single_target_season(meta, season):
        return None
    episodes = _episode_numbers(meta)
    if isinstance(total_episode, int) and not isinstance(total_episode, bool) and total_episode > 0:
        if set(range(1, total_episode + 1)).issubset(episodes):
            return "full_season"
    if re.search(r"全集|全季|完结|\bCOMPLETE\b|\bSEASON[ ._-]?PACK\b|\bS\d{1,2}[ ._-]?(?:PACK|COMPLETE)\b", title, re.I):
        return "suspected_full"
    return "first_episode" if _is_single_first_episode(meta, season) else None


def resource_candidate(
    context: Any, season: int, seen_at: str, total_episode: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    从搜索结果生成安全的 E01 或完整季候选快照。

    :param context: 宿主返回的资源上下文
    :param season: 订阅季数
    :param seen_at: 本轮扫描的 UTC 时间
    :param total_episode: 订阅目标季的总集数；未知时全集只标记为疑似
    :return: 可保存的候选字段；不符合目标季的资源返回空
    """
    meta = getattr(context, "meta_info", None)
    torrent = getattr(context, "torrent_info", None)
    if torrent is None or meta is None:
        return None
    title = _short(getattr(torrent, "title", None), 240)
    if not title:
        return None
    kind = _candidate_kind(meta, title, season, total_episode)
    if kind is None:
        return None
    site = getattr(torrent, "site", None)
    site_id = site if isinstance(site, int) and site > 0 else None
    size = getattr(torrent, "size", None)
    size_bytes = int(size) if isinstance(size, (int, float)) and size > 0 else None
    digest = hashlib.sha256(f"{site_id}|{title}|{size_bytes}".encode("utf-8")).hexdigest()[:20]
    quality = _short(getattr(meta, "resource_type", None), 80)
    resolution = _short(getattr(meta, "resource_pix", None), 80)
    effect = _short(getattr(meta, "resource_effect", None), 80)
    team = _short(getattr(meta, "resource_team", None), 80)
    customization = _short(getattr(meta, "customization", None), 80)
    include_word = next(
        (word for word in (team, customization) if word and re.search(re.escape(word), title, re.I)),
        None,
    )
    include = re.escape(include_word) if include_word else None
    return {
        "id": digest,
        "kind": kind,
        "episodes": _episode_numbers(meta),
        "total_episode": total_episode if isinstance(total_episode, int) and total_episode > 0 else None,
        "title": title,
        "site_id": site_id,
        "site_name": _short(getattr(torrent, "site_name", None), 80),
        "size_bytes": size_bytes,
        "pubdate": _short(getattr(torrent, "pubdate", None), 40),
        "quality": quality,
        "resolution": resolution,
        "effect": effect,
        "team": team or customization,
        "rules": {
            "quality": _matching_pattern(
                quality, title,
                (
                    (r"WEB.?DL|WEB.?RIP", r"WEB[- .]?(?:DL|RIP)"),
                    (r"REMUX", r"REMUX"),
                    (r"Blu.?Ray", r"Blu[- .]?Ray|BluRay"),
                    (r"HDTV", r"HDTV"),
                ),
            ),
            "resolution": _matching_pattern(
                resolution, title,
                (
                    (r"2160|4K", r"2160[pi]?|4K"),
                    (r"1080", r"1080[pi]?"),
                    (r"720", r"720[pi]?"),
                ),
            ),
            "effect": _effect_pattern(effect, title),
            "include": include,
            "sites": [site_id] if site_id else None,
        },
        "first_seen": seen_at,
        "last_seen": seen_at,
    }


def merge_candidates(
    old: list[dict[str, Any]], fresh: list[dict[str, Any]], limit: int = 80,
) -> list[dict[str, Any]]:
    """
    合并扫描记录，保留已消失版本的首次与最后发现时间。

    :param old: 先前保存的候选
    :param fresh: 本轮找到的候选
    :param limit: 每条订阅最多保存的候选数
    :return: 按最近发现时间排序的候选
    """
    merged = {item["id"]: item for item in old if isinstance(item, dict) and item.get("id")}
    for candidate in fresh:
        previous = merged.get(candidate["id"])
        if previous:
            candidate["first_seen"] = previous.get("first_seen") or candidate["first_seen"]
        merged[candidate["id"]] = candidate
    kind_order = {"full_season": 2, "suspected_full": 1, "first_episode": 0}
    return sorted(
        merged.values(),
        key=lambda item: (kind_order.get(item.get("kind"), 0), item.get("last_seen") or ""),
        reverse=True,
    )[:limit]


def is_complete_season_download(event_data: Any, subscribe: Any) -> bool:
    """
    只放行种子资源明确覆盖当前季全部集数的订阅下载事件。

    事件发生在宿主写入 ``confirmed_full_coverage`` 之前，因此用已识别的资源集数
    判断种子是否为全集。选中的文件可以只是缺集；无法得知总集数时保持拦截。

    :param event_data: 宿主的资源下载事件数据
    :param subscribe: 当前订阅，用其季数和总集数确定完整范围
    :return: 资源覆盖整季且本次选中集数属于该资源时返回 True
    """
    total = getattr(subscribe, "total_episode", None)
    season = getattr(subscribe, "season", None)
    if (not isinstance(total, int) or isinstance(total, bool) or total < 1
            or not isinstance(season, int) or isinstance(season, bool) or season < 1):
        return False
    context = getattr(event_data, "context", None)
    meta = getattr(context, "meta_info", None)
    if meta is None or getattr(context, "torrent_info", None) is None:
        return False
    seasons = getattr(meta, "season_list", None) or [1]
    if seasons != [season]:
        return False
    recognized = getattr(meta, "episode_list", None) or []
    selected = getattr(event_data, "episodes", None) or []
    try:
        expected = set(range(1, total + 1))
        resource_episodes = set(recognized)
        selected_episodes = set(selected)
        return bool(selected_episodes) and expected.issubset(resource_episodes) \
            and selected_episodes.issubset(resource_episodes)
    except (TypeError, ValueError):
        return False

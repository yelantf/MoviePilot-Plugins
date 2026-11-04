"""
从目标季搜索结果提取可保存的候选信息和订阅规则。

只保存展示与回填所需的字段及不含访问票据的详情页地址，不保存种子下载链接、站点 Cookie 或完整搜索上下文。
"""

import hashlib
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_PAGE_ID_KEYS = {"id", "tid", "torrentid", "torrent_id"}
_SECRET_PATH_SEGMENT = re.compile(
    r"^(?:(?=[0-9]*[A-Fa-f])[0-9A-Fa-f]{16,}|(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9]{24,})$"
)


def _short(value: Any, limit: int = 100) -> str:
    """
    将搜索结果中的展示字段限制到固定长度。
    """
    return str(value or "").strip()[:limit]


def public_page_url(page_url: Any, enclosure: Any = None) -> Optional[str]:
    """从宿主的资源详情页地址中移除访问票据，不保存下载地址。"""
    if not isinstance(page_url, str):
        return None
    value = page_url.strip()
    if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    if isinstance(enclosure, str) and value == enclosure.strip():
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname \
                or parts.username is not None or parts.password is not None or parts.port == 0:
            return None
    except ValueError:
        return None
    segments = [segment.split(".", 1)[0] for segment in parts.path.split("/") if segment]
    if any(_SECRET_PATH_SEGMENT.fullmatch(segment) for segment in segments):
        return None
    if any(segment.lower() in {"download", "dl", "gettorrent"} for segment in segments) \
            or parts.path.lower().endswith((".torrent", "/download.php", "/dl.php", "/gettorrent.php")):
        return None
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        name = key.lower()
        if name in _PAGE_ID_KEYS and re.fullmatch(r"\d{1,20}", item):
            query.append((key, item))
        elif name in {"hit", "https"} and item in {"0", "1"}:
            query.append((key, item))
        elif name == "type" and re.fullmatch(r"[A-Za-z0-9_-]{1,20}", item):
            query.append((key, item))
    fragment = parts.fragment if re.fullmatch(r"/[A-Za-z0-9/_-]*", parts.fragment) else ""
    if parts.path in {"", "/"} and not query and not fragment:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", urlencode(query), fragment))


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
    优先选择资源标题中可验证的画面或音效标识。
    """
    return _matching_pattern(
        value,
        title,
        (
            (r"Dolby.?Vision|DoVi|\bDV\b", r"Dolby[ ._-]?Vision|DoVi|(?:^|[ ._\[-])DV(?:$|[ ._\]-])"),
            (r"HDR10\+|HDR10Plus", r"HDR10\+|HDR10Plus"),
            (r"HDR", r"HDR"),
            (r"Dolby.?Atmos|Atmos", r"Dolby[ ._-]?Atmos|Atmos"),
            (r"SDR", r"(?:^|[ ._\[-])SDR(?:$|[ ._\]-])"),
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


def target_seasons(meta: Any, season: int) -> list[int]:
    """
    返回包含目标季且季数元数据自洽的资源季列表。
    """
    if meta is None:
        return []
    raw = getattr(meta, "season_list", None) or [1]
    if not isinstance(raw, (list, tuple, set)) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in raw
    ):
        return []
    seasons = sorted(set(raw))
    begin = getattr(meta, "begin_season", None)
    end = getattr(meta, "end_season", None)
    if season not in seasons or (begin is not None and begin not in seasons) \
            or (end is not None and end not in seasons):
        return []
    if begin is not None and end is not None and begin > end:
        return []
    return seasons


def _title_starts_after_first_episode(title: str) -> bool:
    """标题显式写出 S01E11 等后续集数时，不依赖宿主集数列表。"""
    numbers = [int(number) for number in re.findall(
        r"(?<![A-Za-z0-9])S\d{1,2}[ ._-]*E(?:P)?0*(\d{1,3})(?!\d)", title, re.I,
    )]
    return bool(numbers) and 1 not in numbers and all(number > 1 for number in numbers)


def _candidate_kind(meta: Any, title: str, season: int, total_episode: Optional[int]) -> Optional[str]:
    """
    区分跨季资源、识别集数覆盖全集、疑似全集和单集 E01。
    """
    seasons = target_seasons(meta, season)
    if not seasons:
        return None
    if _title_starts_after_first_episode(title):
        return None
    if len(seasons) > 1:
        # 集号列表没有按季拆分，不能据此断言目标季覆盖全集或只有 E01。
        return "cross_season"
    episodes = _episode_numbers(meta)
    if isinstance(total_episode, int) and not isinstance(total_episode, bool) and total_episode > 0:
        if set(range(1, total_episode + 1)).issubset(episodes):
            return "full_season"
    if _is_single_first_episode(meta, season):
        return "first_episode"
    if re.search(r"全集|全季|完结|\bCOMPLETE\b|\bSEASON[ ._-]?PACK\b|\bS\d{1,2}[ ._-]?(?:PACK|COMPLETE)\b", title, re.I):
        # 标题明确标出后续集数时，整季字样不能把单集误判为整季候选。
        begin_episode = getattr(meta, "begin_episode", None)
        if (episodes and 1 not in episodes) or (not episodes and isinstance(begin_episode, int)
                                                 and not isinstance(begin_episode, bool) and begin_episode > 1):
            return None
        return "suspected_full"
    return None


def candidate_is_eligible(candidate: Any) -> bool:
    """排除旧状态中已知只覆盖后续集数的疑似整季候选。"""
    if not isinstance(candidate, dict) or not candidate.get("id"):
        return False
    if _title_starts_after_first_episode(str(candidate.get("title") or "")):
        return False
    if candidate.get("kind") != "suspected_full":
        return True
    episodes = candidate.get("episodes")
    if not isinstance(episodes, (list, tuple, set)):
        episodes = []
    recognized = {episode for episode in episodes
                  if isinstance(episode, int) and not isinstance(episode, bool) and episode > 0}
    return not recognized or 1 in recognized


def resource_candidate(
    context: Any, season: int, seen_at: str, total_episode: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    从搜索结果生成安全的目标季候选快照。

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
    if getattr(context, "match_status", None) not in (None, "exact"):
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
    has_team = bool(team and re.search(re.escape(team), title, re.I))
    has_customization = bool(customization and re.search(re.escape(customization), title, re.I))
    if has_team and has_customization and team.casefold() != customization.casefold():
        include = f"(?=.*{re.escape(customization)})(?=.*{re.escape(team)})"
    elif has_team:
        include = re.escape(team)
    elif has_customization:
        include = re.escape(customization)
    else:
        include = None
    return {
        "id": digest,
        "kind": kind,
        "seasons": target_seasons(meta, season),
        "target_season": season,
        "episodes": _episode_numbers(meta),
        "total_episode": total_episode if isinstance(total_episode, int) and total_episode > 0 else None,
        "title": title,
        "site_id": site_id,
        "site_name": _short(getattr(torrent, "site_name", None), 80),
        "page_url": public_page_url(getattr(torrent, "page_url", None), getattr(torrent, "enclosure", None)),
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
                    (r"UHD|Ultra.?HD", r"UHD|Ultra[- .]?HD"),
                    (r"HDTV", r"HDTV"),
                    (r"MiniBD", r"MiniBD"),
                    (r"BDRIP", r"BD[- .]?RIP"),
                    (r"HEVC|[Hx].?265", r"HEVC|[Hx][ ._-]?265"),
                    (r"AVC|[Hx].?264", r"AVC|[Hx][ ._-]?264"),
                ),
            ),
            "resolution": _matching_pattern(
                resolution, title,
                (
                    (r"2160|4K", r"2160[pi]?|4K|x2160"),
                    (r"1080", r"1080[pi]?|x1080"),
                    (r"720", r"720[pi]?|x720"),
                    (r"480", r"480[pi]?|x480"),
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
    merged = {item["id"]: item for item in old if candidate_is_eligible(item)}
    for candidate in fresh:
        if not candidate_is_eligible(candidate):
            continue
        previous = merged.get(candidate["id"])
        if previous:
            candidate["first_seen"] = previous.get("first_seen") or candidate["first_seen"]
            candidate["page_url"] = candidate.get("page_url") or public_page_url(previous.get("page_url"))
        merged[candidate["id"]] = candidate
    kind_order = {"full_season": 3, "cross_season": 2, "suspected_full": 1, "first_episode": 0}
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

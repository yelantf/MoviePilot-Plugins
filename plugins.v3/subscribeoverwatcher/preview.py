"""
剧集守门员的规则草稿与只读预览数据。

这里仅处理可序列化的筛选条件、集数和展示快照，不保存种子下载凭据。
"""

import json
import re
from hashlib import sha256
from typing import Any, Optional

from .rules import public_page_url, resource_candidate, target_seasons


RULE_FIELDS = ("quality", "resolution", "effect", "include", "exclude", "sites")


def normalize_draft_rules(defaults: dict[str, Any], raw: str) -> dict[str, Any]:
    """
    将候选规格与用户编辑的 JSON 合并为可安全回填的规则。

    :param defaults: 候选资源提取出的默认规则
    :param raw: 用户提供的 JSON；空字符串表示完全采用候选默认规则
    :return: 经过字段与正则校验的规则
    :raises ValueError: JSON 结构、字段或规则格式无效
    """
    try:
        changes = json.loads(raw) if raw.strip() else {}
    except (TypeError, ValueError) as error:
        raise ValueError("规则 JSON 格式错误") from error
    if not isinstance(changes, dict) or set(changes) - set(RULE_FIELDS):
        raise ValueError("规则 JSON 只能包含质量、分辨率、特效、包含、排除和站点字段")
    # 未识别出的候选属性不应清空订阅已有规则；用户仍可显式提交 null 清空。
    result = {key: value for key, value in defaults.items() if key in RULE_FIELDS and value is not None}
    result.update(changes)
    for key in RULE_FIELDS:
        if key not in result:
            continue
        value = result[key]
        if key == "sites":
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value)
            ):
                raise ValueError("sites 必须是正整数站点 ID 列表，空列表表示所有站点")
            continue
        if value is not None and (not isinstance(value, str) or len(value) > 240):
            raise ValueError(f"{key} 必须是长度不超过 240 的正则字符串或 null")
        if value:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"{key} 的正则表达式无效：{error}") from error
    return result


def parse_episode_selection(value: str, total_episode: Optional[int]) -> set[int]:
    """
    解析用户明确选择的回刷集数，支持逗号分隔与闭区间。

    :param value: 例如 ``1,3-5``
    :param total_episode: 已知的目标季总集数
    :return: 需要重新下载的集数集合
    :raises ValueError: 输入为空、越界或范围过大
    """
    if not isinstance(total_episode, int) or isinstance(total_episode, bool) or total_episode < 1:
        raise ValueError("目标季总集数未知，暂不能指定回刷集数")
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError("请输入要回刷的集数，例如 1,3-5")
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if match is None:
            raise ValueError("集数格式错误，请使用 1,3-5")
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first < 1 or last < first or last > total_episode or last - first > 100:
            raise ValueError("回刷集数超出订阅目标范围")
        selected.update(range(first, last + 1))
    if not selected:
        raise ValueError("请先选择要回刷的集数")
    return selected


def preview_resource(context: Any, season: int, total_episode: Optional[int], seen_at: str) -> Optional[dict[str, Any]]:
    """
    为当前搜索结果生成不含下载链接和 Cookie 的规则预览行。

    :param context: 搜索返回的资源上下文
    :param season: 目标季数
    :param total_episode: 已知的目标季总集数
    :param seen_at: 预览时间
    :return: 目标季资源的安全展示字段
    """
    meta = getattr(context, "meta_info", None)
    torrent = getattr(context, "torrent_info", None)
    if meta is None or torrent is None:
        return None
    if getattr(context, "match_status", None) not in (None, "exact"):
        return None
    seasons = target_seasons(meta, season)
    if not seasons:
        return None
    candidate = resource_candidate(context, season, seen_at, total_episode)
    if candidate:
        return {key: candidate[key] for key in (
            "id", "kind", "seasons", "target_season", "episodes", "total_episode",
            "title", "site_id", "site_name", "size_bytes",
            "page_url", "quality", "resolution", "effect", "team",
        )}
    title = str(getattr(torrent, "title", None) or "").strip()[:240]
    if not title:
        return None
    site = getattr(torrent, "site", None)
    size = getattr(torrent, "size", None)
    size_bytes = int(size) if isinstance(size, (int, float)) and size > 0 else None
    site_id = site if isinstance(site, int) and not isinstance(site, bool) and site > 0 else None
    episodes = sorted({item for item in (getattr(meta, "episode_list", None) or [])
                       if isinstance(item, int) and not isinstance(item, bool) and item > 0})
    return {
        "id": sha256(f"{site_id}|{title}|{size_bytes}".encode("utf-8")).hexdigest()[:20],
        "kind": "episode", "seasons": seasons, "target_season": season,
        "episodes": episodes, "total_episode": total_episode,
        "title": title, "site_id": site_id,
        "site_name": str(getattr(torrent, "site_name", None) or "")[:80],
        "page_url": public_page_url(getattr(torrent, "page_url", None), getattr(torrent, "enclosure", None)),
        "size_bytes": size_bytes,
        "quality": str(getattr(meta, "resource_type", None) or "")[:80],
        "resolution": str(getattr(meta, "resource_pix", None) or "")[:80],
        "effect": str(getattr(meta, "resource_effect", None) or "")[:80],
        "team": str(getattr(meta, "resource_team", None) or "")[:80],
    }

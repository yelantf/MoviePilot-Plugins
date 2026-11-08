"""
剧集守门员的规则草稿与只读预览数据。

这里仅处理可序列化的筛选条件、集数和展示快照，不保存种子下载凭据。
"""

import json
import re
from hashlib import sha256
from typing import Any, Optional

from .rules import candidate_rule_names, public_page_url, resource_availability, resource_candidate, target_seasons
from .rulegroups import UNFILTERED_GROUP_NAME


RULE_FIELDS = ("quality", "resolution", "effect", "include", "exclude", "sites", "filter_groups")
RULE_LABELS = {"quality": "质量", "resolution": "分辨率", "effect": "特效",
               "include": "包含", "exclude": "排除", "sites": "站点", "filter_groups": "规则组",
               "tv_size": "剧集大小", "movie_size": "电影大小", "size": "大小",
               "min_seeders": "最少做种人数", "min_seeders_time": "做种检查时间"}

# 与 MoviePilot-Frontend v3/src/api/constants.ts 的订阅筛选选项一致。
SUBSCRIPTION_RULE_OPTIONS = {
    "quality": [
        {"title": "全部", "value": None},
        {"title": "蓝光原盘", "value": "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD"},
        {"title": "Remux", "value": "Remux"}, {"title": "BluRay", "value": "Blu-?Ray"},
        {"title": "UHD", "value": "UHD|UltraHD"}, {"title": "WEB-DL", "value": "WEB-?DL|WEB-?RIP"},
        {"title": "HDTV", "value": "HDTV"}, {"title": "H265", "value": "[Hx].?265|HEVC"},
        {"title": "H264", "value": "[Hx].?264|AVC"},
    ],
    "resolution": [
        {"title": "全部", "value": None}, {"title": "4K", "value": "4K|2160p|x2160"},
        {"title": "1080p", "value": "1080[pi]|x1080"}, {"title": "720p", "value": "720[pi]|x720"},
    ],
    "effect": [
        {"title": "全部", "value": None},
        {"title": "杜比视界", "value": r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+"},
        {"title": "杜比全景声", "value": r"Dolby[\s.]*\+?Atmos|Atmos"},
        {"title": "HDR", "value": r"[\s.]+HDR[\s.]+|HDR10|HDR10\+"},
        {"title": "SDR", "value": r"[\s.]+SDR[\s.]+"},
    ],
}

_QUALITY_OPTION_NAMES = {
    r"WEB[- .]?(?:DL|RIP)": "WEB-DL", r"REMUX": "Remux",
    r"Blu[- .]?Ray|BluRay": "BluRay", r"UHD|Ultra[- .]?HD": "UHD",
    r"HDTV": "HDTV", r"MiniBD": "蓝光原盘",
    r"HEVC|[Hx][ ._-]?265": "H265", r"AVC|[Hx][ ._-]?264": "H264",
}


def candidate_default_rules(candidate: dict[str, Any]) -> dict[str, Any]:
    """常见候选质量优先采用内置规则，仍须能匹配所选资源标题。"""
    rules = dict(candidate.get("rules") or {})
    name = _QUALITY_OPTION_NAMES.get(rules.get("quality"))
    option = next((item for item in SUBSCRIPTION_RULE_OPTIONS["quality"] if item["title"] == name), None)
    if option and re.search(option["value"], candidate.get("title") or "", re.I):
        rules["quality"] = option["value"]
    return rules


def normalize_draft_rules(
    defaults: dict[str, Any], raw: str, names: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
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
        raise ValueError("规则 JSON 只能包含质量、分辨率、特效、包含、排除、站点和规则组字段")
    # 未识别出的候选属性不应清空订阅已有规则；用户仍可显式提交 null 清空。
    result = {key: value for key, value in defaults.items() if key in RULE_FIELDS and value is not None}
    result.update(changes)
    for key in RULE_FIELDS:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, dict):
            if set(value) == {"subscription"} and value["subscription"] is True and key != "sites":
                result.pop(key)
                continue
            if set(value) != {"candidate"} or not names or not names.get(key) \
                    or value["candidate"] != names[key] or not defaults.get(key):
                raise ValueError(f"{key} 的候选规则引用已失效，请重新选择版本并重新预览")
            value = result[key] = defaults[key]
        if key == "filter_groups":
            if value != []:
                raise ValueError("规则组只能选择保留原订阅或不使用规则组")
            continue
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


def editor_draft_rules(candidate: dict[str, Any], rules: dict[str, Any], raw: str) -> dict[str, Any]:
    """保留选项面板中的“保留原订阅”选择，兼容原始 JSON 草稿。"""
    displayed = display_draft_rules(candidate, rules)
    changes = json.loads(raw) if raw.strip() else {}
    displayed.update({field: value for field, value in changes.items()
                      if value == {"subscription": True} and field not in rules})
    return displayed


def display_draft_rules(candidate: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """把与所选候选完全一致的规则显示成名称引用，自定义规则保留原文。"""
    defaults = candidate_default_rules(candidate)
    names = candidate_rule_names(candidate)
    return {
        field: {"candidate": names[field]}
        if field in names and value and value == defaults.get(field)
        and not any(item["value"] == value for item in SUBSCRIPTION_RULE_OPTIONS.get(field, [])) else value
        for field, value in rules.items()
    }


def rule_summary(
    params: dict[str, Any], sites: list[int], candidate: Optional[dict[str, Any]] = None,
    *, working: Any = None, rules: Optional[dict[str, Any]] = None,
    group_policy: Optional[dict[str, Any]] = None,
) -> list[dict[str, str]]:
    """展示本次筛选的实际条件，包括宿主默认条件。"""
    displayed = display_draft_rules(candidate or {}, params)
    rules = rules or {}
    summary = [{"field": "sites", "label": "站点", "value": ", ".join(map(str, sites)) or "不限站点",
                "source": "候选 / 草稿" if rules.get("sites") is not None else "原订阅"}]
    groups = getattr(working, "filter_groups", None) or []
    if rules.get("filter_groups") == []:
        group_label, group_source = "不使用规则组（确认后生效）", "草稿"
    elif groups == [UNFILTERED_GROUP_NAME]:
        group_label, group_source = "不使用规则组", "原订阅"
    elif groups:
        group_label, group_source = "、".join(groups), "原订阅"
    elif group_policy is not None:
        group_label = "、".join(group_policy["names"]) or "未配置规则组"
        group_source = group_policy["source"]
    else:
        group_label, group_source = "使用宿主默认规则组", "宿主默认"
    summary.append({"field": "filter_groups", "label": "规则组", "value": group_label, "source": group_source})
    defaults = candidate_default_rules(candidate or {})
    for field, value in displayed.items():
        if field == "filter_groups":
            continue
        if value is None or value == "":
            continue
        if isinstance(value, dict) and "candidate" in value:
            text = value["candidate"]
        elif field in SUBSCRIPTION_RULE_OPTIONS and (option := next(
            (item for item in SUBSCRIPTION_RULE_OPTIONS[field] if item["value"] == value), None,
        )):
            text = option["title"]
        elif field == "quality" and isinstance(value, str) and value in _QUALITY_OPTION_NAMES:
            text = f"{_QUALITY_OPTION_NAMES[value]}（候选匹配方式）"
        elif field in RULE_FIELDS:
            text = f"自定义正则：{value}"
        else:
            text = str(value)
        source = "宿主默认"
        if working is not None and getattr(working, field, None):
            source = "原订阅"
            if field in rules and rules[field] == params[field]:
                source = "候选规格" if field in defaults and params[field] == defaults[field] else "草稿"
        summary.append({"field": field, "label": RULE_LABELS.get(field, field), "value": text, "source": source})
    return summary


def readable_rejection(reason: str, summary: list[dict[str, str]]) -> str:
    """沿用宿主首个失败条件，用生效条件名称解释包含和排除的含义。"""
    for item in summary:
        field, label = item["field"], item["label"]
        if reason.startswith((f"不符合{label}条件", f"不符合{field}条件")):
            prefix = {"include": "缺少包含标识", "exclude": "命中排除规则"}.get(field, f"不符合{label}")
            return f"{prefix}：{item['value']}"
    return reason


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
            "title", "site_id", "site_name", "size_bytes", "seeders", "promotion",
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
        **resource_availability(torrent),
        "quality": str(getattr(meta, "resource_type", None) or "")[:80],
        "resolution": str(getattr(meta, "resource_pix", None) or "")[:80],
        "effect": str(getattr(meta, "resource_effect", None) or "")[:80],
        "team": str(getattr(meta, "resource_team", None) or "")[:80],
    }

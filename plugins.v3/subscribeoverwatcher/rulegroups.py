"""为显式选择不使用规则组的订阅提供全放行组，避免空列表回退宿主默认组。"""

import copy
import hashlib
import json
import threading
from typing import Any


UNFILTERED_GROUP_NAME = "剧集守门员·不使用规则组"
# 宿主内置 WEBDL 的布尔恒真式，保留合法规则表达式及正常优先级计算。
UNFILTERED_GROUP = {
    "name": UNFILTERED_GROUP_NAME, "rule_string": "WEBDL | !WEBDL", "media_type": None, "category": None,
}
_group_lock = threading.RLock()


def preview_group_policy(subscribe: Any, *, disabled: bool = False) -> dict[str, Any]:
    """按宿主的订阅优先级选择规则组，并冻结相关定义以核对预览。"""
    original = getattr(subscribe, "filter_groups", None) or []
    if disabled or original == [UNFILTERED_GROUP_NAME]:
        return {"names": [], "source": "草稿" if disabled else "原订阅", "signature": None}
    from app.db.oper.systemconfig import SystemConfigOper
    from app.schemas.types import SystemConfigKey

    oper = SystemConfigOper()
    key = (SystemConfigKey.BestVersionFilterRuleGroups if getattr(subscribe, "best_version", False)
           else SystemConfigKey.SubscribeFilterRuleGroups)
    names = list(original or oper.get(key) or [])
    source = "原订阅" if original else "宿主默认"
    definitions = oper.get(SystemConfigKey.UserFilterRuleGroups) or []
    signature = hashlib.sha256(json.dumps({
        "names": names,
        "groups": [group for group in definitions if group.get("name") in names],
        "custom_rules": oper.get(SystemConfigKey.CustomFilterRules) or [],
    }, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest() if names else None
    return {"names": names, "source": source, "signature": signature}


def group_rejections(contexts: list[Any], policy: dict[str, Any]) -> dict[int, str]:
    """使用宿主筛选器逐站核对规则组；只传入副本，避免污染共享搜索缓存。"""
    names = policy["names"]
    if not names or not contexts:
        return {}
    from app.chain.search import SearchChain

    batches: dict[Any, list[Any]] = {}
    for context in contexts:
        batches.setdefault(context.torrent_info.site, []).append(context)
    rejected = {}
    origin = "原订阅规则组" if policy["source"] == "原订阅" else "系统默认规则组"
    reason = f"不符合{origin}：{'、'.join(names)}"
    chain = SearchChain()
    for batch in batches.values():
        torrents = [copy.deepcopy(context.torrent_info) for context in batch]
        try:
            retained = chain.filter_torrents(
                rule_groups=names, torrent_list=torrents,
                mediainfo=getattr(batch[0], "media_info", None),
            ) or []
        except Exception as error:
            raise ValueError("规则组筛选失败，请检查系统规则组配置，或选择“不使用规则组”后重新预览") from error
        retained_ids = {id(torrent) for torrent in retained}
        for context, torrent in zip(batch, torrents):
            if id(torrent) not in retained_ids:
                rejected[id(context)] = reason
    return rejected


def _with_unfiltered_group(value: Any) -> list[dict[str, Any]]:
    groups = copy.deepcopy(value or [])
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        raise ValueError("宿主规则组配置格式异常，无法添加不使用规则组选项")
    existing = next((group for group in groups if group.get("name") == UNFILTERED_GROUP_NAME), None)
    if existing is not None:
        if any(existing.get(field) != expected for field, expected in UNFILTERED_GROUP.items()):
            raise ValueError("同名“不使用规则组”已被修改，请在宿主中重命名后重新确认")
        return groups
    return [*groups, copy.deepcopy(UNFILTERED_GROUP)]


def ensure_unfiltered_group() -> str:
    """仅在确认此选项时创建或复用规则组，保留其他规则组和宿主默认选择。"""
    from app.db.oper.systemconfig import SystemConfigOper
    from app.schemas.types import SystemConfigKey

    oper = SystemConfigOper()
    key = SystemConfigKey.UserFilterRuleGroups
    with _group_lock:
        original = oper.get(key) or []
        updated = _with_unfiltered_group(original)
        if updated == original:
            return UNFILTERED_GROUP_NAME
        if callable(getattr(oper, "update_atomically", None)):
            oper.update_atomically(key, lambda _db, current: (None, _with_unfiltered_group(current)))
        elif oper.set(key, updated) is False:
            raise ValueError("不使用规则组的配置保存失败")
        committed = oper.get(key) or []
        if _with_unfiltered_group(committed) != committed:
            raise ValueError("不使用规则组的配置未成功保存")
    return UNFILTERED_GROUP_NAME

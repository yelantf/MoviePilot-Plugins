"""为显式选择不使用规则组的订阅提供全放行组，避免空列表回退宿主默认组。"""

import copy
import threading
from typing import Any


UNFILTERED_GROUP_NAME = "剧集守门员·不使用规则组"
# 宿主内置 WEBDL 的布尔恒真式，保留合法规则表达式及正常优先级计算。
UNFILTERED_GROUP = {
    "name": UNFILTERED_GROUP_NAME, "rule_string": "WEBDL | !WEBDL", "media_type": None, "category": None,
}
_group_lock = threading.RLock()


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

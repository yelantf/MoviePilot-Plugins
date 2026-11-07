"""目标季搜索词及站点失败后的请求抑制，网络和媒体匹配仍交给宿主。"""

import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable

from app.chain.search import SearchChain


@contextmanager
def _observation():
    """旧宿主没有观察接口时保持原搜索返回合同。"""
    try:
        from app.application.site.observation import capture_site_search_observation
    except ImportError:
        yield None
        return
    observation = None
    try:
        with capture_site_search_observation() as observation:
            yield observation
    finally:
        # 宿主可能也在外层观察请求。将事实传回外层，保持它的失败预算与翻页判断。
        try:
            from app.application.site.observation import report_site_search_outcome, report_site_search_page
        except ImportError:
            pass
        else:
            if observation is not None and observation.outcome not in {"skipped", "unknown"}:
                report_site_search_outcome(attempted=observation.attempted, outcome=observation.outcome,
                                           error=observation.error)
            if observation is not None and getattr(observation, "raw_count", None) is not None:
                report_site_search_page(raw_count=observation.raw_count, has_more=observation.has_more)


def search_names(media: Any, fallback: str) -> list[str]:
    """优先使用发布标题常见的英文名，保留一个本地标题作为带季查询的备选。"""
    english = [getattr(media, field, None) for field in ("en_title", "original_title")]
    english += list(getattr(media, "names", None) or [])
    preferred = next((name for name in english if isinstance(name, str)
                      and re.search(r"[A-Za-z]", name)
                      and not re.search(r"[\u4e00-\u9fff]", name)), None)
    localized = getattr(media, "title", None) or fallback
    names = []
    for name in (preferred, localized):
        if not isinstance(name, str):
            continue
        # 避免把 & 交给站点当成特殊查询符号，也匹配常见的 and 发布标题。
        name = " ".join(re.sub(r"\s*&\s*", " and ", name).split())[:240]
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def failure_hold(outcome: str, detail: str, now: float) -> tuple[float, str]:
    """遵守明确的解除时间；其余失败使用固定冷却，避免换词后立即重试。"""
    text = detail.lower()
    limited = outcome in {"rate_limited", "forbidden", "login_invalid", "deferred"} or any(
        word in text for word in ("429", "403", "rate limit", "forbidden", "限制", "限流", "异常的浏览")
    )
    reason = ("站点限制访问或登录失效" if limited else
              "站点请求超时" if outcome == "timeout" or "超时" in text or "timeout" in text
              else "站点请求失败")
    until = now + (4 * 3600 if limited else 15 * 60)
    if limited:
        match = re.search(r"(?:至|到|until)\s*[：:]?\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail, re.I)
        if match:
            try:
                local_zone = datetime.now().astimezone().tzinfo
                explicit = datetime.fromisoformat(match.group(1)).replace(tzinfo=local_zone).timestamp()
                if explicit > now:
                    until = explicit
            except (ValueError, OverflowError):
                pass
    return until, reason


class CandidateSearchChain(SearchChain):
    """在宿主站点请求边界记录失败；后续搜索词和其他订阅遵守同一冷却。"""

    def __init__(self, held: Callable, hold: Callable, *, focused_season: int | None = None) -> None:
        super().__init__()
        self._held = held
        self._hold = hold
        self._focused_season = focused_season
        self._reports_lock = threading.Lock()
        self.site_reports: list[dict[str, Any]] = []

    def _site_keyword(self, keyword: str) -> str:
        # 宿主可能把“中文剧名 S07”替换为纯英文名；在请求边界补回本轮目标季。
        # 原订阅搜索没有 focused_season，保留宿主传来的关键词，不额外添加季号。
        if self._focused_season is None:
            return keyword
        token = f"S{self._focused_season:02d}"
        return keyword if re.search(rf"(?:^|\s){token}$", keyword, re.I) else f"{keyword} {token}"

    def _search_site_torrents_with_budget(self, *, site: dict[str, Any], keyword: str,
                                        mtype: Any = None, page: int = 0) -> list[Any]:
        # 宿主在此方法内等待站点额度；已有插件冷却时应在等待之前直接跳过。
        pause = self._held(site)
        if pause:
            self._report(site, "paused", 0, pause)
            return []
        with _observation() as observation:
            result = super()._search_site_torrents_with_budget(
                site=site, keyword=self._site_keyword(keyword), mtype=mtype, page=page,
            )
            # 请求尚未发出就被宿主共享冷却拒绝时，站点入口不会被调用。
            outcome = getattr(observation, "outcome", "unknown")
            if outcome not in {"success", "skipped", "unknown"} and not self._held(site):
                pause = self._hold(site, outcome, getattr(observation, "error", None) or "")
                self._report(site, outcome, len(result or []), pause)
            return result

    def search_site_torrents(self, *, site: dict[str, Any], keyword: str, mtype: Any = None,
                            page: int = 0) -> list[Any]:
        keyword = self._site_keyword(keyword)
        pause = self._held(site)
        if pause:
            self._report(site, "paused", 0, pause, keyword=keyword)
            return []
        with _observation() as observation:
            try:
                result = super().search_site_torrents(
                    site=site, keyword=keyword, mtype=mtype, page=page,
                ) or []
            except Exception:
                if observation is not None:
                    observation.attempted = True
                    observation.outcome = "error"
                    observation.error = "站点请求失败"
                pause = self._hold(site, "error", "")
                self._report(site, "error", 0, pause, keyword=keyword)
                return []
            outcome = getattr(observation, "outcome", "unknown")
            if outcome not in {"success", "skipped", "unknown"}:
                pause = self._hold(site, outcome, getattr(observation, "error", None) or "")
            self._report(site, outcome, len(result), pause, keyword=keyword)
            return result

    def _report(self, site: dict[str, Any], status: str, count: int, pause: Any, *, keyword: str = "") -> None:
        with self._reports_lock:
            self.site_reports.append({
                "site_id": site.get("id"), "site_name": str(site.get("name") or "未知站点")[:100],
                "status": status, "count": count,
                "keyword": keyword,
                "reason": (pause or {}).get("reason"), "until": (pause or {}).get("until"),
            })

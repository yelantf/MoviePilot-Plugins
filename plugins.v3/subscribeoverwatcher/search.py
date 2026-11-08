"""目标季搜索词及站点失败后的请求抑制，网络和媒体匹配仍交给宿主。"""

import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from inspect import signature
from typing import Any, Callable
from uuid import uuid4

from app.chain.search import SearchChain
from app.sdk.logging import logger


def configure_candidate_budget(chain: Any, cancelled: Callable[[], bool]) -> bool:
    """复用宿主的站点租约；真实请求计入同一记录，不创建下载或订阅搜索任务。"""
    try:
        from app.application.subscription.sitebudget import SubscriptionSiteBudget, SubscriptionSiteBudgetUnavailable
        from app.runtime.stop import runtime_stop_state
    except ImportError:
        return False
    repository = getattr(chain, "subscription_search_repository", None)
    configure = getattr(chain, "configure_subscription_site_budget", None)
    if repository is None or not callable(configure):
        return False
    if "minimum_interval" not in signature(SubscriptionSiteBudget.acquire).parameters:
        return False
    try:
        from app.chain.search.scan import AUTOMATIC_REQUEST_INTERVAL
    except ImportError:
        AUTOMATIC_REQUEST_INTERVAL = 10

    class CandidateBudget(SubscriptionSiteBudget):
        """短间隔等待不算错误；过长等待保留诊断，由用户或下一轮扫描重试。"""

        def acquire(self, site_id: int, *, minimum_interval: float = 0):
            site = chain._budget_sites.get(site_id) or {}
            try:
                site_interval = max(0, float(site.get("limit_seconds") or 0))
            except (TypeError, ValueError, OverflowError):
                site_interval = 0
            interval = max(minimum_interval, site_interval,
                           random.uniform(AUTOMATIC_REQUEST_INTERVAL, AUTOMATIC_REQUEST_INTERVAL * 2))
            started = time.monotonic()
            notified = False
            try:
                while True:
                    try:
                        return super().acquire(site_id, minimum_interval=interval)
                    except SubscriptionSiteBudgetUnavailable as error:
                        retry = datetime.fromisoformat(error.retry_at)
                        if retry.tzinfo is None:
                            retry = retry.astimezone()
                        seconds = max(0.05, retry.timestamp() - time.time())
                        remaining = 60 - (time.monotonic() - started)
                        if error.wait_reason == "failing" or remaining <= 0 \
                                or (error.wait_reason != "busy" and seconds > remaining):
                            reason = {
                                "busy": "站点正在被系统或其他搜索占用，本轮暂缓",
                                "cooldown": "站点搜索间隔尚未结束，本轮暂缓",
                                "rate_limit": "站点访问额度尚未恢复，本轮暂缓",
                                "failing": "宿主报告该站点处于失败冷却，本轮暂缓",
                            }.get(error.wait_reason, "站点暂不可用，本轮暂缓")
                            chain._budget_deferred(site_id, reason, retry.timestamp())
                            raise
                        if not notified:
                            logger.info(f"插件搜索等待站点 {site_id} 的共享访问间隔或在途请求，约 {seconds:.1f} 秒后重试")
                            notified = True
                        time.sleep(min(1.0, seconds, remaining))
            finally:
                if notified:
                    chain._record_site_wait(site_id, time.monotonic() - started)

    configure(CandidateBudget(repository=repository, owner=f"SubscribeOverwatcher:{uuid4().hex}",
                              cancelled=cancelled, stop_state=runtime_stop_state))
    return True


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

    def __init__(self, held: Callable, hold: Callable, *, focused_season: int | None = None,
                 budget_deferrals: dict | None = None) -> None:
        super().__init__()
        self._held = held
        self._hold = hold
        self._focused_season = focused_season
        self._reports_lock = threading.Lock()
        self.site_reports: list[dict[str, Any]] = []
        self.site_waits: dict[int, float] = {}
        self._budget_sites: dict[int, dict[str, Any]] = {}
        self._budget_deferrals = budget_deferrals if budget_deferrals is not None else {}

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
        deferred = self._budget_deferrals.get(site.get("id"))
        if deferred and deferred["until"] > time.time():
            self._report(site, "deferred", 0, deferred)
            return []
        with self._reports_lock:
            self._budget_sites[site.get("id")] = site
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

    def _budget_deferred(self, site_id: int, reason: str, until: float) -> None:
        site = self._budget_sites.get(site_id) or {"id": site_id}
        pause = {"reason": reason, "until": until}
        with self._reports_lock:
            self._budget_deferrals[site_id] = pause
        self._report(site, "deferred", 0, pause)

    def _record_site_wait(self, site_id: int, seconds: float) -> None:
        with self._reports_lock:
            self.site_waits[site_id] = self.site_waits.get(site_id, 0) + seconds

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

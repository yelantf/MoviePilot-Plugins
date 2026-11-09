"""每条订阅独立搜索；本轮失败不重试，请求仍遵守站点流控和访问间隔。"""

import random
import math
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable
from uuid import uuid4

from app.chain.search import SearchChain
from app.sdk.logging import logger


class RequestDeferred(RuntimeError):
    """只记录当前请求的间隔等待，不产生跨订阅的失败冷却。"""

    def __init__(self, reason: str, until: float | None):
        super().__init__(reason)
        self.until = until


class CandidateRequestPacer:
    """进程内限制同站请求开始间隔和在途重叠，不保存或判断失败状态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[Any, float] = {}
        self._active: dict[Any, str] = {}

    @staticmethod
    def _key(site: dict[str, Any]) -> Any:
        return site.get("id") or site.get("domain") or site.get("name")

    def acquire(self, site: dict[str, Any], cancelled: Callable[[], bool]) -> tuple[str, float]:
        key = self._key(site)
        try:
            configured = max(0, float(site.get("limit_seconds") or 0))
        except (TypeError, ValueError, OverflowError):
            configured = 0
        if not math.isfinite(configured):
            configured = 0
        interval = max(configured, random.uniform(10, 20))
        started = time.monotonic()
        notified = False
        while True:
            if cancelled():
                raise RuntimeError("插件搜索已停止")
            with self._lock:
                now = time.monotonic()
                active = key in self._active
                delay = max(0, self._next.get(key, now) - now)
                if not active and delay <= 0:
                    token = uuid4().hex
                    self._active[key] = token
                    self._next[key] = now + interval
                    return token, now - started
            remaining = 60 - (time.monotonic() - started)
            if remaining <= 0 or (not active and delay > remaining):
                reason = "站点请求仍在进行，本轮暂缓" if active else "站点访问间隔较长，本轮暂缓"
                raise RequestDeferred(reason, None if active else time.time() + max(delay, 1))
            if not notified:
                logger.info(f"插件搜索等待站点 {site.get('name') or key} 的请求间隔或在途请求")
                notified = True
            time.sleep(min(1, remaining, max(delay, 0.05) if not active else 1))

    def release(self, site: dict[str, Any], token: str) -> None:
        key = self._key(site)
        with self._lock:
            if self._active.get(key) == token:
                self._active.pop(key, None)


# 更新配置或重新创建插件实例不应使正常请求间隔归零。
REQUEST_PACER = CandidateRequestPacer()


@contextmanager
def _observation():
    """兼容旧宿主，同时向外层保留真实请求和分页事实。"""
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
    """优先英文名，再保留一个本地名作为带季查询备选。"""
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
        name = " ".join(re.sub(r"\s*&\s*", " and ", name).split())[:240]
        if name and name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def failure_reason(outcome: str, detail: str) -> str:
    """只保留安全的原因类别，原始 URL、Cookie 和响应不写入展示记录。"""
    text = detail.lower()
    if outcome in {"rate_limited", "forbidden", "login_invalid", "deferred"} or any(
        word in text for word in ("429", "403", "rate limit", "forbidden", "限制", "限流", "异常的浏览")
    ):
        return "站点限制访问、额度不足或登录失效"
    if outcome == "timeout" or "超时" in text or "timeout" in text:
        return "站点请求超时"
    return "站点请求失败"


class CandidateSearchChain(SearchChain):
    """同一订阅本轮共享失败与相同查询结果，其他订阅各自尝试。"""

    def __init__(self, *, focused_season: int | None = None, failed_sites: dict | None = None,
                 budget_deferrals: dict | None = None, request_cache: dict | None = None,
                 pacer: CandidateRequestPacer | None = None, cancelled: Callable[[], bool] | None = None):
        super().__init__()
        # 不读写系统订阅的共享失败预算；实际站点请求仍经过宿主的访问计数与流控。
        self._subscription_site_budget = None
        self._focused_season = focused_season
        self._reports_lock = threading.Lock()
        self._request_local = threading.local()
        self.site_reports: list[dict[str, Any]] = []
        self.site_waits: dict[Any, float] = {}
        self._failed_sites = failed_sites if failed_sites is not None else {}
        self._budget_deferrals = budget_deferrals if budget_deferrals is not None else {}
        self._request_cache = request_cache if request_cache is not None else {}
        self._pacer = pacer or REQUEST_PACER
        self._cancelled = cancelled or (lambda: False)

    def _site_keyword(self, keyword: str) -> str:
        if self._focused_season is None:
            return keyword
        token = f"S{self._focused_season:02d}"
        return keyword if re.search(rf"(?:^|\s){token}$", keyword, re.I) else f"{keyword} {token}"

    def _perform(self, site: dict[str, Any], keyword: str, mtype: Any, page: int,
                 search: Callable[[], Any]) -> list[Any]:
        if self._cancelled():
            raise RuntimeError("插件搜索已停止")
        site_id = CandidateRequestPacer._key(site)
        failure = self._failed_sites.get(site_id) or self._budget_deferrals.get(site_id)
        if failure:
            self._report(site, "skipped_in_round", 0, failure, keyword=keyword, attempted=False)
            return []
        key = (site_id, keyword, str(mtype), page)
        if key in self._request_cache:
            result = list(self._request_cache[key])
            self._report(site, "reused_in_round", len(result), None, keyword=keyword, attempted=False)
            return result
        started = time.monotonic()
        try:
            token, waited = self._pacer.acquire(site, self._cancelled)
        except RequestDeferred as error:
            pause = {"reason": str(error), "until": error.until}
            self._budget_deferrals[site_id] = pause
            self.site_waits[site_id] = time.monotonic() - started
            self._report(site, "deferred", 0, pause, keyword=keyword, attempted=False)
            return []
        self.site_waits[site_id] = self.site_waits.get(site_id, 0) + waited
        observation = None
        try:
            with _observation() as observation:
                self._request_local.active = True
                try:
                    result = search() or []
                except Exception:
                    if self._cancelled():
                        raise
                    if observation is not None:
                        observation.attempted = True
                        observation.outcome = "error"
                        observation.error = "站点请求失败"
                    self._failed_sites[site_id] = {"reason": "站点请求失败"}
                    self._report(site, "error", 0, self._failed_sites[site_id], keyword=keyword, attempted=True)
                    return []
                finally:
                    self._request_local.active = False
        finally:
            self._pacer.release(site, token)
        outcome = getattr(observation, "outcome", "unknown")
        attempted = getattr(observation, "attempted", outcome not in {"skipped", "deferred"})
        pause = None
        if outcome not in {"success", "skipped", "unknown"}:
            pause = {"reason": failure_reason(outcome, getattr(observation, "error", None) or "")}
            self._failed_sites[site_id] = pause
        elif outcome in {"success", "unknown"}:
            self._request_cache[key] = list(result)
        self._report(site, outcome, len(result), pause, keyword=keyword, attempted=attempted)
        return result

    def _search_site_torrents_with_budget(self, *, site: dict[str, Any], keyword: str,
                                        mtype: Any = None, page: int = 0) -> list[Any]:
        keyword = self._site_keyword(keyword)
        parent = super()

        def search():
            try:
                from app.application.site.throttle import wait_for_site_request
                from app.application.site.observation import report_site_search_outcome
                from app.schemas.exception import RateLimitExceededException
            except ImportError:
                return parent._search_site_torrents_with_budget(site=site, keyword=keyword, mtype=mtype, page=page)
            # 与宿主无订阅预算的单页路径一致；直接等待真实额度后调用原始入口。
            # 不进入宿主活动记录的内层观察上下文，避免丢失限流或失败事实。
            try:
                wait_for_site_request(site)
            except RateLimitExceededException as error:
                report_site_search_outcome(attempted=False, outcome="deferred", error=str(error))
                return []
            return parent.search_site_torrents(site=site, keyword=keyword, mtype=mtype, page=page)

        return self._perform(site, keyword, mtype, page, search)

    def search_site_torrents(self, *, site: dict[str, Any], keyword: str, mtype: Any = None,
                            page: int = 0) -> list[Any]:
        keyword = self._site_keyword(keyword)
        parent = super().search_site_torrents
        if getattr(self._request_local, "active", False):
            return parent(site=site, keyword=keyword, mtype=mtype, page=page)
        return self._perform(site, keyword, mtype, page,
                             lambda: parent(site=site, keyword=keyword, mtype=mtype, page=page))

    def _report(self, site: dict[str, Any], status: str, count: int, pause: Any, *,
                keyword: str = "", attempted: bool = False) -> None:
        with self._reports_lock:
            self.site_reports.append({
                "site_id": site.get("id"), "site_name": str(site.get("name") or "未知站点")[:100],
                "status": status, "count": count, "keyword": keyword, "attempted": attempted,
                "reason": (pause or {}).get("reason"), "until": (pause or {}).get("until"),
            })

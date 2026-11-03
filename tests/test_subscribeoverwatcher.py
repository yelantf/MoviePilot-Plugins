"""
剧集守门员插件的隔离行为测试。

用内存宿主替身验证事件时序、规则写入和候选快照，不访问真实数据库或站点。
"""

import copy
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins.v3" / "subscribeoverwatcher"


@pytest.fixture
def plugin_host(monkeypatch):
    """
    装配只有插件所需接口的内存宿主。

    :param monkeypatch: pytest 模块替换夹具
    :return: 插件模块、订阅表替身和任务记录
    """
    subscriptions = {}
    storage = {}
    jobs = {}
    search_results = []
    search_calls = []

    for name in ("app", "app.chain", "app.db", "app.db.oper", "app.schemas", "app.sdk"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)

    class PluginBase:
        """
        以深拷贝模拟插件数据存取边界。
        """

        def __init__(self):
            """
            创建插件实例，不触碰宿主资源。
            """

        def get_data(self, key):
            """
            读取持久化快照。
            """
            return copy.deepcopy(storage.get(key))

        def save_data(self, key, value):
            """
            保存持久化快照。
            """
            storage[key] = copy.deepcopy(value)

        def update_config(self, config):
            """
            记录一次性确认控件被复位。
            """
            storage["config"] = copy.deepcopy(config)

    class SubscribeOper:
        """
        提供可观察的订阅读写。
        """

        def list(self):
            """
            返回当前订阅集合。
            """
            return list(subscriptions.values())

        def get(self, sid):
            """
            按 ID 查询订阅。
            """
            return subscriptions.get(sid)

        def update(self, sid, payload):
            """
            将规则或状态写到内存订阅。
            """
            for key, value in payload.items():
                setattr(subscriptions[sid], key, value)
            return subscriptions[sid]

    class SearchChain:
        """
        只返回预置搜索结果，不产生下载副作用。
        """

        def search_by_id(self, **kwargs):
            """
            记录首集扫描并返回预置候选。
            """
            search_calls.append(kwargs)
            return list(search_results)

    class SubscribeChain:
        """
        记录确认后发起的目标补搜。
        """

        def search(self, **kwargs):
            """
            保存补搜参数供断言。
            """
            search_calls.append({"subscription_search": kwargs})

    def register(_event_type, priority=10):
        """
        保留事件处理器供测试直接调用。
        """
        del priority
        return lambda handler: handler

    def add_job(_plugin_id, job_id, func, _name, delay_seconds=0, func_kwargs=None):
        """
        记录宿主调度器的一次性任务。
        """
        jobs[job_id] = (func, delay_seconds, func_kwargs or {})
        return True

    def remove_job(_plugin_id, job_id):
        """
        移除已记录的一次性任务。
        """
        jobs.pop(job_id, None)

    exports = {
        "app.chain.search": {"SearchChain": SearchChain},
        "app.chain.subscribe": {"SubscribeChain": SubscribeChain},
        "app.db.oper.subscribe": {"SubscribeOper": SubscribeOper},
        "app.plugins": {"_PluginBase": PluginBase},
        "app.schemas.types": {
            "ChainEventType": SimpleNamespace(ResourceDownload="resource.download"),
            "EventType": SimpleNamespace(SubscribeAdded="subscribe.added"),
            "MediaSource": lambda value: value,
            "MediaType": SimpleNamespace(TV=SimpleNamespace(value="电视剧")),
        },
        "app.sdk.events": {"Event": SimpleNamespace, "eventmanager": SimpleNamespace(register=register)},
        "app.sdk.logging": {"logger": SimpleNamespace(
            info=lambda *_args: None, warning=lambda *_args: None, exception=lambda *_args: None,
        )},
        "app.sdk.scheduler": {"add_plugin_once_job": add_job, "remove_plugin_once_job": remove_job},
    }
    for name, attributes in exports.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)

    name = "subscribeoverwatcher_isolated_test"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        module=module, subscriptions=subscriptions, storage=storage, jobs=jobs,
        search_results=search_results, search_calls=search_calls,
    )


def _subscribe(sid, mtype="电视剧"):
    """
    建立一条包含确认身份字段的新订阅。
    """
    return SimpleNamespace(
        id=sid, type=mtype, media_source="themoviedb", media_id="123",
        season=1, episode_group=None, activity_key=f"activity-{sid}", name="样例剧", state="N",
        total_episode=8, quality=None, resolution=None, effect=None, include=None, sites=[],
    )


def _context(title, episodes=(1,), enclosure="https://secret.example/torrent"):
    """
    建立带敏感字段的资源上下文，用于验证快照范围。
    """
    return SimpleNamespace(
        meta_info=SimpleNamespace(
            begin_season=1, end_season=None, season_list=[1], episode_list=list(episodes),
            begin_episode=1, end_episode=max(episodes), resource_type="WEB-DL",
            resource_pix="1080p", resource_effect="HDR", resource_team="TEAM",
        ),
        torrent_info=SimpleNamespace(
            title=title, site=5, site_name="样例站", size=1024,
            pubdate="2026-11-02", enclosure=enclosure, site_cookie="cookie-secret",
        ),
    )


def _download_event(sid, context=None, episodes=None):
    """
    建立带宿主订阅来源的下载前事件。
    """
    origin = "Subscribe|" + json.dumps({"id": sid})
    return SimpleNamespace(event_data=SimpleNamespace(
        origin=origin, context=context, episodes=episodes,
        cancel=False, source="原处理器", reason="原原因",
    ))


def _form_items(plugin, model):
    """
    根据配置字段读取选择项，避免表单顺序影响行为测试。
    """
    form, _defaults = plugin.get_form()
    return next(item["props"]["items"] for item in form[0]["content"] if item["props"].get("model") == model)


def test_delayed_added_event_is_gated_until_rules_are_confirmed(plugin_host):
    """
    新增广播未到时先拦下载；确认后回填规则并补搜。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)

    before_added = _download_event(2)
    plugin.intercept_download(before_added)
    assert before_added.event_data.cancel is True
    assert host.subscriptions[2].state == "N"

    added = SimpleNamespace(event_data={"subscribe_id": 2})
    plugin.on_subscribe_added(added)
    plugin.on_subscribe_added(added)
    assert host.subscriptions[2].state == "S"
    assert len(host.storage["gate_state"]["entries"]) == 1

    host.search_results.extend([
        _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"),
        _context("Show.S01E01-E02.1080p.WEB-DL.HDR-TEAM", episodes=(1, 2)),
    ])
    plugin.refresh_candidates()
    entry = host.storage["gate_state"]["entries"]["2"]
    assert len(entry["candidates"]) == 1
    assert "secret.example" not in json.dumps(entry)
    assert "cookie-secret" not in json.dumps(entry)
    assert host.search_calls[0]["season"] == 1

    choice = _form_items(plugin, "candidate")[0]["value"]
    plugin.init_plugin({"enabled": True, "candidate": choice, "confirm": True})
    updated = host.subscriptions[2]
    assert updated.state == "R"
    assert updated.quality == r"WEB[- .]?(?:DL|RIP)"
    assert updated.resolution == r"1080[pi]?"
    assert updated.effect == "HDR"
    assert updated.include == "TEAM"
    assert updated.sites == [5]
    assert host.storage["config"]["confirm"] is False
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is True

    after_confirm = _download_event(2)
    plugin.intercept_download(after_confirm)
    assert after_confirm.event_data.cancel is False
    assert after_confirm.event_data.source == "原处理器"
    plugin.on_subscribe_added(added)
    assert host.subscriptions[2].state == "R"
    plugin._search_approved(2)
    assert {"subscription_search": {"sid": 2, "state": None, "manual": True}} in host.search_calls
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is False
    del host.subscriptions[2]
    plugin.refresh_candidates()
    assert "2" not in host.storage["gate_state"]["entries"]


def test_fake_downloader_receives_task_only_after_confirming_a_version(plugin_host):
    """
    用内存下载器走完候选更新与确认前后的实际提交分支。
    """
    host = plugin_host
    host.subscriptions[41] = _subscribe(41)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[42] = _subscribe(42)
    submitted = []

    def fake_submit(title, site_id):
        """
        模拟宿主先匹配订阅规则，再根据同步事件决定是否提交。
        """
        subscribe = host.subscriptions[42]
        patterns = (subscribe.quality, subscribe.resolution, subscribe.effect, subscribe.include)
        if any(pattern and not re.search(pattern, title, re.I) for pattern in patterns):
            return "filtered"
        if subscribe.sites and site_id not in subscribe.sites:
            return "filtered"
        event = _download_event(42)
        plugin.intercept_download(event)
        if event.event_data.cancel:
            return "blocked"
        submitted.append(title)
        return "submitted"

    assert fake_submit("Show.S01E01.1080p.WEB-DL.HDR-TEAM", 5) == "blocked"
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 42}))
    assert host.subscriptions[42].state == "S"
    assert fake_submit("Show.S01E01.1080p.WEB-DL.HDR-TEAM", 5) == "blocked"
    assert submitted == []

    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    other_version = _context("Show.S01E01.2160p.BluRay.DV-GROUP")
    other_version.meta_info.resource_type = "BluRay"
    other_version.meta_info.resource_pix = "2160p"
    other_version.meta_info.resource_effect = "DV"
    other_version.meta_info.resource_team = "GROUP"
    other_version.torrent_info.site = 7
    other_version.torrent_info.site_name = "另一个测试站"
    host.search_results[:] = [other_version]
    plugin.refresh_candidates()
    entry = host.storage["gate_state"]["entries"]["42"]
    assert len(entry["candidates"]) == 2
    assert submitted == []

    choices = _form_items(plugin, "candidate")
    choice = next(item["value"] for item in choices if "BluRay" in item["title"])
    plugin.init_plugin({"enabled": True, "candidate": choice, "confirm": True})
    subscribe = host.subscriptions[42]
    assert subscribe.state == "R"
    assert subscribe.quality == r"Blu[- .]?Ray|BluRay"
    assert subscribe.resolution == r"2160[pi]?|4K"
    assert subscribe.effect is not None
    assert subscribe.include == "GROUP"
    assert subscribe.sites == [7]
    func, _delay, kwargs = host.jobs["search-42"]
    func(**kwargs)
    assert {"subscription_search": {"sid": 42, "state": None, "manual": True}} in host.search_calls
    assert fake_submit("Show.S01E02.1080p.WEB-DL.HDR-TEAM", 5) == "filtered"
    assert fake_submit("Show.S01E02.2160p.BluRay.DV-GROUP", 7) == "submitted"
    assert submitted == ["Show.S01E02.2160p.BluRay.DV-GROUP"]


def test_invalid_choice_and_reload_keep_pending_subscription_blocked(plugin_host):
    """
    无效选择不能放行；重载沿用原水位和待确认记录。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    success, _message = plugin.confirm_candidate("2:missing")
    assert success is False
    assert host.subscriptions[2].state == "S"

    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    assert host.storage["gate_state"]["baseline"] == 1
    event = _download_event(2)
    reloaded.intercept_download(event)
    assert event.event_data.cancel is True

    host.subscriptions[3] = _subscribe(3, mtype="电影")
    movie = _download_event(3)
    reloaded.intercept_download(movie)
    assert movie.event_data.cancel is False


def test_first_episode_candidates_keep_history_without_torrent_links(plugin_host):
    """
    候选只收单集 E01，并在资源消失后保留发现记录。
    """
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    first = rules.resource_candidate(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"), 1, "2026-11-02T01:00:00+00:00")
    assert first is not None
    assert rules.resource_candidate(_context("Show.S01E01-E02", episodes=(1, 2)), 1, "now") is None
    assert rules.resource_candidate(_context("Show.S01E01"), 2, "now") is None
    unspecified_season = _context("Show.E01.1080p.WEB-DL")
    unspecified_season.meta_info.begin_season = None
    assert rules.resource_candidate(unspecified_season, 2, "now") is None
    assert "enclosure" not in first
    assert "site_cookie" not in first

    updated = rules.resource_candidate(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"), 1, "2026-11-02T02:00:00+00:00")
    merged = rules.merge_candidates([first], [updated])
    assert merged[0]["first_seen"] == "2026-11-02T01:00:00+00:00"
    assert merged[0]["last_seen"] == "2026-11-02T02:00:00+00:00"
    assert rules.merge_candidates(merged, []) == merged


def test_failed_resume_keeps_download_gate_closed(plugin_host, monkeypatch):
    """
    规则写入后若恢复订阅失败，确认记录必须回退为待确认。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    candidate_id = host.storage["gate_state"]["entries"]["2"]["candidates"][0]["id"]
    original_update = plugin._subscribes.update

    def fail_resume(sid, payload):
        """
        只模拟恢复状态的写入失败。
        """
        if payload.get("state") == "R":
            raise RuntimeError("数据库写入失败")
        return original_update(sid, payload)

    monkeypatch.setattr(plugin._subscribes, "update", fail_resume)
    success, _message = plugin.confirm_candidate(f"2:{candidate_id}")
    assert success is False
    assert host.subscriptions[2].state == "S"
    assert host.storage["gate_state"]["entries"]["2"]["status"] == "pending"
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is True


def test_scheduler_unavailable_does_not_release_pending_subscription(plugin_host, monkeypatch):
    """
    调度器尚未装配时仍保持订阅暂停和下载拦截。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)

    def unavailable(*_args, **_kwargs):
        """
        模拟插件加载早于宿主调度器。
        """
        raise RuntimeError("调度器尚未初始化")

    monkeypatch.setattr(host.module, "add_plugin_once_job", unavailable)
    monkeypatch.setattr(host.module, "remove_plugin_once_job", unavailable)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert host.subscriptions[2].state == "S"
    plugin.stop_service()
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is True


def test_full_season_option_allows_only_complete_seed(plugin_host):
    """
    关闭全集确认后继续自动搜索，只放行种子覆盖当前季全部八集的资源。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True, "confirm_full_pack": False})
    host.subscriptions[2] = _subscribe(2)
    host.subscriptions[2].quality = "WEB-DL"
    host.subscriptions[2].sites = [5]
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert host.subscriptions[2].state == "N"
    assert host.storage["gate_state"]["entries"]["2"]["status"] == "pending"

    full = _context("Show.S01.Complete.1080p.WEB-DL", episodes=tuple(range(1, 9)))
    partial = _context("Show.S01E01-E04.1080p.WEB-DL", episodes=(1, 2, 3, 4))
    submitted = []

    def fake_submit(context, selected):
        """
        模拟宿主先应用订阅规则，再根据同步事件提交下载。
        """
        subscribe = host.subscriptions[2]
        torrent = context.torrent_info
        if not re.search(subscribe.quality, torrent.title) or torrent.site not in subscribe.sites:
            return "filtered"
        event = _download_event(2, context=context, episodes=selected)
        plugin.intercept_download(event)
        if event.event_data.cancel:
            return "blocked"
        submitted.append(torrent.title)
        return "submitted"

    assert fake_submit(partial, set(range(1, 5))) == "blocked"
    assert fake_submit(full, {1, 2}) == "submitted"
    assert submitted == [full.torrent_info.title]
    full.torrent_info.site = 7
    assert fake_submit(full, set(range(1, 9))) == "filtered"
    full.torrent_info.site = 5
    already_cancelled = _download_event(2, context=full, episodes=set(range(1, 9)))
    already_cancelled.event_data.cancel = True
    plugin.intercept_download(already_cancelled)
    assert already_cancelled.event_data.cancel is True
    assert already_cancelled.event_data.source == "原处理器"
    for context, selected, expected in (
        (partial, set(range(1, 5)), True),
        (full, {1, 2}, False),
        (full, {9}, True),
        (full, set(range(1, 9)), False),
    ):
        event = _download_event(2, context=context, episodes=selected)
        plugin.intercept_download(event)
        assert event.event_data.cancel is expected

    unknown_total = _download_event(2, context=full, episodes=set(range(1, 9)))
    host.subscriptions[2].total_episode = None
    plugin.intercept_download(unknown_total)
    assert unknown_total.event_data.cancel is True
    host.subscriptions[2].total_episode = 8
    full.meta_info.season_list = [2]
    wrong_season = _download_event(2, context=full, episodes=set(range(1, 9)))
    plugin.intercept_download(wrong_season)
    assert wrong_season.event_data.cancel is True

    plugin.refresh_candidates()
    assert host.subscriptions[2].state == "N"
    assert {"subscription_search": {"sid": 2, "state": None, "manual": True}} in host.search_calls
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is False


def test_switching_full_season_option_releases_plugin_pause(plugin_host):
    """
    改为全集直下会恢复插件暂停的订阅并安排补搜；再开启会暂停。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert host.subscriptions[2].state == "S"
    assert host.storage["gate_state"]["entries"]["2"]["plugin_paused"] is True

    plugin.init_plugin({"enabled": True, "confirm_full_pack": False})
    assert host.subscriptions[2].state == "R"
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is True
    plugin.refresh_candidates()
    assert {"subscription_search": {"sid": 2, "state": None, "manual": True}} in host.search_calls

    plugin.init_plugin({"enabled": True, "confirm_full_pack": True})
    assert host.subscriptions[2].state == "S"
    full = _context("Show.S01.Complete", episodes=tuple(range(1, 9)))
    event = _download_event(2, context=full, episodes=set(range(1, 9)))
    plugin.intercept_download(event)
    assert event.event_data.cancel is True


def test_old_pending_record_can_enable_full_season_exemption(plugin_host):
    """
    升级前的待确认记录没有暂停来源字段，切换开关后仍可恢复搜索。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.storage["gate_state"]["entries"]["2"].pop("plugin_paused")
    host.storage["gate_state"]["entries"]["2"].pop("needs_search")

    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True, "confirm_full_pack": False})
    assert host.subscriptions[2].state == "R"
    assert host.storage["gate_state"]["entries"]["2"]["plugin_paused"] is False
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is True


def test_direct_approval_preserves_rules_and_can_be_revoked(plugin_host):
    """
    直接批准不改规则；撤销后保留手动规则、取消补搜并重启闸门。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.quality = "WEB-DL"
    subscribe.sites = [7]
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert _form_items(plugin, "direct_subscription") == [
        {"title": "#2 样例剧 S01", "value": "2"},
    ]

    plugin.init_plugin({"enabled": True, "direct_subscription": "2", "direct_approve": True})
    assert subscribe.state == "R"
    assert subscribe.quality == "WEB-DL"
    assert subscribe.sites == [7]
    assert host.storage["gate_state"]["entries"]["2"]["mode"] == "direct"
    assert host.storage["config"]["direct_approve"] is False
    assert "search-2" in host.jobs
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is False

    subscribe.resolution = "2160p"
    plugin.init_plugin({"enabled": True, "revoke_subscription": "2", "revoke": True})
    assert subscribe.state == "S"
    assert subscribe.quality == "WEB-DL"
    assert subscribe.resolution == "2160p"
    assert subscribe.sites == [7]
    assert "search-2" not in host.jobs
    assert host.storage["gate_state"]["entries"]["2"]["status"] == "pending"
    assert _form_items(plugin, "direct_subscription")[0]["value"] == "2"
    blocked = _download_event(2)
    plugin.intercept_download(blocked)
    assert blocked.event_data.cancel is True
    plugin._search_approved(2)
    assert not any("subscription_search" in call for call in host.search_calls)

    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    after_reload = _download_event(2)
    reloaded.intercept_download(after_reload)
    assert after_reload.event_data.cancel is True


def test_candidate_approval_can_be_revoked_without_overwriting_manual_edits(plugin_host):
    """
    候选批准后允许手改规则；撤销仅改变批准状态和订阅状态。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]
    success, _message = plugin.confirm_candidate(choice)
    assert success is True
    subscribe.quality = "REMUX"

    success, _message = plugin.revoke_approval("2")
    assert success is True
    assert subscribe.state == "S"
    assert subscribe.quality == "REMUX"
    assert subscribe.resolution == r"1080[pi]?"
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is True


def test_revoke_overrides_full_season_exemption_until_reapproved(plugin_host):
    """
    显式撤销批准会暂停订阅，并覆盖原先的全集免确认设置。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True, "confirm_full_pack": False})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    success, _message = plugin.approve_subscription("2")
    assert success is True
    success, _message = plugin.revoke_approval("2")
    assert success is True
    assert host.subscriptions[2].state == "S"
    partial = _download_event(2, context=_context("Show.S01E01"), episodes={1})
    plugin.intercept_download(partial)
    assert partial.event_data.cancel is True
    full = _download_event(
        2, context=_context("Show.S01.Complete", episodes=tuple(range(1, 9))),
        episodes=set(range(1, 9)),
    )
    plugin.intercept_download(full)
    assert full.event_data.cancel is True
    plugin.refresh_candidates()
    assert host.subscriptions[2].state == "S"
    plugin.init_plugin({"enabled": True, "confirm_full_pack": True})
    plugin.init_plugin({"enabled": True, "confirm_full_pack": False})
    assert host.subscriptions[2].state == "S"
    success, _message = plugin.approve_subscription("2")
    assert success is True
    assert host.subscriptions[2].state == "R"


def test_disabled_plugin_does_not_defer_approval_action(plugin_host):
    """
    停用时保存的一次性批准动作会清除，重新启用后不会意外放行。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    plugin.init_plugin({"enabled": False, "direct_subscription": "2", "direct_approve": True})
    assert host.storage["config"]["direct_approve"] is False
    plugin.init_plugin(host.storage["config"] | {"enabled": True})
    assert host.subscriptions[2].state == "S"
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is True

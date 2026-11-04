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
    downloads = []
    messages = []

    for name in ("app", "app.application", "app.application.torrent", "app.chain",
                 "app.db", "app.db.oper", "app.schemas", "app.sdk"):
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

        def post_message(self, **kwargs):
            """记录通过宿主通知渠道发送的消息。"""
            messages.append(kwargs)

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

        def process(self, **kwargs):
            """
            记录未应用规则组的搜索并返回预置候选。
            """
            search_calls.append(kwargs)
            contexts = list(search_results)
            callback = kwargs.get("candidate_filter")
            return callback(contexts) if callback else contexts

    class MediaChain:
        """返回与订阅身份对应的媒体信息。"""

        def recognize_media(self, *, media_source, media_id, mtype):
            """模拟宿主按媒体 ID 识别目标剧集。"""
            return SimpleNamespace(media_source=media_source, media_id=media_id,
                                   type=mtype, season=None)

    class NotExistMediaInfo:
        """记录按目标季搜索所需的缺集占位信息。"""

        def __init__(self, *, episodes):
            """保留目标集数参数供断言。"""
            self.episodes = episodes

    def resolve_media_identity(*, media):
        """从内存媒体对象取出来源与原生 ID。"""
        return media.media_source, media.media_id

    def build_media_key(media_source, media_id):
        """生成内存搜索使用的媒体键。"""
        return f"{media_source}:{media_id}"

    class SubscribeChain:
        """
        记录确认后发起的目标补搜。
        """

        def search(self, **kwargs):
            """
            保存补搜参数供断言。
            """
            search_calls.append({"subscription_search": kwargs})

        @staticmethod
        def get_params(subscribe):
            """
            返回当前订阅的基础正则筛选参数。
            """
            return {name: getattr(subscribe, name, None) for name in (
                "quality", "resolution", "effect", "include", "exclude",
            )}

        @staticmethod
        def get_subscribe_source_keyword(subscribe):
            """
            为明确选中的回刷资源保留订阅来源。
            """
            return "Subscribe|" + json.dumps({"id": subscribe.id})

    class TorrentHelper:
        """
        用与宿主相同的字段顺序模拟当前规则拒绝原因。
        """

        @staticmethod
        def filter_torrent_reason(torrent, params):
            """
            返回第一个未满足的基础筛选条件。
            """
            content = f"{torrent.title} {torrent.description} {' '.join(torrent.labels)} {torrent.volume_factor}"
            for key, subject, expected in (
                ("include", content, True), ("exclude", content, False),
                ("quality", torrent.title, True), ("resolution", torrent.title, True),
                ("effect", torrent.title, True),
            ):
                pattern = params.get(key)
                if pattern and bool(re.search(pattern, subject, re.I)) is not expected:
                    return f"不符合{key}条件"
            return None

    class DownloadChain:
        """
        记录用户明确选择后的单资源下载调用。
        """

        def download_single(self, context, **kwargs):
            """
            保存所选资源、集数和订阅来源。
            """
            downloads.append((context, kwargs))
            return ("fake-hash", None)

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
        "app.application.torrent.download": {"TorrentHelper": TorrentHelper},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.chain.media": {"MediaChain": MediaChain},
        "app.chain.search": {"SearchChain": SearchChain},
        "app.chain.subscribe": {"SubscribeChain": SubscribeChain},
        "app.db.oper.subscribe": {"SubscribeOper": SubscribeOper},
        "app.plugins": {"_PluginBase": PluginBase},
        "app.schemas.media": {"build_media_key": build_media_key,
                              "resolve_media_identity": resolve_media_identity},
        "app.schemas.mediaserver": {"NotExistMediaInfo": NotExistMediaInfo},
        "app.schemas.types": {
            "ChainEventType": SimpleNamespace(ResourceDownload="resource.download"),
            "EventType": SimpleNamespace(SubscribeAdded="subscribe.added"),
            "MediaSource": lambda value: value,
            "MediaType": SimpleNamespace(TV=SimpleNamespace(value="电视剧")),
            "MessageType": SimpleNamespace(Plugin="plugin"),
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
        search_results=search_results, search_calls=search_calls, downloads=downloads,
        messages=messages,
    )


def _subscribe(sid, mtype="电视剧"):
    """
    建立一条包含确认身份字段的新订阅。
    """
    return SimpleNamespace(
        id=sid, type=mtype, media_source="themoviedb", media_id="123",
        season=1, episode_group=None, activity_key=f"activity-{sid}", name="样例剧", state="N",
        total_episode=8, lack_episode=8, start_episode=1, best_version=0,
        quality=None, resolution=None, effect=None, include=None, sites=[],
    )


def _context(title, episodes=(1,), enclosure="https://secret.example/torrent", page_url=None):
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
            pubdate="2026-11-02", enclosure=enclosure, page_url=page_url, site_cookie="cookie-secret",
            description="", labels=[], volume_factor="",
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


def _form_field(plugin, model):
    """
    根据配置字段查找嵌套表单控件，避免依赖布局顺序。
    """
    form, _defaults = plugin.get_form()

    def walk(components):
        for component in components:
            yield component
            yield from walk(component.get("content", []))

    return next(item for item in walk(form)
                if item.get("props", {}).get("model") == model)


def _form_items(plugin, model):
    """读取指定选择控件的选项。"""
    return _form_field(plugin, model)["props"]["items"]


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
    assert host.search_calls[0]["rule_groups"] == []
    assert host.search_calls[0]["mediainfo"].season is None
    assert "no_exists" not in host.search_calls[0]
    assert callable(host.search_calls[0]["candidate_filter"])

    choice = _form_items(plugin, "candidate")[0]["value"]
    plugin.init_plugin({"enabled": True, "candidate": choice, "operation": "preview"})
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].quality is None
    assert host.storage["config"]["operation"] == "save"
    assert host.storage["config"]["candidate"] == choice
    plugin.init_plugin({**host.storage["config"], "operation": "confirm"})
    updated = host.subscriptions[2]
    assert updated.state == "R"
    assert updated.quality == r"WEB[- .]?(?:DL|RIP)"
    assert updated.resolution == r"1080[pi]?|x1080"
    assert updated.effect == "HDR"
    assert updated.include == "TEAM"
    assert updated.sites == [5]
    assert host.storage["config"]["operation"] == "save"
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is True

    after_confirm = _download_event(2, context=host.search_results[0], episodes={1})
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
        context = _context(title)
        context.torrent_info.site = site_id
        event = _download_event(42, context=context, episodes={1})
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
    plugin.init_plugin({"enabled": True, "candidate": choice, "operation": "preview"})
    plugin.init_plugin({**host.storage["config"], "operation": "confirm"})
    subscribe = host.subscriptions[42]
    assert subscribe.state == "R"
    assert subscribe.quality == r"Blu[- .]?Ray|BluRay"
    assert subscribe.resolution == r"2160[pi]?|4K|x2160"
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
    first = rules.resource_candidate(_context(
        "Show.S01E01.1080p.WEB-DL.HDR-TEAM",
        page_url="https://tracker.example/details.php?id=42&hit=1&passkey=secret",
    ), 1, "2026-11-02T01:00:00+00:00")
    assert first is not None
    assert rules.resource_candidate(_context("Show.S01E01-E02", episodes=(1, 2)), 1, "now") is None
    assert rules.resource_candidate(_context("Show.S01E02", episodes=(2,)), 1, "now") is None
    assert rules.resource_candidate(_context("Show.S01E01"), 2, "now") is None
    unspecified_season = _context("Show.E01.1080p.WEB-DL")
    unspecified_season.meta_info.begin_season = None
    assert rules.resource_candidate(unspecified_season, 2, "now") is None
    assert "enclosure" not in first
    assert "site_cookie" not in first
    assert first["page_url"] == "https://tracker.example/details.php?id=42&hit=1"
    assert "secret" not in json.dumps(first)

    updated = rules.resource_candidate(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"), 1, "2026-11-02T02:00:00+00:00")
    merged = rules.merge_candidates([first], [updated])
    assert merged[0]["first_seen"] == "2026-11-02T01:00:00+00:00"
    assert merged[0]["last_seen"] == "2026-11-02T02:00:00+00:00"
    assert merged[0]["page_url"] == first["page_url"]
    assert rules.merge_candidates(merged, []) == merged


def test_late_episode_with_pack_word_is_not_a_candidate(plugin_host):
    """整季字样不能让明确的 E11 或 E11–E12 混入首集与整季候选。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    assert rules.resource_candidate(_context("Show.S01E11.Complete", episodes=(11,)),
                                    1, "now", total_episode=12) is None
    assert rules.resource_candidate(_context("Show.S01E11-E12.Complete", episodes=(11, 12)),
                                    1, "now", total_episode=12) is None
    missing_list = _context("Show.S01E11.Complete", episodes=(11,))
    missing_list.meta_info.episode_list = []
    missing_list.meta_info.begin_episode = 11
    assert rules.resource_candidate(missing_list, 1, "now", total_episode=12) is None
    missing_list.meta_info.begin_episode = None
    assert rules.resource_candidate(missing_list, 1, "now", total_episode=12) is None
    wrongly_full = _context("Show.S01E11.Complete", episodes=tuple(range(1, 13)))
    assert rules.resource_candidate(wrongly_full, 1, "now", total_episode=12) is None
    assert rules.resource_candidate(_context("Show.S01E01.Complete", episodes=(1,)),
                                    1, "now", total_episode=12)["kind"] == "first_episode"


def test_reload_discards_saved_late_episode_pack_candidate(plugin_host):
    """升级时清理旧状态的误判候选和与之关联的预览。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    saved = host.storage["gate_state"]["entries"]["2"]
    saved["candidates"] = [{
        "id": "late", "kind": "suspected_full", "episodes": [11], "total_episode": 12,
        "title": "Show.S01E11.Complete", "first_seen": "old", "last_seen": "old",
    }, {
        "id": "unparsed-late", "kind": "suspected_full", "episodes": [], "total_episode": 12,
        "title": "Show.S01E11.Complete", "first_seen": "old", "last_seen": "old",
    }]
    saved["preview"] = {"choice": "2:late"}

    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    entry = host.storage["gate_state"]["entries"]["2"]
    assert entry["candidates"] == []
    assert "preview" not in entry
    assert _form_items(reloaded, "candidate") == []


def test_candidate_rules_recognize_spec_aliases_and_both_include_markers(plugin_host):
    """候选回填常见规格别名，并在标题验证两个包含标识。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    title = "Show.S01E01.x2160.H265.Atmos.CUSTOM-TEAM"
    context = _context(title)
    context.meta_info.resource_type = "HEVC"
    context.meta_info.resource_pix = "2160p"
    context.meta_info.resource_effect = "Dolby Atmos"
    context.meta_info.customization = "CUSTOM"
    candidate = rules.resource_candidate(context, 1, "now")
    assert candidate is not None
    for field in ("quality", "resolution", "effect", "include"):
        assert re.search(candidate["rules"][field], title, re.I)
    assert not re.search(candidate["rules"]["include"], title.replace("CUSTOM", "OTHER"), re.I)
    assert not re.search(candidate["rules"]["include"], title.replace("TEAM", "OTHER"), re.I)

    context.meta_info.resource_type = "UHD"
    context.meta_info.resource_effect = "SDR"
    context.torrent_info.title = "Show.S01E01.UHD.SDR.CUSTOM-TEAM"
    candidate = rules.resource_candidate(context, 1, "now")
    assert candidate is not None
    assert re.search(candidate["rules"]["quality"], context.torrent_info.title, re.I)
    assert re.search(candidate["rules"]["effect"], context.torrent_info.title, re.I)


def test_unrecognized_candidate_values_preserve_existing_subscription_rules(plugin_host):
    """候选未识别到字段时确认不清空订阅已有规则。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.quality = "WEB-DL"
    subscribe.resolution = "1080p"
    subscribe.effect = "HDR"
    subscribe.include = "TEAM"
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    context = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    context.meta_info.resource_type = "未识别质量"
    context.meta_info.resource_pix = "未识别分辨率"
    context.meta_info.resource_effect = "未识别特效"
    context.meta_info.resource_team = "未识别制作组"
    host.search_results.append(context)
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]
    success, _message, draft = plugin.preview_candidate(choice)
    assert success is True
    assert set(json.loads(draft)) == {"sites"}
    assert plugin.confirm_candidate(choice, draft)[0] is True
    assert (subscribe.quality, subscribe.resolution, subscribe.effect, subscribe.include) == \
           ("WEB-DL", "1080p", "HDR", "TEAM")


def test_only_public_detail_pages_are_kept(plugin_host):
    """详情链接保留站内资源 ID，过滤下载链接、票据与非网页协议。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    public = rules.public_page_url
    assert public("https://tracker.example/#/torrent/detail/100/") == \
        "https://tracker.example/#/torrent/detail/100/"
    assert public("https://tracker.example/details.php?id=42&passkey=secret") == \
        "https://tracker.example/details.php?id=42"
    assert public("javascript:alert(1)") is None
    assert public("https://user:password@tracker.example/details.php?id=42") is None
    assert public("https://tracker.example/download.php?id=42") is None
    assert public("https://tracker.example/dl/42/signed-ticket") is None
    assert public("https://tracker.example/details.php?id=42",
                  "https://tracker.example/details.php?id=42") is None
    preview = importlib.import_module(f"{plugin_host.module.__name__}.preview")
    later_episode = preview.preview_resource(_context(
        "Show.S01E02.1080p.WEB-DL", episodes=(2,),
        page_url="https://tracker.example/details.php?id=43&passkey=secret",
    ), 1, 8, "now")
    assert later_episode["kind"] == "episode"
    assert later_episode["page_url"] == "https://tracker.example/details.php?id=43"


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
    success, _message, _rules = plugin.preview_candidate(f"2:{candidate_id}")
    assert success is True
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


def test_daily_notice_lists_live_pending_subscriptions_and_uses_configured_time(plugin_host):
    """日报包含当前待确认剧集，忽略已批准及已删除记录。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    host.subscriptions[1].name = "历史剧"
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({
        "enabled": True, "daily_notice": True,
        "notice_hour": 20, "notice_minute": 15,
    })
    assert plugin.adopt_subscription(1)[0] is True
    for sid, name in ((2, "新剧"), (3, "已批准剧"), (4, "已删除剧")):
        host.subscriptions[sid] = _subscribe(sid)
        host.subscriptions[sid].name = name
        plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": sid}))
    assert plugin.approve_subscription(3)[0] is True
    del host.subscriptions[4]

    services = {item["id"]: item for item in plugin.get_service()}
    assert services["pending_daily_notice"]["trigger"] == "cron"
    assert services["pending_daily_notice"]["kwargs"] == {"hour": 20, "minute": 15}
    services["pending_daily_notice"]["func"]()

    assert len(host.messages) == 1
    assert host.messages[0] == {
        "mtype": "plugin",
        "title": "剧集守门员：待确认订阅日报",
        "text": "当前有 2 条待确认订阅。\n1. #1 历史剧 S01\n2. #2 新剧 S01",
    }


def test_daily_notice_reports_zero_and_respects_switch(plugin_host):
    """每天零待确认也发送汇总，关闭通知后不注册任务。"""
    plugin = plugin_host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    plugin.notify_pending_subscriptions()
    assert plugin_host.messages[0]["text"] == "当前有 0 条待确认订阅。\n暂无待确认的剧集订阅。"

    plugin.init_plugin({"enabled": True, "daily_notice": False})
    assert {item["id"] for item in plugin.get_service()} == {"candidate_refresh"}
    plugin.notify_pending_subscriptions()
    assert len(plugin_host.messages) == 1


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

    plugin.init_plugin({"enabled": True, "direct_subscription": "2", "operation": "direct_approve"})
    assert subscribe.state == "R"
    assert subscribe.quality == "WEB-DL"
    assert subscribe.sites == [7]
    assert host.storage["gate_state"]["entries"]["2"]["mode"] == "direct"
    assert host.storage["config"]["operation"] == "save"
    assert "search-2" in host.jobs
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is False

    subscribe.resolution = "2160p"
    plugin.init_plugin({"enabled": True, "revoke_subscription": "2", "operation": "revoke"})
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
    success, _message, _rules = plugin.preview_candidate(choice)
    assert success is True
    success, _message = plugin.confirm_candidate(choice)
    assert success is True
    subscribe.quality = "REMUX"

    success, _message = plugin.revoke_approval("2")
    assert success is True
    assert subscribe.state == "S"
    assert subscribe.quality == "REMUX"
    assert subscribe.resolution == r"1080[pi]?|x1080"
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


def test_historical_subscriptions_require_explicit_adoption(plugin_host):
    """
    旧订阅默认不受影响；手动纳入后仅恢复插件暂停过的订阅。
    """
    host = plugin_host
    active = _subscribe(1)
    active.state = "R"
    manually_paused = _subscribe(2)
    manually_paused.state = "S"
    host.subscriptions.update({1: active, 2: manually_paused})
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    assert host.storage["gate_state"]["baseline"] == 2
    assert len(_form_items(plugin, "historical_subscription")) == 2

    before = _download_event(1)
    plugin.intercept_download(before)
    assert before.event_data.cancel is False

    success, _message = plugin.adopt_subscription("1")
    assert success is True
    assert active.state == "S"
    pending = _download_event(1)
    plugin.intercept_download(pending)
    assert pending.event_data.cancel is True
    success, _message = plugin.approve_subscription("1")
    assert success is True
    assert active.state == "R"

    success, _message = plugin.adopt_subscription("2")
    assert success is True
    assert manually_paused.state == "S"
    success, _message = plugin.approve_subscription("2")
    assert success is True
    assert manually_paused.state == "S"
    assert host.storage["gate_state"]["entries"]["2"]["needs_search"] is False


def test_historical_choices_are_visible_before_first_enable(plugin_host):
    """
    未启用时可先选择现有剧集；启用并纳入时再冻结水位。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    host.subscriptions[2] = _subscribe(2, mtype="电影")
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": False})

    assert host.storage["gate_state"]["baseline"] is None
    assert [item["value"] for item in _form_items(plugin, "historical_subscription")] == [1]
    assert host.subscriptions[1].state == "N"

    plugin.init_plugin({"enabled": True, "historical_subscription": [1], "operation": "adopt"})
    assert host.storage["gate_state"]["baseline"] == 2
    assert host.storage["gate_state"]["entries"]["1"]["source"] == "historical"
    assert host.subscriptions[1].state == "S"


def test_multiple_historical_subscriptions_need_one_explicit_operation(plugin_host):
    """多选只在明确执行纳入时生效；无效选择不造成部分接管。"""
    host = plugin_host
    host.subscriptions.update({sid: _subscribe(sid) for sid in (1, 2, 3)})
    plugin = host.module.SubscribeOverwatcher()
    assert _form_field(plugin, "historical_subscription")["props"]["multiple"] is True
    assert plugin.get_form()[1]["operation"] == "save"

    plugin.init_plugin({"enabled": True, "historical_subscription": [1, 2], "operation": "save"})
    assert host.storage["gate_state"]["entries"] == {}
    assert host.subscriptions[1].state == "N"

    plugin.init_plugin({
        "enabled": True, "historical_subscription": [1, 2, 2], "operation": "adopt",
        "operation_request_id": "adopt-1",
    })
    assert set(host.storage["gate_state"]["entries"]) == {"1", "2"}
    assert all(host.subscriptions[sid].state == "S" for sid in (1, 2))
    assert host.storage["config"]["operation"] == "save"
    assert host.storage["config"]["historical_subscription"] == []
    assert plugin.get_form()[1]["_last_confirmation"]["request_id"] == "adopt-1"
    assert [item["value"] for item in _form_items(plugin, "historical_subscription")] == [3]

    plugin.init_plugin({
        "enabled": True, "historical_subscription": [3, 99], "operation": "adopt",
        "operation_request_id": "adopt-2",
    })
    assert "3" not in host.storage["gate_state"]["entries"]
    assert host.storage["gate_state"]["last_confirmation"]["success"] is False
    assert host.storage["config"]["historical_subscription"] == [3, 99]
    assert plugin.get_form()[1]["_last_confirmation"]["request_id"] == "adopt-2"


def test_old_single_history_selection_is_migrated(plugin_host):
    """升级时将旧版单选值转成多选值，旧操作开关仍可完成本次提交。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()

    plugin.init_plugin({"enabled": False, "historical_subscription": 1})
    assert host.storage["config"]["historical_subscription"] == [1]
    plugin.init_plugin({**host.storage["config"], "enabled": True, "adopt": True})
    assert host.storage["gate_state"]["entries"]["1"]["source"] == "historical"
    assert host.storage["config"]["historical_subscription"] == []


def test_custom_config_action_returns_result_and_refreshes_choices(plugin_host, monkeypatch):
    """分区按钮提交后返回真实执行结果，重新取表单可见最新选择项。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    for name in ("app.api", "app.api.dependencies", "app.api.dependencies.plugin"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)

    def update(plugin_id, config):
        assert plugin_id == "SubscribeOverwatcher"
        plugin.init_plugin(config)
        return SimpleNamespace(success=True, message="")

    sys.modules["app.api.dependencies.plugin"].get_plugin_config_command = lambda: SimpleNamespace(
        update=update,
    )
    result = plugin.apply_config({
        "enabled": True, "historical_subscription": [1], "operation": "adopt",
        "operation_request_id": "ui-adopt-1",
    })
    assert result["success"] is True
    assert "订阅 1 已纳入管理" in result["message"]
    assert plugin.get_form()[1]["_last_confirmation"]["request_id"] == "ui-adopt-1"
    assert _form_items(plugin, "historical_subscription") == []

    failed = plugin.apply_config({
        "enabled": True, "historical_subscription": [99], "operation": "adopt",
        "operation_request_id": "ui-adopt-2",
    })
    assert failed["success"] is False
    assert "99" in failed["message"]
    assert plugin.get_form()[1]["_last_confirmation"]["request_id"] == "ui-adopt-2"


def test_detail_shows_current_state_without_repeating_last_operation(plugin_host):
    """配置页仍可确认操作结果，详情页只展示当前管理状态。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({
        "enabled": True, "historical_subscription": [1], "operation": "adopt",
        "operation_request_id": "adopt-1",
    })

    assert plugin.get_form()[1]["_last_confirmation"]["request_id"] == "adopt-1"
    page_text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "当前管理 1 条剧集订阅" in page_text
    assert "订阅 1 已纳入管理" not in page_text


def test_custom_interface_bundle_is_ready(plugin_host):
    """插件市场安装后的 Vue 配置和详情入口都包含在插件目录内。"""
    plugin = plugin_host.module.SubscribeOverwatcher()
    mode, relative_path = plugin.get_render_mode()
    assert mode == "vue"
    entry = PLUGIN_DIR / relative_path / "remoteEntry.js"
    assert entry.is_file()
    assert "Config" in entry.read_text()
    assert "Page" in entry.read_text()


def test_historical_choices_show_progress_and_prioritize_less_completed(plugin_host):
    """纳入列表先显示零进度和低进度，未知进度明确标记。"""
    host = plugin_host
    for sid, lack in ((1, 8), (2, 2), (3, 7), (4, None)):
        host.subscriptions[sid] = _subscribe(sid)
        host.subscriptions[sid].name = f"剧集{sid}"
        host.subscriptions[sid].lack_episode = lack
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": False})

    choices = _form_items(plugin, "historical_subscription")
    assert [item["value"] for item in choices] == [1, 3, 2, 4]
    assert "已入库 0/8 集" in choices[0]["title"]
    assert "已入库 1/8 集" in choices[1]["title"]
    assert "订阅进度未知" in choices[3]["title"]

    washing = _subscribe(5)
    washing.best_version = 1
    washing.episode_priority = {"1": 100, "2": 80}
    assert host.module._progress_label(washing) == "洗版完成 1/8 集"


def test_upgrade_reload_keeps_managed_history_and_shows_current_status(plugin_host):
    """新版实例继续读取旧 gate_state，详情页列出手动纳入和新增接管记录。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    host.subscriptions[1].name = "历史剧"
    host.subscriptions[1].lack_episode = 6
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    assert plugin.adopt_subscription(1)[0] is True
    assert plugin.approve_subscription(1)[0] is True

    host.subscriptions[2] = _subscribe(2)
    host.subscriptions[2].name = "新剧"
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    saved = copy.deepcopy(host.storage["gate_state"])

    upgraded = host.module.SubscribeOverwatcher()
    upgraded.init_plugin({"enabled": True})
    entries = host.storage["gate_state"]["entries"]
    assert entries["1"]["status"] == "approved"
    assert entries["2"]["status"] == "pending"
    assert entries["2"]["candidates"] == saved["entries"]["2"]["candidates"]
    form_text = json.dumps(upgraded.get_form()[0], ensure_ascii=False)
    assert "已纳入管理 2 条" in form_text
    assert "#1 历史剧 S01（已批准）" in form_text
    assert "#2 新剧 S01（待确认）" in form_text

    table = next(item for item in upgraded.get_page()
                 if item["component"] == "VTable"
                 and "接管方式" in json.dumps(item, ensure_ascii=False))
    rows = [[cell["text"] for cell in row["content"]]
            for row in table["content"][1]["content"]]
    assert ["#1", "历史剧 S01", "手动纳入", "已批准", "已入库 2/8 集"] in rows
    assert ["#2", "新剧 S01", "新增接管", "待确认", "已入库 0/8 集"] in rows


def test_reenable_does_not_adopt_subscriptions_created_while_disabled(plugin_host):
    """
    停用期间的新订阅留给用户管理，原待确认记录仍被拦截。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    plugin.init_plugin({"enabled": False})
    host.subscriptions[3] = _subscribe(3)
    assert 3 in [item["value"] for item in _form_items(plugin, "historical_subscription")]
    plugin.init_plugin({"enabled": True})

    assert host.storage["gate_state"]["baseline"] == 3
    plugin.refresh_candidates()
    assert "2" in host.storage["gate_state"]["entries"]
    assert "3" not in host.storage["gate_state"]["entries"]
    managed = _download_event(2)
    plugin.intercept_download(managed)
    assert managed.event_data.cancel is True
    ignored = _download_event(3)
    plugin.intercept_download(ignored)
    assert ignored.event_data.cancel is False


def test_complete_and_cross_season_candidates_are_shown_before_first_episode(plugin_host):
    """
    核实全集优先，含目标季的跨季包进入候选但不冒充目标季全集。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    first = _context("Show.S01E01.1080p.WEB-DL", episodes=(1,))
    full_1080 = _context(
        "Show.S01.Complete.1080p.WEB-DL", episodes=tuple(range(1, 9)),
        page_url="https://tracker.example/details.php?id=1080",
    )
    full_2160 = _context("Show.S01.Complete.2160p.WEB-DL", episodes=tuple(range(1, 9)))
    suspect = _context("Show.S01.Complete.Unknown", episodes=(1, 2))
    cross_season = _context("Show.S01-S02.Complete", episodes=tuple(range(1, 9)))
    cross_season.meta_info.season_list = [1, 2]
    cross_season.meta_info.end_season = 2
    other_season = _context("Show.S03.Complete", episodes=tuple(range(1, 9)))
    other_season.meta_info.begin_season = 3
    other_season.meta_info.season_list = [3]
    host.search_results.extend([first, full_1080, full_2160, suspect, cross_season, other_season])

    plugin.refresh_candidates()
    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert [item["kind"] for item in candidates] == [
        "full_season", "full_season", "cross_season", "suspected_full", "first_episode",
    ]
    assert all(item["episodes"] == list(range(1, 9)) for item in candidates[:2])
    assert candidates[2]["seasons"] == [1, 2]
    assert "各季集数待核实" in host.module._candidate_coverage(candidates[2])
    assert "secret.example" not in json.dumps(candidates)
    assert "集数覆盖全集" in _form_items(plugin, "candidate")[0]["title"]
    assert next(item["page_url"] for item in _form_items(plugin, "candidate")
                if "Complete.1080p" in item["title"]) == \
        "https://tracker.example/details.php?id=1080"
    candidate_table = next(item for item in plugin.get_page()
                           if item["component"] == "VTable"
                           and "最后发现" in json.dumps(item, ensure_ascii=False))
    links = [node for row in candidate_table["content"][1]["content"]
             for cell in row["content"] for node in cell.get("content", [])
             if node["component"] == "a"]
    assert len(links) == 1
    assert links[0]["text"] == "样例站"
    assert links[0]["props"]["href"] == "https://tracker.example/details.php?id=1080"
    assert links[0]["props"]["rel"] == "noopener noreferrer"
    assert all(row["content"][3].get("content") is None
               for row in candidate_table["content"][1]["content"])


def test_cross_season_candidate_requires_target_season_and_preview_keeps_uncertainty(plugin_host):
    """跨季包可供目标季选择，但不借混合集号断言该季全集。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    preview = importlib.import_module(f"{plugin_host.module.__name__}.preview")
    context = _context("Show.S01-S02.Complete", episodes=tuple(range(1, 9)))
    context.meta_info.season_list = [1, 2]
    context.meta_info.end_season = 2
    first = rules.resource_candidate(context, 1, "now", total_episode=8)
    second = rules.resource_candidate(context, 2, "now", total_episode=8)
    assert first["kind"] == second["kind"] == "cross_season"
    assert first["target_season"] == 1
    assert second["target_season"] == 2
    assert rules.resource_candidate(context, 3, "now", total_episode=8) is None
    assert preview.preview_resource(context, 1, 8, "now")["kind"] == "cross_season"
    context.meta_info.begin_season = 0
    context.meta_info.season_list = [0, 1]
    context.meta_info.end_season = 1
    assert rules.resource_candidate(context, 1, "now", total_episode=8)["seasons"] == [0, 1]


def test_cross_season_resource_is_not_eligible_for_manual_episode_replacement(plugin_host):
    """跨季资源可作为订阅候选，不能用混合集号提交目标季单集回刷。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.approve_subscription(2)[0] is True
    context = _context("Show.S01-S02.Complete", episodes=tuple(range(1, 9)))
    context.meta_info.season_list = [1, 2]
    context.meta_info.end_season = 2
    host.search_results.append(context)

    assert plugin.preview_replacements(2)[0] is True
    preview = plugin._state["entries"]["2"]["replacement_preview"]
    assert preview["matched_count"] == 0
    assert preview["rejected_count"] == 1
    assert "跨季资源无法确定目标季集数" in preview["rejected"][0]["reason"]
    assert _form_items(plugin, "replace_resource") == []

    # 旧预览即使错误地把跨季资源列为通过，提交前也必须再次拦截。
    item = preview["rejected"][0]
    preview["matched"] = [item]
    preview["matched_count"] = 1
    assert plugin.submit_replacement(f"2:{item['id']}", "1")[0] is False
    assert host.downloads == []


def test_cross_site_same_version_is_grouped_only_for_display(plugin_host):
    """同标题同大小跨站候选合并展示，站点链接与单站确认保持独立。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    title = "Show.S01E01.1080p.WEB-DL.HDR-TEAM"
    first = _context(title, page_url="https://one.example/details.php?id=11")
    second = _context(title, page_url="https://two.example/details.php?id=22")
    second.torrent_info.site = 7
    second.torrent_info.site_name = "另一个站"
    host.search_results.extend([first, second])
    plugin.refresh_candidates()

    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert len(candidates) == 2
    same_site = {**candidates[0], "id": "another-id"}
    different_size = {**candidates[1], "size_bytes": 2048}
    assert len(host.module._display_groups([candidates[0], same_site])) == 2
    assert len(host.module._display_groups([candidates[0], different_size])) == 2
    choices = _form_items(plugin, "candidate")
    assert len(choices) == 2
    candidate_table = next(item for item in plugin.get_page()
                           if item["component"] == "VTable"
                           and "最后发现" in json.dumps(item, ensure_ascii=False))
    rows = candidate_table["content"][1]["content"]
    assert len(rows) == 1
    site_links = [item for item in rows[0]["content"][2]["content"] if item["component"] == "a"]
    assert {item["props"]["href"] for item in site_links} == {
        "https://one.example/details.php?id=11", "https://two.example/details.php?id=22",
    }
    assert rows[0]["content"][3] == {"component": "td", "text": title}

    choice = next(item["value"] for item in choices if "另一个站" in item["title"])
    assert plugin.preview_candidate(choice, '{"sites": []}')[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 2
    preview_table = next(item for item in plugin.get_page()
                         if item["component"] == "VTable"
                         and "结果" in json.dumps(item["content"][0], ensure_ascii=False))
    assert len(preview_table["content"][1]["content"]) == 1
    assert len([node for node in preview_table["content"][1]["content"][0]["content"][3]["content"]
                if node["component"] == "a"]) == 2
    assert preview_table["content"][1]["content"][0]["content"][4] == \
        {"component": "td", "text": title}
    assert plugin.preview_candidate(choice)[0] is True
    assert plugin.confirm_candidate(choice)[0] is True
    assert host.subscriptions[2].sites == [7]


def test_all_sites_mode_clears_site_filter_and_keeps_candidate_specs(plugin_host):
    """跨站确认可清除站点限制，仍回填所选版本的其他规格。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.sites = [5]
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    first = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    second = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    second.torrent_info.site = 7
    second.torrent_info.site_name = "另一个站"
    host.search_results.extend([first, second])
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]

    assert plugin.preview_candidate(choice)[0] is True
    assert plugin.confirm_candidate(choice, rule_mode="all_sites")[0] is False
    plugin.init_plugin({
        "enabled": True, "candidate": choice, "rule_mode": "all_sites", "operation": "preview",
    })
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 2
    assert preview["rules"]["sites"] == []
    plugin.init_plugin({**host.storage["config"], "operation": "confirm"})
    assert subscribe.sites == []
    assert subscribe.quality == r"WEB[- .]?(?:DL|RIP)"
    assert subscribe.resolution == r"1080[pi]?|x1080"
    assert subscribe.effect == "HDR"
    assert subscribe.include == "TEAM"
    assert host.storage["config"]["rule_mode"] == "candidate"


def test_candidate_labels_do_not_claim_unrecognized_or_sparse_episodes_are_complete(plugin_host):
    """标题整季词和已识别集号分别展示，跳号不能渲染成连续覆盖。"""
    module = plugin_host.module
    candidate = {"kind": "suspected_full", "episodes": [], "total_episode": 8}
    assert module._candidate_label(candidate) == "标题疑似整季"
    assert module._candidate_coverage(candidate) == "未识别具体集号（目标总集数 8）"
    candidate["episodes"] = [1, 3, 4, 8]
    assert module._candidate_coverage(candidate) == "E01, E03–E04, E08/8"


def test_preview_shows_matches_rejections_and_requires_same_draft(plugin_host):
    """
    预览仅生成安全快照；无匹配或草稿改变都不能批准。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    chosen = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM",
                      page_url="https://tracker.example/details.php?id=1")
    rejected = _context("Show.S01E02.2160p.BluRay.DV-GROUP", episodes=(2,))
    cross_season = _context("Show.S01-S02.1080p.WEB-DL.HDR-TEAM")
    cross_season.meta_info.end_season = 2
    unconfirmed_identity = _context("Show.S01E03.1080p.WEB-DL.HDR-TEAM", episodes=(3,))
    unconfirmed_identity.match_status = "candidate"
    host.search_results.extend([chosen, rejected, cross_season, unconfirmed_identity])
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]

    success, _message, draft = plugin.preview_candidate(choice)
    assert success is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 1
    assert preview["rejected"][0]["reason"]
    assert "secret.example" not in json.dumps(preview)
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].quality is None
    page_text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "符合当前规则" in page_text
    assert "排除" in page_text
    assert "规则预览" in page_text
    preview_table = next(item for item in plugin.get_page()
                         if item["component"] == "VTable"
                         and "结果" in json.dumps(item["content"][0], ensure_ascii=False))
    assert preview_table["content"][1]["content"][0]["content"][3]["content"][0]["props"]["href"] == \
        "https://tracker.example/details.php?id=1"
    assert preview_table["content"][1]["content"][0]["content"][4] == \
        {"component": "td", "text": chosen.torrent_info.title}

    success, message = plugin.confirm_candidate(choice, draft.replace("1080", "2160"))
    assert success is False
    assert "重新预览" in message
    assert host.subscriptions[2].state == "S"

    host.search_results.clear()
    success, _message, _draft = plugin.preview_candidate(choice)
    assert success is True
    success, message = plugin.confirm_candidate(choice)
    assert success is False
    assert "未通过" in message


def test_multiple_full_season_versions_need_narrower_rules(plugin_host):
    """
    多个全集版本同时通过时，选择全集模板不能直接放行。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    full_1 = _context("Show.S01.Complete.1080p.WEB-DL.HDR-TEAM.A", episodes=tuple(range(1, 9)))
    full_2 = _context("Show.S01.Complete.1080p.WEB-DL.HDR-TEAM.B", episodes=tuple(range(1, 9)))
    host.search_results.extend([full_1, full_2])
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]
    success, _message, _draft = plugin.preview_candidate(choice)
    assert success is True
    success, message = plugin.confirm_candidate(choice)
    assert success is False
    assert "多个全集版本" in message
    assert host.subscriptions[2].state == "S"
    assert "多个集数覆盖全集" in json.dumps(plugin.get_page(), ensure_ascii=False)

    success, _message, draft = plugin.preview_candidate(choice, r'{"include": "TEAM\\.A"}')
    assert success is True
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 1
    success, _message = plugin.confirm_candidate(choice, draft)
    assert success is True


def test_same_full_season_version_across_sites_counts_once(plugin_host):
    """同标题同大小的跨站整季资源按一个版本确认。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    title = "Show.S01.Complete.1080p.WEB-DL.HDR-TEAM"
    first = _context(title, episodes=tuple(range(1, 9)))
    second = _context(title, episodes=tuple(range(1, 9)))
    second.torrent_info.site = 7
    second.torrent_info.site_name = "另一个站"
    host.search_results.extend([first, second])
    plugin.refresh_candidates()
    choice = next(item["value"] for item in _form_items(plugin, "candidate")
                  if "另一个站" in item["title"])
    success, _message, draft = plugin.preview_candidate(choice, '{"sites": []}')
    assert success is True
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 2
    assert "规则匹配多个集数覆盖全集的版本" not in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert plugin.confirm_candidate(choice, draft)[0] is True


def test_approved_subscription_can_preview_and_submit_selected_episodes(plugin_host):
    """
    已批准订阅按当前规则预览，明确指定集数后才提交单资源下载。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.quality = "WEB-DL"
    subscribe.sites = [5]
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.approve_subscription("2")[0] is True
    full = _context("Show.S01.Complete.1080p.WEB-DL.HDR-TEAM", episodes=tuple(range(1, 9)))
    other = _context("Show.S01E02.2160p.BluRay.DV-GROUP", episodes=(2,))
    unknown_episodes = _context("Show.S01.Complete.1080p.WEB-DL.HDR-TEAM.Unknown")
    unknown_episodes.meta_info.episode_list = []
    unknown_episodes.meta_info.begin_episode = None
    unknown_episodes.meta_info.end_episode = None
    host.search_results.extend([other, unknown_episodes, full])

    plugin.init_plugin({"enabled": True, "replace_subscription": "2", "operation": "preview_replace"})
    preview = host.storage["gate_state"]["entries"]["2"]["replacement_preview"]
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 2
    assert any("未明确识别集数" in item["reason"] for item in preview["rejected"])
    assert "secret.example" not in json.dumps(preview)
    choice = _form_items(plugin, "replace_resource")[0]["value"]
    assert "集数覆盖全集" in _form_items(plugin, "replace_resource")[0]["title"]
    plugin.init_plugin({
        "enabled": True, "replace_resource": choice, "replace_episodes": "1,3-5",
        "operation": "submit_replace",
    })
    assert len(host.downloads) == 1
    context, options = host.downloads[0]
    assert context is full
    assert options["episodes"] == {1, 3, 4, 5}
    assert options["source"] == 'Subscribe|{"id": 2}'
    assert options["return_detail"] is True
    assert host.storage["config"]["operation"] == "save"
    history = host.storage["gate_state"]["entries"]["2"]["replacement_history"]
    assert history[0]["episodes"] == [1, 3, 4, 5]
    assert "secret.example" not in json.dumps(history)
    assert len(host.search_calls) == 2
    assert all(call["rule_groups"] == [] for call in host.search_calls)


def test_replacement_rechecks_rules_resource_and_episode_coverage(plugin_host):
    """
    修改订阅、资源消失或缺少指定集数时均不提交下载。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.quality = "WEB-DL"
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.approve_subscription("2")[0] is True
    context = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    host.search_results.append(context)
    assert plugin.preview_replacements("2")[0] is True
    choice = _form_items(plugin, "replace_resource")[0]["value"]

    assert plugin.submit_replacement(choice, "2")[0] is False
    assert plugin.submit_replacement(choice, "0")[0] is False
    host.search_results.clear()
    assert plugin.submit_replacement(choice, "1")[0] is False
    host.search_results.extend([context, copy.deepcopy(context)])
    assert plugin.submit_replacement(choice, "1")[0] is False
    host.search_results.pop()
    context.meta_info.episode_list = [True]
    assert plugin.submit_replacement(choice, "1")[0] is False
    context.meta_info.episode_list = [1]
    subscribe.quality = "BluRay"
    assert plugin.submit_replacement(choice, "1")[0] is False
    assert not host.downloads


def test_replacement_download_failure_and_revocation_keep_state_safe(plugin_host, monkeypatch):
    """
    下载器拒绝不记成功；撤销批准会废弃已生成的回刷预览。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.approve_subscription("2")[0] is True
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    assert plugin.preview_replacements("2")[0] is True
    choice = _form_items(plugin, "replace_resource")[0]["value"]
    monkeypatch.setattr(host.module.DownloadChain, "download_single", lambda *_args, **_kwargs: (None, "失败"))
    success, message = plugin.submit_replacement(choice, "1")
    assert success is False
    assert "未接受" in message
    assert "replacement_history" not in host.storage["gate_state"]["entries"]["2"]
    assert plugin.revoke_approval("2")[0] is True
    assert "replacement_preview" not in host.storage["gate_state"]["entries"]["2"]
    assert plugin.submit_replacement(choice, "1")[0] is False


def test_expired_previews_and_changed_approved_rules_do_not_release_download(plugin_host):
    """
    确认与回刷预览过期后失效；候选批准后的下载仍检查当前规则。
    """
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    context = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    host.search_results.append(context)
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]
    assert plugin.preview_candidate(choice)[0] is True
    host.storage["gate_state"]["entries"]["2"]["preview"]["searched_ts"] = 0
    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    assert reloaded.confirm_candidate(choice)[0] is False
    assert subscribe.state == "S"

    assert reloaded.preview_candidate(choice)[0] is True
    assert reloaded.confirm_candidate(choice)[0] is True
    subscribe.quality = "BluRay"
    blocked = _download_event(2, context=context, episodes={1})
    reloaded.intercept_download(blocked)
    assert blocked.event_data.cancel is True
    assert "当前规则未匹配" in blocked.event_data.reason

    subscribe.quality = "WEB-DL"
    assert reloaded.preview_replacements("2")[0] is True
    replace_choice = _form_items(reloaded, "replace_resource")[0]["value"]
    host.storage["gate_state"]["entries"]["2"]["replacement_preview"]["searched_ts"] = 0
    final_reload = host.module.SubscribeOverwatcher()
    final_reload.init_plugin({"enabled": True})
    assert final_reload.submit_replacement(replace_choice, "1")[0] is False
    assert not host.downloads

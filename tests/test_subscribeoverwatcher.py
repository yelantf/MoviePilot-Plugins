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
    directories = []
    directory_calls = []
    group_filter_calls = []
    system_values = {}

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

        def filter_torrents(self, **kwargs):
            """记录规则组调用，默认保留资源；具体组行为由场景提供。"""
            group_filter_calls.append(kwargs)
            return kwargs["torrent_list"]

    class SystemConfigOper:
        def get(self, key):
            return copy.deepcopy(system_values.get(key))

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

    class DirectoryHelper:
        """
        提供隔离的宿主目录查询接口，记录分类路径委派。
        """

        def get_download_dirs(self):
            """
            返回按优先级排列的内存目录卡片。
            """
            return sorted(directories, key=lambda item: item.priority)

        def get_download_dir(self, media):
            """
            返回首个适用的默认下载目录。
            """
            return next((item for item in self.get_download_dirs() if self.matches_media(item, media)), None)

        @staticmethod
        def has_fixed_category(directory):
            """
            返回目录是否指定固定类别。
            """
            return bool(directory.media_category_id or directory.media_category)

        def matches_media(self, directory, media):
            """
            按替身媒体标记核对卡片适用范围。
            """
            return not getattr(media, "directory_mismatch", False) and directory.media_type in (None, "", "tv", "电视剧")

        def get_download_dir_by_save_path(self, media, path):
            """
            模拟宿主只让精确资源根目录继承分类开关的合同。
            """
            return next((item for item in self.get_download_dirs()
                         if path == file_uri(item.storage, item.download_path)
                         and self.matches_media(item, media)), None)

    def file_uri(storage, path):
        """
        将内存目录投影为本地或远端保存路径。
        """
        return str(path) if storage in (None, "local") else f"{storage}:{path}"

    def build_media_download_path(root, directory, media, helper):
        """
        记录所选卡片，返回宿主替身预设的分类结果。
        """
        del helper
        directory_calls.append(directory.name)
        relative = getattr(media, "download_relative", "") if directory.download_category_folder else ""
        return root / relative

    def validate_download_save_path(path):
        """
        只允许内存配置资源根目录及其子路径。
        """
        roots = [file_uri(item.storage, item.download_path) for item in directories]
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
            return path
        raise ValueError("保存路径不在允许的下载目录范围内")

    class FileURI:
        """
        提供宿主保存路径的存储前缀投影。

        :param storage: 内存存储类型
        :param path: 资源保存路径
        """

        def __init__(self, storage, path):
            """
            保存与宿主 URI 合同一致的路径。

            :param storage: 存储类型
            :param path: 资源保存路径
            """
            self.storage = storage
            self.path = path
            self.uri = file_uri(storage, path)

        @classmethod
        def from_uri(cls, uri):
            """
            解析测试使用的本地或远端下载路径。

            :param uri: 已校验的保存路径
            :return: 含存储和路径字段的 URI 对象
            """
            storage, path = uri.split(":", 1) if ":" in uri else ("local", uri)
            return cls(storage, path)

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
        "app.application.directory": {
            "DirectoryHelper": DirectoryHelper, "build_media_download_path": build_media_download_path,
            "validate_download_save_path": validate_download_save_path,
        },
        "app.schemas.file": {"FileURI": FileURI},
        "app.application.torrent.download": {"TorrentHelper": TorrentHelper},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.chain.media": {"MediaChain": MediaChain},
        "app.chain.search": {"SearchChain": SearchChain},
        "app.chain.subscribe": {"SubscribeChain": SubscribeChain},
        "app.db.oper.subscribe": {"SubscribeOper": SubscribeOper},
        "app.db.oper.systemconfig": {"SystemConfigOper": SystemConfigOper},
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
            "SystemConfigKey": SimpleNamespace(
                UserFilterRuleGroups="UserFilterRuleGroups", CustomFilterRules="CustomFilterRules",
                SubscribeFilterRuleGroups="SubscribeFilterRuleGroups",
                BestVersionFilterRuleGroups="BestVersionFilterRuleGroups",
            ),
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
    # 搜索适配器继承当前夹具的宿主替身，不能跨场景复用上一个宿主的基类。
    monkeypatch.delitem(sys.modules, f"{name}.search", raising=False)
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    directory_spec = importlib.util.spec_from_file_location(f"{name}.directory", PLUGIN_DIR / "directory.py")
    directory_module = importlib.util.module_from_spec(directory_spec)
    monkeypatch.setitem(sys.modules, f"{name}.directory", directory_module)
    directory_spec.loader.exec_module(directory_module)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        module=module, subscriptions=subscriptions, storage=storage, jobs=jobs,
        search_results=search_results, search_calls=search_calls, downloads=downloads,
        messages=messages,
        directories=directories, directory_calls=directory_calls,
        directory_module=directory_module,
        group_filter_calls=group_filter_calls, system_values=system_values,
    )


def _subscribe(sid, mtype="电视剧"):
    """
    建立一条包含确认身份字段的新订阅。
    """
    return SimpleNamespace(
        id=sid, type=mtype, media_source="themoviedb", media_id="123",
        season=1, episode_group=None, activity_key=f"activity-{sid}", name="样例剧", state="N",
        total_episode=8, lack_episode=8, start_episode=1, best_version=0,
        quality=None, resolution=None, effect=None, include=None, sites=[], filter_groups=[],
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


def _directory(name, root, **overrides):
    """
    建立可修改的目录卡片替身。

    :param name: 目录卡片名称
    :param root: MoviePilot 侧的资源根目录
    :param overrides: 分类开关、存储类型等配置覆盖值
    :return: 具有宿主目录配置字段的内存对象
    """
    return SimpleNamespace(**{
        "name": name, "download_path": root, "storage": "local", "priority": 0,
        "media_type": None, "media_category": None, "media_category_id": None,
        "download_type_folder": True, "download_category_folder": True,
        "library_path": f"/library/{name}", "library_storage": "local", "transfer_type": "link",
        **overrides,
    })


def _prepare_directory_confirmation(host):
    """
    准备一个有完整媒体分类信息的待确认版本。

    :param host: 隔离宿主替身
    :return: 插件、候选选择值和下载上下文
    """
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    host.subscriptions[2].save_path = "/old/downloads"
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    context = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")
    context.media_info = SimpleNamespace(download_relative="电视剧/国产剧")
    host.search_results.append(context)
    plugin.refresh_candidates()
    return plugin, _form_items(plugin, "candidate")[0]["value"], context


def test_directory_confirmation_previews_full_path_and_only_fills_root(plugin_host):
    """
    预览显示分类全路径，确认只回填根目录，重载后也不改写下载事件。
    """
    host = plugin_host
    host.directories.extend([_directory("A盘", "/diskA/downloads"),
                             _directory("B盘", "/PtResourceDir2/bt/downloads", priority=1)])
    plugin, choice, context = _prepare_directory_confirmation(host)
    root = _form_items(plugin, "download_directory")[1]["value"]
    assert root == "/PtResourceDir2/bt/downloads"
    config = {"enabled": True, "candidate": choice, "download_directory": root, "operation": "preview"}
    plugin.init_plugin(config)
    assert host.subscriptions[2].save_path == "/old/downloads"
    assert "预计下载目录：/PtResourceDir2/bt/downloads/电视剧/国产剧" in \
        host.storage["gate_state"]["last_confirmation"]["message"]
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "预计下载目录" in page
    assert "/PtResourceDir2/bt/downloads/电视剧/国产剧" in page
    plugin.init_plugin({**host.storage["config"], "operation": "confirm"})
    assert host.storage["gate_state"]["last_confirmation"]["success"] is True
    assert host.subscriptions[2].save_path == root
    assert host.storage["config"]["download_directory"] == ""
    assert "download_directory" not in host.storage["gate_state"]["entries"]["2"]

    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    event = _download_event(2, context=context)
    event.event_data.options = {"save_path": root}
    host.directory_calls.clear()
    reloaded.intercept_download(event)
    assert event.event_data.cancel is False
    assert event.event_data.options["save_path"] == root
    assert host.directory_calls == []


@pytest.mark.parametrize("saved, expected", [
    (None, "/a/电视剧/国产剧"), ("", "/a/电视剧/国产剧"),
    ("/b", "/b/电视剧/国产剧"), ("/b/custom", "/b/custom"),
])
def test_unselected_directory_previews_original_or_default_without_filling(plugin_host, saved, expected):
    """
    留空目录选择时预览原保存位置或宿主默认目录，确认保持原值。

    :param plugin_host: 隔离宿主替身
    :param saved: 原订阅保存路径，空值代表默认目录
    :param expected: 宿主规则预期生成的完整路径
    """
    host = plugin_host
    host.directories.extend([_directory("A", "/a"), _directory("B", "/b", priority=1)])
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    host.subscriptions[2].save_path = saved
    assert plugin.preview_candidate(choice)[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched"][0]["download_path"] == expected
    assert plugin.confirm_candidate(choice)[0] is True
    assert host.subscriptions[2].save_path == saved


@pytest.mark.parametrize("change", ["delete", "folder", "save_path"])
def test_directory_changes_after_confirmation_remain_host_owned(plugin_host, change):
    """
    批准后目录卡片或保存路径变化时，插件只核对版本规则，不额外取消或改写路径。

    :param plugin_host: 隔离宿主替身
    :param change: 本次下载前模拟的目录变化
    """
    host = plugin_host
    host.directories.append(_directory("B", "/b"))
    plugin, choice, context = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice, download_directory="/b")[0] is True
    assert plugin.confirm_candidate(choice, download_directory="/b")[0] is True
    if change == "delete":
        host.directories.clear()
    elif change == "folder":
        host.directories[0].download_category_folder = False
    else:
        host.subscriptions[2].save_path = "/manual"
    event = _download_event(2, context=context)
    event.event_data.options = {"save_path": host.subscriptions[2].save_path}
    host.directory_calls.clear()
    plugin.intercept_download(event)
    assert event.event_data.cancel is False
    assert event.event_data.options["save_path"] == host.subscriptions[2].save_path
    assert host.directory_calls == []


def test_directory_selection_changes_require_preview_and_removed_roots_cannot_be_filled(plugin_host):
    """
    变更目录选择需要重新预览，已删除的根目录不能在确认时回填。
    """
    host = plugin_host
    host.directories.extend([_directory("A", "/a"), _directory("B", "/b")])
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice, download_directory="/a")[0] is True
    assert plugin.confirm_candidate(choice, download_directory="/b")[0] is False
    host.directories.pop(0)
    assert plugin.confirm_candidate(choice, download_directory="/a")[0] is False
    assert host.subscriptions[2].save_path == "/old/downloads"


def test_directory_options_deduplicate_roots_and_preview_uses_host_matching(plugin_host):
    """
    同根卡片按根目录去重，预览遵循宿主匹配结果，名称及分类开关不形成插件绑定。
    """
    host = plugin_host
    host.directories.extend([
        _directory("直接保存", "/downloads", download_category_folder=False),
        _directory("分类保存", "/downloads", priority=1),
        _directory("电影", "/movies", media_type="电影"),
    ])
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    assert _form_items(plugin, "download_directory") == [
        {"title": "直接保存 · /downloads", "value": "/downloads"},
    ]
    assert plugin.preview_candidate(choice, download_directory="/downloads")[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched"][0]["download_path"] == "/downloads"
    host.directories[0].name = "改名"
    host.directories[0].download_category_folder = True
    assert plugin.confirm_candidate(choice, download_directory="/downloads")[0] is True
    assert host.subscriptions[2].save_path == "/downloads"


def test_remote_directory_preview_preserves_prefix_but_fills_only_root(plugin_host):
    """
    远端目录预览保留存储前缀及分类子目录，确认仅写入完整根目录 URI。
    """
    host = plugin_host
    host.directories.append(_directory("远端", "/downloads", storage="rclone"))
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    root = _form_items(plugin, "download_directory")[0]["value"]
    assert plugin.preview_candidate(choice, download_directory=root)[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched"][0]["download_path"] == "rclone:/downloads/电视剧/国产剧"
    assert plugin.confirm_candidate(choice, download_directory=root)[0] is True
    assert host.subscriptions[2].save_path == "rclone:/downloads"


def test_missing_media_information_is_shown_without_changing_rule_matches(plugin_host):
    """
    路径预览失败只显示原因，不改变版本规则的匹配结果。
    """
    host = plugin_host
    plugin, choice, context = _prepare_directory_confirmation(host)
    context.media_info = None
    assert plugin.preview_candidate(choice)[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["download_path_error"] == "缺少媒体信息，无法预览下载目录"
    assert plugin.confirm_candidate(choice)[0] is True


def test_version_confirmation_lists_only_pending_series_with_candidates(plugin_host):
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})

    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert _form_items(plugin, "candidate_subscription") == []

    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    host.subscriptions[3] = _subscribe(3)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 3}))

    assert _form_items(plugin, "candidate_subscription") == [
        {"title": "#2 样例剧 S01", "value": "2"},
    ]
    assert {item["value"] for item in _form_items(plugin, "direct_subscription")} == {"2", "3"}
    assert {item["subscription_id"] for item in _form_items(plugin, "candidate")} == {"2"}


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
    assert updated.quality == "WEB-?DL|WEB-?RIP"
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
    assert subscribe.quality == "Blu-?Ray"
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

    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.refresh_candidates()
    choice = _form_items(plugin, "candidate")[0]["value"]
    previewed = plugin.apply_config({
        "enabled": True, "candidate": choice, "operation": "preview",
        "operation_request_id": "ui-preview-1",
    })
    assert previewed["success"] is True
    assert previewed["preview"] == plugin.get_form()[1]["_candidate_preview"]
    assert previewed["preview"]["choice"] == choice
    assert previewed["preview"]["table"]["component"] == "VTable"
    assert "secret.example" not in json.dumps(previewed)


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
    """仅同大小的跨站候选合并展示，并在选择项和表格显示大小。"""
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
    third = _context(title, page_url="https://three.example/details.php?id=33")
    third.torrent_info.site = 9
    third.torrent_info.site_name = "第三个站"
    first.torrent_info.size = second.torrent_info.size = 1024 ** 3
    third.torrent_info.size = 1024 ** 3 + 1
    host.search_results.extend([first, second, third])
    plugin.refresh_candidates()

    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert len(candidates) == 3
    by_site = {item["site_name"]: item for item in candidates}
    first_candidate = by_site["样例站"]
    second_candidate = by_site["另一个站"]
    third_candidate = by_site["第三个站"]
    same_site = {**first_candidate, "id": "another-id"}
    unknown_size = {**second_candidate, "size_bytes": None}
    different_episode = {**second_candidate, "episodes": [2]}
    different_quality = {**second_candidate, "quality": "BluRay"}
    assert len(host.module._display_groups([first_candidate, second_candidate])) == 1
    assert len(host.module._display_groups([first_candidate, same_site])) == 2
    assert len(host.module._display_groups([first_candidate, third_candidate])) == 2
    assert len(host.module._display_groups([first_candidate, unknown_size])) == 2
    assert len(host.module._display_groups([first_candidate, different_episode])) == 2
    assert len(host.module._display_groups([first_candidate, different_quality])) == 2
    assert host.module._size_label(None) == "未知"
    choices = _form_items(plugin, "candidate")
    assert len(choices) == 3
    assert all("1.00 GiB（1,073,741,824 B）" in item["title"]
               for item in choices if "第三个站" not in item["title"])
    assert "1.00 GiB（1,073,741,825 B）" in next(
        item["title"] for item in choices if "第三个站" in item["title"]
    )
    assert {tuple(item["rules"]["sites"]) for item in choices} == {(5,), (7,), (9,)}
    assert host.module._size_label(1024 ** 3, exact=True) != \
        host.module._size_label(1024 ** 3 + 1, exact=True)
    candidate_table = next(item for item in plugin.get_page()
                           if item["component"] == "VTable"
                           and "最后发现" in json.dumps(item, ensure_ascii=False))
    rows = candidate_table["content"][1]["content"]
    assert len(rows) == 2
    grouped_row = next(row for row in rows
                       if row["content"][4]["content"][1]["text"] == "1,073,741,824 B")
    other_row = next(row for row in rows
                     if row["content"][4]["content"][1]["text"] == "1,073,741,825 B")
    assert grouped_row["content"][4]["content"][0]["text"] == "1.00 GiB"
    assert other_row["content"][4]["content"][0]["text"] == "1.00 GiB"
    assert grouped_row["content"][4]["props"]["title"] == "精确大小：1,073,741,824 B"
    assert other_row["content"][4]["props"]["title"] == "精确大小：1,073,741,825 B"
    site_links = [item for item in grouped_row["content"][2]["content"] if item["component"] == "a"]
    assert {item["props"]["href"] for item in site_links} == {
        "https://one.example/details.php?id=11", "https://two.example/details.php?id=22",
    }
    assert other_row["content"][2]["content"][0]["props"]["href"] == \
        "https://three.example/details.php?id=33"
    assert all(row["content"][3] == {"component": "td", "text": title} for row in rows)
    actions = grouped_row["content"][-1]["content"]
    assert {action["props"]["data-candidate-id"] for action in actions} == {
        f"2:{first_candidate['id']}", f"2:{second_candidate['id']}",
    }
    assert {action["content"][0]["text"] for action in actions} == {"配置·样例站", "配置·另一个站"}
    assert other_row["content"][-1]["content"][0]["props"]["data-candidate-id"] == \
        f"2:{third_candidate['id']}"

    choice = next(item["value"] for item in choices if "另一个站" in item["title"])
    assert plugin.preview_candidate(choice, '{"sites": []}')[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 3
    preview_table = next(item for item in plugin.get_page()
                         if item["component"] == "VTable"
                         and "结果" in json.dumps(item["content"][0], ensure_ascii=False))
    preview_rows = preview_table["content"][1]["content"]
    assert len(preview_rows) == 2
    grouped_preview = next(row for row in preview_rows
                           if row["content"][5]["content"][1]["text"] == "1,073,741,824 B")
    assert any(row["content"][5]["content"][1]["text"] == "1,073,741,825 B"
               for row in preview_rows)
    assert all(row["content"][5]["content"][0]["text"] == "1.00 GiB" for row in preview_rows)
    assert grouped_preview["content"][5]["props"]["title"] == "精确大小：1,073,741,824 B"
    assert len([node for node in grouped_preview["content"][3]["content"]
                if node["component"] == "a"]) == 2
    assert grouped_preview["content"][4] == \
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
    assert subscribe.quality == "WEB-?DL|WEB-?RIP"
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
    success, _message, _draft = plugin.preview_candidate(choice, refresh_search=True)
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
    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert host.module._full_season_version_count(candidates) == 1
    assert host.module._full_season_version_count([
        candidates[0], {**candidates[1], "size_bytes": 2048},
    ]) == 2
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
    assert "1.00 KiB（1,024 B）" in _form_items(plugin, "replace_resource")[0]["title"]
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
    assert len(host.search_calls) == 4
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


def test_standard_quality_and_named_candidate_draft_round_trip_without_changing_custom_edits(plugin_host):
    """常见质量用内置规则，其他候选保持名称引用，自定义正则原样保留。"""
    host = plugin_host
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    candidate = host.storage["gate_state"]["entries"]["2"]["candidates"][0]
    displayed = _form_items(plugin, "candidate")[0]["rules"]
    assert displayed["quality"] == "WEB-?DL|WEB-?RIP"
    assert displayed["include"] == {"candidate": "TEAM"}
    success, _message, draft = plugin.preview_candidate(choice, json.dumps(displayed))
    assert success
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["rules"] == host.module.candidate_default_rules(candidate)
    custom = {**displayed, "include": "TEAM|GROUP", "effect": None}
    normalized = host.module._draft_rules(candidate, json.dumps(custom), "all_sites")
    assert normalized["include"] == "TEAM|GROUP"
    assert normalized["effect"] is None
    assert normalized["sites"] == []
    assert host.module.display_draft_rules(candidate, normalized)["include"] == "TEAM|GROUP"
    with pytest.raises(ValueError, match="候选规则引用已失效"):
        host.module._draft_rules(candidate, '{"quality":{"candidate":"1080p"}}', "candidate")
    assert plugin.confirm_candidate(choice, draft)[0]
    assert host.subscriptions[2].quality == "WEB-?DL|WEB-?RIP"
    assert host.subscriptions[2].include == "TEAM"


def test_preview_reuses_search_without_age_limit_but_rechecks_rules_and_can_refresh(plugin_host, monkeypatch):
    """规则变化即时重筛；缓存不按年龄失效，强制刷新才搜索，空结果也可复用。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    plugin._preview_search_cache.clear()
    another_site = copy.deepcopy(chosen)
    another_site.torrent_info.site = 7
    another_site.torrent_info.site_name = "另一个站"
    host.search_results.append(another_site)
    host.search_calls.clear()
    clock_value = [100.0]
    monkeypatch.setattr(host.module.time, "monotonic", lambda: clock_value[0])
    plugin.init_plugin({"enabled": True, "candidate": choice, "operation": "preview"})
    assert len(host.search_calls) == 2
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 1
    assert host.search_calls[0]["sites"] == [5]
    clock_value[0] += 3
    plugin.init_plugin({**host.storage["config"], "operation": "preview", "rule_mode": "all_sites"})
    assert len(host.search_calls) == 4
    assert host.search_calls[2]["sites"] is None
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["search_cached"] is False
    clock_value[0] += 3
    plugin.init_plugin({**host.storage["config"], "operation": "preview"})
    cached = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert len(host.search_calls) == 4
    assert cached["search_cached"] is True
    assert cached["search_age_seconds"] == 3
    assert cached["matched_count"] == 2
    assert cached["timings"]["search_seconds"] == 0
    host.search_results[:] = [chosen]
    assert plugin.preview_candidate(choice, rule_mode="all_sites", refresh_search=True)[0]
    assert len(host.search_calls) == 6
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 1
    clock_value[0] += 90 * 24 * 3600
    host.search_results.clear()
    assert plugin.preview_candidate(choice)[0]
    assert len(host.search_calls) == 6
    cached = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert cached["search_cached"] is True
    assert cached["search_age_seconds"] == 90 * 24 * 3600
    assert cached["matched_count"] == 1
    assert plugin.preview_candidate(choice, refresh_search=True)[0]
    assert len(host.search_calls) == 8
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 0
    assert plugin.preview_candidate(choice)[0]
    assert len(host.search_calls) == 8
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["search_cached"] is True
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["search_result_count"] == 0
    assert "cookie-secret" not in json.dumps(host.storage)
    assert "secret.example" not in json.dumps(host.storage)


def test_preview_explains_inherited_rules_and_counts_all_rejections_before_truncation(plugin_host, monkeypatch):
    """摘要统计全部排除资源，区分候选、原订阅和宿主默认条件。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    host.subscriptions[2].exclude = "blocked"
    original_params = host.module.SubscribeChain.get_params
    monkeypatch.setattr(host.module.SubscribeChain, "get_params", lambda subscribe: {
        **original_params(subscribe), "min_seeders": 5,
    })
    host.search_results.extend(_context(f"Show.S01E02.1080p.WEB-DL.HDR-GROUP.{number}", episodes=(2,))
                               for number in range(45))
    excluded = _context("Show.S01E03.1080p.WEB-DL.HDR-TEAM.blocked", episodes=(3,))
    host.search_results.append(excluded)
    assert plugin.preview_candidate(choice, refresh_search=True)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["rejected_count"] == 46
    assert len(preview["rejected"]) == 40
    assert preview["rejection_summary"] == [
        {"reason": "缺少包含标识：TEAM", "count": 45},
        {"reason": "命中排除规则：自定义正则：blocked", "count": 1},
    ]
    summary = {item["field"]: item for item in preview["effective_rules"]}
    assert summary["quality"]["source"] == "候选规格"
    assert summary["exclude"]["source"] == "原订阅"
    assert summary["min_seeders"]["source"] == "宿主默认"
    panel = plugin.get_form()[1]["_candidate_preview"]
    assert panel["rejection_summary"] == preview["rejection_summary"]
    assert panel["matched_shown"] == 1
    assert panel["rejected_shown"] == 40
    assert panel["table"]["component"] == "VTable"
    assert "secret.example" not in json.dumps(panel)


def test_preview_timings_separate_recognition_search_filtering_and_directory(plugin_host, monkeypatch):
    """用模拟耗时验证界面能定位等待阶段，不对站点进行实际请求。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    host.search_results.append(_context("Show.S01E02.1080p.WEB-DL.HDR-GROUP", episodes=(2,)))
    elapsed = [0.0]
    monkeypatch.setattr(host.module.time, "perf_counter", lambda: elapsed[0])

    def timed(function, seconds):
        def wrapped(*args, **kwargs):
            elapsed[0] += seconds
            return function(*args, **kwargs)
        return wrapped

    monkeypatch.setattr(host.module.MediaChain, "recognize_media",
                        timed(host.module.MediaChain.recognize_media, 2))
    monkeypatch.setattr(host.module.SearchChain, "process", timed(host.module.SearchChain.process, 11))
    monkeypatch.setattr(host.module.TorrentHelper, "filter_torrent_reason",
                        staticmethod(timed(host.module.TorrentHelper.filter_torrent_reason, 0.25)))
    monkeypatch.setattr(host.module, "preview_download_path", timed(host.module.preview_download_path, 3))
    assert plugin.preview_candidate(choice, refresh_search=True)[0]
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["timings"] == {
        "recognition_seconds": 2, "search_seconds": 22, "filter_seconds": 0.5,
        "directory_seconds": 3, "total_seconds": 27.5,
    }


def test_scan_cache_replaces_previous_round_while_candidates_merge_and_approval_releases_it(plugin_host):
    """历史候选可保留，预览只使用最新扫描；批准成功释放完整上下文。"""
    host = plugin_host
    plugin, old_choice, _old = _prepare_directory_confirmation(host)
    old_id = old_choice.split(":", 1)[1]
    newer = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM.New")
    later_episode = _context("Show.S01E02.1080p.WEB-DL.HDR-TEAM.New", episodes=(2,))
    host.search_results[:] = [newer, later_episode]
    plugin.refresh_candidates()
    entry = host.storage["gate_state"]["entries"]["2"]
    assert len(entry["candidates"]) == 2
    assert any(item["id"] == old_id for item in entry["candidates"])
    host.search_calls.clear()
    assert plugin.preview_candidate(old_choice)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert host.search_calls == []
    assert preview["search_cached"] is True
    assert preview["search_source"] == "候选扫描"
    assert preview["search_result_at"] == entry["last_scan"]
    assert preview["matched_count"] == 2
    assert old_id not in {item["id"] for item in preview["matched"]}
    assert plugin.confirm_candidate(old_choice)[0] is False
    assert plugin._preview_search_cache
    new_choice = next(item["value"] for item in _form_items(plugin, "candidate") if item["value"] != old_choice)
    assert plugin.preview_candidate(new_choice)[0]
    assert plugin.confirm_candidate(new_choice)[0]
    assert plugin._preview_search_cache == {}
    approved = host.storage["gate_state"]["entries"]["2"]
    assert len(approved["candidates"]) == 2
    assert approved["preview"]["matched_count"] == 2
    assert "确认时的规则预览（历史记录）" in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert next(service for service in plugin.get_service() if service["id"] == "candidate_refresh")["kwargs"] == {"hours": 4}


def test_single_site_preview_filters_shared_scan_and_only_queries_selected_site_when_refreshed(plugin_host):
    """全站扫描可为单站提供资源，单站预览不计入其他站点的排除资源。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    other_site = copy.deepcopy(chosen)
    other_site.torrent_info.site = 7
    other_site.torrent_info.site_name = "另一个站"
    host.search_results.append(other_site)
    plugin.refresh_candidates()
    host.search_calls.clear()
    assert plugin.preview_candidate(choice)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert host.search_calls == []
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 0
    assert preview["search_sites"] == [5]
    assert all(item["site_id"] == 5 for item in preview["matched"])
    assert plugin.preview_candidate(choice, refresh_search=True)[0]
    assert host.search_calls[-1]["sites"] == [5]
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 1
    assert plugin.preview_candidate(choice, rule_mode="all_sites", refresh_search=True)[0]
    assert host.search_calls[-1]["sites"] is None
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["matched_count"] == 2


def test_original_subscription_option_survives_preview_and_is_not_overwritten_by_candidate(plugin_host):
    """保留原订阅是独立选项，规范化后仍保留选择，不回填候选该项。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    original = "WEB-?DL|WEB-?RIP"
    host.subscriptions[2].quality = original
    success, _message, draft = plugin.preview_candidate(choice, '{"quality":{"subscription":true}}')
    assert success
    assert json.loads(draft)["quality"] == {"subscription": True}
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert "quality" not in preview["rules"]
    quality = next(item for item in preview["effective_rules"] if item["field"] == "quality")
    assert quality["source"] == "原订阅"
    assert quality["value"] == "WEB-DL"
    assert plugin.confirm_candidate(choice, draft)[0]
    assert host.subscriptions[2].quality == original
    assert host.subscriptions[2].resolution == r"1080[pi]?|x1080"


def test_search_cache_ignores_removed_duration_setting_and_is_bounded(plugin_host):
    """旧时长设置被移除；缓存始终复用，容量有上限，停用释放内存。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    plugin.init_plugin({"enabled": True, "search_cache_hours": 0})
    host.search_calls.clear()
    assert plugin.preview_candidate(choice)[0]
    assert plugin.preview_candidate(choice)[0]
    assert host.search_calls == []
    assert plugin._preview_search_cache
    assert "search_cache_hours" not in host.storage["config"]
    assert "search_cache_hours" not in json.dumps(plugin.get_form())
    plugin.init_plugin({"enabled": True, "search_cache_hours": 24})
    assert "search_cache_hours" not in host.storage["config"]
    for number in range(70):
        subscribe = _subscribe(number + 10)
        subscribe.media_id = str(number)
        plugin._remember_search(subscribe, [chosen], [], "候选扫描", "now")
    assert len(plugin._preview_search_cache) == 64
    plugin.init_plugin({"enabled": False, "search_cache_hours": 24})
    assert plugin._preview_search_cache == {}


def test_scan_removes_deleted_subscription_records_and_unused_search_cache(plugin_host):
    """不按时间清缓存，但会清掉订阅删除或身份变化后无对应订阅的数据。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    assert host.storage["gate_state"]["entries"]["2"]["preview"]
    assert host.storage["gate_state"]["entries"]["2"]["candidates"]
    plugin._remember_search(host.subscriptions[1], [chosen], [], "规则预览搜索", "now")
    surviving_identity = host.module._identity(host.subscriptions[1])
    del host.subscriptions[2]
    plugin.refresh_candidates()
    assert "2" not in host.storage["gate_state"]["entries"]
    assert set(plugin._preview_search_cache) == {(surviving_identity, ())}
    host.subscriptions[1].media_id = "456"
    plugin.refresh_candidates()
    assert plugin._preview_search_cache == {}


def test_preview_status_checks_the_actual_cache_without_searching_or_saving(plugin_host):
    """开始前的提示与预览使用相同缓存范围，区分空缓存、缺失及手动刷新。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    config = {"operation": "preview", "candidate": choice}
    host.search_calls.clear()
    saved = copy.deepcopy(host.storage)
    assert plugin.preview_search_status(config) == {
        "success": True, "status": "cached", "search_sites": [5], "search_result_count": 1,
    }
    assert host.search_calls == []
    assert host.storage == saved
    plugin._preview_search_cache.clear()
    assert plugin.preview_search_status(config)["status"] == "cache_miss"
    assert host.search_calls == []
    assert plugin.preview_candidate(choice)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["search_trigger"] == "cache_miss"
    assert preview["search_result_count"] == 1
    assert plugin.preview_search_status({**config, "rule_mode": "all_sites"})["status"] == "cache_miss"
    assert plugin.preview_search_status({**config, "refresh_search": True})["status"] == "manual"
    # 有全站缓存，但其中没有所选站点的资源，也属于本次站点范围的空缓存。
    other_site = copy.deepcopy(chosen)
    other_site.torrent_info.site = 7
    plugin._remember_search(host.subscriptions[2], [other_site], [], "候选扫描", "now")
    assert plugin.preview_search_status(config)["status"] == "cached_empty"
    assert plugin.preview_search_status(config)["search_result_count"] == 0
    assert plugin.preview_search_status({**config, "rule_mode": "all_sites"})["status"] == "cached"
    host.search_calls.clear()
    assert plugin.preview_candidate(choice)[0]
    assert host.search_calls == []
    panel = plugin.get_form()[1]["_candidate_preview"]
    assert panel["search_cached"] is True
    assert panel["search_result_count"] == 0
    assert panel["search_trigger"] == "cache"
    assert plugin.preview_search_status({**config, "rule_json": "{broken"})["success"] is False


def test_scan_expires_confirmed_candidates_and_previews_after_24_hours_without_revoking(plugin_host, monkeypatch):
    """确认后的大记录满 24 小时由扫描清理，批准、规则与新的回刷预览仍可用。"""
    from datetime import datetime as RealDatetime, timedelta, timezone as utc_timezone

    host = plugin_host
    base = RealDatetime(2026, 11, 7, 12, tzinfo=utc_timezone.utc)
    clock_value = [base]

    class FakeDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return clock_value[0].astimezone(tz) if tz else clock_value[0].replace(tzinfo=None)

    monkeypatch.setattr(host.module, "datetime", FakeDatetime)
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    assert plugin.confirm_candidate(choice)[0]
    approved_rules = {name: copy.deepcopy(getattr(host.subscriptions[2], name))
                      for name in ("quality", "resolution", "effect", "include", "sites")}
    approved_at = host.storage["gate_state"]["entries"]["2"]["approved_at"]
    host.subscriptions[3] = _subscribe(3)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 3}))
    plugin.refresh_candidates()
    pending_choice = next(item["value"] for item in _form_items(plugin, "candidate")
                          if item["value"].startswith("3:"))
    assert plugin.preview_candidate(pending_choice)[0]
    clock_value[0] = base + timedelta(hours=23)
    assert plugin.preview_replacements("2")[0]
    assert plugin.preview_search_status({"operation": "preview_replace", "replace_subscription": "2"})["status"] == "cached"
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    clock_value[0] = base + timedelta(hours=24, seconds=-1)
    plugin.refresh_candidates()
    approved = host.storage["gate_state"]["entries"]["2"]
    assert approved["candidates"] and approved["preview"] and approved["replacement_preview"]
    clock_value[0] = base + timedelta(hours=24)
    plugin.refresh_candidates()
    approved = host.storage["gate_state"]["entries"]["2"]
    assert "candidates" not in approved
    assert "preview" not in approved
    assert approved["replacement_preview"]
    assert approved["status"] == "approved"
    assert approved["approved_at"] == approved_at
    assert plugin._is_approved(host.subscriptions[2]) is True
    assert {name: getattr(host.subscriptions[2], name) for name in approved_rules} == approved_rules
    assert "确认时的规则预览（历史记录）" not in json.dumps(plugin.get_page(), ensure_ascii=False)
    clock_value[0] = base + timedelta(hours=47)
    plugin.refresh_candidates()
    assert "replacement_preview" not in host.storage["gate_state"]["entries"]["2"]
    pending = host.storage["gate_state"]["entries"]["3"]
    assert pending["status"] == "pending"
    assert pending["candidates"] and pending["preview"]


@pytest.mark.parametrize("quality,title,expected", [
    ("WEB-DL", "Show.S01E01.1080p.WEB-DL.HDR-TEAM", "WEB-?DL|WEB-?RIP"),
    ("WEB-DL", "Show.S01E01.1080p.WEB.DL.HDR-TEAM", r"WEB[- .]?(?:DL|RIP)"),
    ("BluRay", "Show.S01E01.1080p.BluRay.HDR-TEAM", "Blu-?Ray"),
    ("Remux", "Show.S01E01.1080p.Remux.HDR-TEAM", "Remux"),
    ("H265", "Show.S01E01.1080p.H.265.HDR-TEAM", "[Hx].?265|HEVC"),
])
def test_candidate_quality_defaults_to_builtin_only_when_it_matches_selected_title(plugin_host, quality, title, expected):
    """内置质量仍须匹配所选标题，带特殊分隔符的标题继续保留兼容候选匹配。"""
    host = plugin_host
    context = _context(title)
    context.meta_info.resource_type = quality
    rules_module = importlib.import_module(f"{host.module.__name__}.rules")
    candidate = rules_module.resource_candidate(context, 1, "now")
    original = copy.deepcopy(candidate)
    default_rules = host.module._draft_rules(candidate, "", "candidate")
    assert default_rules["quality"] == expected
    assert re.search(default_rules["quality"], title, re.I)
    assert candidate == original
    raw = json.dumps({"quality": candidate["rules"]["quality"]})
    explicit = host.module._draft_rules(candidate, raw, "candidate")
    assert explicit["quality"] == candidate["rules"]["quality"]
    redisplayed = host.module.editor_draft_rules(candidate, explicit, raw)
    assert host.module._draft_rules(candidate, json.dumps(redisplayed), "candidate")["quality"] == explicit["quality"]
    summary = host.module.rule_summary(explicit, [5], candidate, working=SimpleNamespace(**explicit), rules=explicit)
    assert not next(item["value"] for item in summary if item["field"] == "quality").startswith("自定义正则")


def test_team_preset_is_literal_and_independent_of_combined_include_rule(plugin_host):
    host = plugin_host
    context = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM.A.CUSTOM")
    context.meta_info.resource_team = "TEAM.A"
    context.meta_info.customization = "CUSTOM"
    rules_module = importlib.import_module(f"{host.module.__name__}.rules")
    candidate = rules_module.resource_candidate(context, 1, "now")
    team_pattern = rules_module.candidate_team_pattern(candidate)
    assert team_pattern == r"TEAM\.A"
    assert re.search(team_pattern, context.torrent_info.title.replace("CUSTOM", "OTHER"))
    assert not re.search(candidate["rules"]["include"], context.torrent_info.title.replace("CUSTOM", "OTHER"))
    custom = host.module._draft_rules(candidate, json.dumps({"include": team_pattern}), "candidate")
    assert custom["include"] == team_pattern
    assert rules_module.candidate_team_pattern({**candidate, "release_team": "MISSING"}) is None
    context.meta_info.resource_team = ""
    custom_only = rules_module.resource_candidate(context, 1, "now")
    assert custom_only["team"] == "CUSTOM"
    assert rules_module.candidate_team_pattern(custom_only) is None


@pytest.fixture
def rule_group_settings(plugin_host, monkeypatch):
    """模拟宿主的规则组注册表和独立的普通/洗版默认选择。"""
    values = {
        "UserFilterRuleGroups": [{"name": "严格规则组", "rule_string": "WEBDL & 4K"}],
        "SubscribeFilterRuleGroups": ["严格规则组"], "BestVersionFilterRuleGroups": ["洗版规则组"],
    }
    writes = []

    class SystemConfigOper:
        def get(self, key):
            return copy.deepcopy(values.get(key))

        def set(self, key, value):
            writes.append(key)
            values[key] = copy.deepcopy(value)
            return True

        def update_atomically(self, key, mutation):
            result, updated = mutation(None, copy.deepcopy(values.get(key)))
            self.set(key, updated)
            return result

    module = ModuleType("app.db.oper.systemconfig")
    module.SystemConfigOper = SystemConfigOper
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys.modules["app.schemas.types"], "SystemConfigKey", SimpleNamespace(
        UserFilterRuleGroups="UserFilterRuleGroups",
        CustomFilterRules="CustomFilterRules", SubscribeFilterRuleGroups="SubscribeFilterRuleGroups",
        BestVersionFilterRuleGroups="BestVersionFilterRuleGroups",
    ), raising=False)
    return SimpleNamespace(values=values, writes=writes, oper=SystemConfigOper)


@pytest.mark.parametrize("atomic", [True, False])
def test_disable_rule_groups_only_applies_after_confirmation_and_prevents_default_fallback(
    plugin_host, rule_group_settings, monkeypatch, atomic,
):
    """预览只记录选择；确认显式使用合法全放行组，不修改其他组或默认选择。"""
    host, settings = plugin_host, rule_group_settings
    if not atomic:
        monkeypatch.setattr(settings.oper, "update_atomically", None)
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    host.subscriptions[2].filter_groups = ["严格规则组"]
    original = copy.deepcopy(settings.values)
    success, _message, draft = plugin.preview_candidate(choice, '{"filter_groups":[]}')
    assert success
    assert settings.writes == []
    assert host.subscriptions[2].filter_groups == ["严格规则组"]
    preview = plugin.get_form()[1]["_candidate_preview"]
    assert preview["rule_groups_disabled"] is True
    groups = next(item for item in preview["effective_rules"] if item["field"] == "filter_groups")
    assert groups["value"] == "不使用规则组（确认后生效）"
    assert plugin.confirm_candidate(choice, '{"filter_groups":{"subscription":true}}')[0] is False
    assert settings.writes == []
    assert plugin.confirm_candidate(choice, draft)[0]
    name = host.module.UNFILTERED_GROUP_NAME
    assert host.subscriptions[2].filter_groups == [name]
    # 宿主的“订阅组 or 默认组”选择现在得到显式组，不能再落回严格默认组。
    assert (host.subscriptions[2].filter_groups or settings.values["SubscribeFilterRuleGroups"]) == [name]
    assert settings.values["UserFilterRuleGroups"][:-1] == original["UserFilterRuleGroups"]
    assert settings.values["SubscribeFilterRuleGroups"] == original["SubscribeFilterRuleGroups"]
    assert settings.values["BestVersionFilterRuleGroups"] == original["BestVersionFilterRuleGroups"]
    assert plugin.preview_replacements("2")[0]
    replacement = plugin.get_form()[1]["_replacement_preview"]
    assert replacement["rule_groups_disabled"] is True
    assert next(item for item in replacement["effective_rules"] if item["field"] == "filter_groups")["value"] == "不使用规则组"
    # 下一条订阅选择相同策略时复用该组，不累积注册表记录。
    assert host.module.ensure_unfiltered_group() == name
    assert settings.writes == ["UserFilterRuleGroups"]


def test_keep_rule_groups_preserves_subscription_groups_and_editor_choice(plugin_host, rule_group_settings):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    host.subscriptions[2].filter_groups = ["严格规则组"]
    success, _message, draft = plugin.preview_candidate(choice, '{"filter_groups":{"subscription":true}}')
    assert success
    assert json.loads(draft)["filter_groups"] == {"subscription": True}
    assert plugin.confirm_candidate(choice, draft)[0]
    assert host.subscriptions[2].filter_groups == ["严格规则组"]
    assert rule_group_settings.writes == []
    with pytest.raises(ValueError, match="规则组只能选择"):
        host.module._draft_rules({}, '{"filter_groups":["随意组名"]}', "candidate")


@pytest.mark.parametrize("failure", ["name_collision", "save_failed"])
def test_failed_rule_group_configuration_keeps_subscription_pending_and_cache(
    plugin_host, rule_group_settings, monkeypatch, failure,
):
    host, settings = plugin_host, rule_group_settings
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    if failure == "name_collision":
        settings.values["UserFilterRuleGroups"].append({
            "name": host.module.UNFILTERED_GROUP_NAME, "rule_string": "WEBDL & 4K",
        })
    else:
        monkeypatch.setattr(settings.oper, "update_atomically", None)
        monkeypatch.setattr(settings.oper, "set", lambda *_args: False)
    original = copy.deepcopy(settings.values)
    success, _message, draft = plugin.preview_candidate(choice, '{"filter_groups":[]}')
    assert success
    success, message = plugin.confirm_candidate(choice, draft)
    assert success is False
    assert "不使用规则组设置失败" in message
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].quality is None
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert plugin._preview_search_cache
    assert settings.values == original


@pytest.mark.parametrize("explicit", [False, True])
def test_preview_compares_groups_and_only_updates_draft_when_groups_remove_every_match(
    plugin_host, rule_group_settings, monkeypatch, explicit,
):
    """同一缓存比较两种策略，自动回填草稿但确认前不修改订阅或共享资源。"""
    host, settings = plugin_host, rule_group_settings
    plugin, choice, context = _prepare_directory_confirmation(host)
    if explicit:
        host.subscriptions[2].filter_groups = ["自选规则组"]
    expected_groups = ["自选规则组"] if explicit else ["严格规则组"]
    origin = "原订阅规则组" if explicit else "系统默认规则组"
    calls = []

    def reject_all(_self, **kwargs):
        calls.append(kwargs)
        for torrent in kwargs["torrent_list"]:
            torrent.pri_order = 999
            torrent.labels.append("仅在副本中出现")
        return []

    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", reject_all)
    round_count = len(host.search_calls)
    plugin.init_plugin({
        "enabled": True, "operation": "preview", "candidate": choice, "rule_mode": "all_sites",
        "rule_json": '{"filter_groups":{"subscription":true}}',
    })
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    draft = host.storage["config"]["rule_json"]
    assert json.loads(draft)["filter_groups"] == []
    assert preview["rules"]["filter_groups"] == []
    assert preview["matched_count"] == 1
    assert preview["rule_groups_checked"] is True
    assert preview["rule_groups_auto_disabled"] is True
    assert preview["rule_groups_disabled"] is True
    assert preview["rule_group_comparison"] == {
        "names": expected_groups, "source": "原订阅" if explicit else "宿主默认",
        "with_groups_count": 0, "without_groups_count": 1,
    }
    assert origin in preview["rule_group_notice"]
    assert "已自动选择" in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert len(host.search_calls) == round_count
    assert len(calls) == 1
    assert calls[0]["rule_groups"] == expected_groups
    assert calls[0]["torrent_list"][0] is not context.torrent_info
    assert "仅在副本中出现" not in context.torrent_info.labels
    assert getattr(context.torrent_info, "pri_order", None) != 999
    assert host.subscriptions[2].filter_groups == (expected_groups if explicit else [])
    assert host.subscriptions[2].state == "S"
    assert settings.writes == []
    assert plugin.confirm_candidate(choice, draft, "all_sites")[0]
    assert host.subscriptions[2].filter_groups == [host.module.UNFILTERED_GROUP_NAME]


def test_preview_keeps_groups_when_some_resources_pass_and_can_explicitly_skip_them(
    plugin_host, rule_group_settings, monkeypatch,
):
    host = plugin_host
    plugin, choice, _context_chosen = _prepare_directory_confirmation(host)
    host.search_results.append(_context("Show.S01E02.1080p.WEB-DL.HDR-TEAM.blocked", episodes=(2,)))
    calls = []

    def filter_groups(_self, **kwargs):
        calls.append(kwargs)
        return [torrent for torrent in kwargs["torrent_list"] if "blocked" not in torrent.title]

    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", filter_groups)
    success, _message, draft = plugin.preview_candidate(
        choice, '{"filter_groups":{"subscription":true}}', "all_sites", refresh_search=True,
    )
    assert success
    preview = plugin.get_form()[1]["_candidate_preview"]
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 1
    assert preview["rule_group_comparison"]["with_groups_count"] == 1
    assert preview["rule_group_comparison"]["without_groups_count"] == 2
    assert preview["rule_groups_disabled"] is False
    assert preview["rule_groups_auto_disabled"] is False
    assert preview["rule_group_notice"] == ""
    assert json.loads(draft)["filter_groups"] == {"subscription": True}
    assert preview["rejection_summary"] == [{"reason": "不符合系统默认规则组：严格规则组", "count": 1}]
    assert next(item for item in preview["effective_rules"] if item["field"] == "filter_groups") == {
        "field": "filter_groups", "label": "规则组", "value": "严格规则组", "source": "宿主默认",
    }
    calls.clear()
    assert plugin.preview_candidate(choice, '{"filter_groups":[]}', "all_sites")[0]
    preview = plugin.get_form()[1]["_candidate_preview"]
    assert preview["matched_count"] == 2
    assert preview["rule_groups_disabled"] is True
    assert preview["rule_groups_auto_disabled"] is False
    assert preview["rule_group_comparison"] is None
    assert calls == []


def test_no_base_matches_does_not_blame_or_disable_rule_groups(plugin_host, rule_group_settings, monkeypatch):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)

    def should_not_filter(*_args, **_kwargs):
        pytest.fail("没有基础条件通过的资源时不应调用规则组筛选")

    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", should_not_filter)
    success, _message, draft = plugin.preview_candidate(choice, '{"include":"MISSING"}', "all_sites")
    assert success
    preview = plugin.get_form()[1]["_candidate_preview"]
    assert preview["matched_count"] == 0
    assert preview["rule_groups_auto_disabled"] is False
    assert preview["rule_groups_disabled"] is False
    assert preview["rule_group_comparison"]["without_groups_count"] == 0
    assert "filter_groups" not in json.loads(draft)
    assert "缺少包含标识" in preview["rejection_summary"][0]["reason"]


def test_rule_group_failure_is_reported_without_automatically_changing_the_policy(
    plugin_host, rule_group_settings, monkeypatch,
):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)

    def broken_groups(*_args, **_kwargs):
        raise RuntimeError("无效的规则表达式")

    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", broken_groups)
    raw = '{"filter_groups":{"subscription":true}}'
    success, message, draft = plugin.preview_candidate(choice, raw, "all_sites")
    assert success is False
    assert "规则组筛选失败" in message
    assert draft == raw
    assert "preview" not in host.storage["gate_state"]["entries"]["2"]
    assert host.subscriptions[2].filter_groups == []
    assert rule_group_settings.writes == []
    assert plugin.preview_candidate(choice, '{"filter_groups":[]}', "all_sites")[0]


@pytest.mark.parametrize("changed", ["selection", "definition", "custom_rule"])
def test_rule_group_changes_invalidate_confirmation(plugin_host, rule_group_settings, changed):
    host, settings = plugin_host, rule_group_settings
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    success, _message, draft = plugin.preview_candidate(choice)
    assert success
    if changed == "selection":
        settings.values["SubscribeFilterRuleGroups"] = ["其他规则组"]
    elif changed == "definition":
        settings.values["UserFilterRuleGroups"][0]["rule_string"] = "WEBDL & HDR"
    else:
        settings.values["CustomFilterRules"] = [{"id": "TEAM", "include": "CHANGED"}]
    assert plugin.confirm_candidate(choice, draft)[0] is False
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].quality is None


def test_rule_group_changes_during_filtering_require_a_new_preview(plugin_host, rule_group_settings, monkeypatch):
    host, settings = plugin_host, rule_group_settings
    plugin, choice, _chosen = _prepare_directory_confirmation(host)

    def change_configuration(_self, **kwargs):
        settings.values["UserFilterRuleGroups"][0]["rule_string"] = "WEBDL & HDR"
        return kwargs["torrent_list"]

    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", change_configuration)
    success, message, _draft = plugin.preview_candidate(choice)
    assert success is False
    assert "规则组配置已变化" in message
    assert "preview" not in host.storage["gate_state"]["entries"]["2"]


def test_replacement_preview_applies_groups_without_changing_an_approved_subscription(
    plugin_host, rule_group_settings, monkeypatch,
):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    success, _message, draft = plugin.preview_candidate(choice)
    assert success and plugin.confirm_candidate(choice, draft)[0]
    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", lambda *_args, **_kwargs: [])
    assert plugin.preview_replacements("2")[0]
    preview = plugin.get_form()[1]["_replacement_preview"]
    assert preview["matched_count"] == 0
    assert preview["rule_group_comparison"]["without_groups_count"] == 1
    assert preview["rule_groups_auto_disabled"] is False
    assert host.subscriptions[2].filter_groups == []


def test_preview_uses_subscription_groups_before_normal_or_best_version_defaults(plugin_host, rule_group_settings):
    host = plugin_host
    subscribe = _subscribe(2)
    assert host.module.preview_group_policy(subscribe)["names"] == ["严格规则组"]
    subscribe.best_version = 1
    assert host.module.preview_group_policy(subscribe)["names"] == ["洗版规则组"]
    subscribe.filter_groups = ["自选规则组"]
    assert host.module.preview_group_policy(subscribe)["names"] == ["自选规则组"]


def test_replacement_rechecks_rule_groups_against_the_fresh_resource_before_downloading(
    plugin_host, rule_group_settings, monkeypatch,
):
    host = plugin_host
    plugin, choice, context = _prepare_directory_confirmation(host)
    context.torrent_info.labels = ["allowed"]
    monkeypatch.setattr(host.module.SearchChain, "filter_torrents", lambda _self, **kwargs: [
        torrent for torrent in kwargs["torrent_list"] if "allowed" in torrent.labels
    ])
    success, _message, draft = plugin.preview_candidate(choice)
    assert success and plugin.confirm_candidate(choice, draft)[0]
    assert plugin.preview_replacements("2")[0]
    resource = plugin._state["entries"]["2"]["replacement_preview"]["matched"][0]
    context.torrent_info.labels = ["blocked"]
    success, message = plugin.submit_replacement(f"2:{resource['id']}", "1")
    assert success is False
    assert "系统默认规则组" in message
    assert host.downloads == []


def test_saving_and_cached_preview_do_not_schedule_background_searches(plugin_host):
    host = plugin_host
    plugin, choice, _context_value = _prepare_directory_confirmation(host)
    host.jobs.clear()
    host.search_calls.clear()
    plugin.init_plugin({"enabled": True, "operation": "save"})
    plugin.init_plugin({"enabled": True, "operation": "preview", "candidate": choice})
    assert host.search_calls == []
    assert "collect" not in host.jobs
    assert plugin._queued_scan_ids == set()
    assert plugin.get_service()[0]["kwargs"] == {"hours": 4}


def test_added_subscription_only_scans_itself_and_busy_scan_preserves_queue(plugin_host, monkeypatch):
    host = plugin_host
    plugin, _choice, _context_value = _prepare_directory_confirmation(host)
    calls = []
    monkeypatch.setattr(plugin, "_scan_one", calls.append)
    host.subscriptions[3] = _subscribe(3)
    event = SimpleNamespace(event_data={"subscribe_id": 3})
    plugin.on_subscribe_added(event)
    plugin.on_subscribe_added(event)
    plugin._scan_lock.acquire()
    try:
        plugin._run_scheduled_scan()
        assert plugin._queued_scan_ids == {3}
        assert calls == []
    finally:
        plugin._scan_lock.release()
    plugin._run_scheduled_scan()
    assert calls == [3]
    assert plugin._queued_scan_ids == set()


def test_restart_does_not_repeat_a_recent_scan_but_collects_due_subscriptions(plugin_host):
    host = plugin_host
    _plugin, _choice, _context_value = _prepare_directory_confirmation(host)
    host.jobs.clear()
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert "collect" not in host.jobs
    assert not restarted._queued_scan_ids
    host.storage["gate_state"]["entries"]["2"]["last_scan"] = "2020-01-01T00:00:00+00:00"
    due = host.module.SubscribeOverwatcher()
    due.init_plugin({"enabled": True})
    assert due._queued_scan_ids == {2}
    assert host.jobs["collect"][1] == 15


@pytest.mark.parametrize("season", [2, 7])
def test_search_uses_season_keyword_and_original_search_without_losing_middle_season(plugin_host, monkeypatch, season):
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    subscribe = _subscribe(7)
    subscribe.season = season
    subscribe.name = "万物生灵"
    media = SimpleNamespace(media_source="themoviedb", media_id="123", season=None,
                            en_title="All Creatures Great & Small", title="万物生灵")
    monkeypatch.setattr(host.module.MediaChain, "recognize_media", lambda _self, **kwargs: media)
    first = _context(f"All Creatures Great and Small 2020 S{season:02d}E01 1080p")
    first.meta_info.begin_season = season
    first.meta_info.season_list = [season]
    pack = _context("All Creatures Great and Small S01-S10 Complete")
    pack.meta_info.end_season = 10
    pack.meta_info.season_list = list(range(1, 11))
    wrong_season = _context("All Creatures Great and Small S01E01")

    def process(_self, **kwargs):
        host.search_calls.append(kwargs)
        results = [first] if (kwargs["keyword"] or "").endswith(f" S{season:02d}") else [first, pack, wrong_season]
        return kwargs["candidate_filter"](results)

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    diagnostics = {}
    contexts = plugin._search_unfiltered(subscribe, sites=[5], diagnostics=diagnostics)
    assert contexts == [first, pack]
    assert [call["keyword"] for call in host.search_calls] == [
        f"All Creatures Great and Small S{season:02d}", None,
    ]
    assert all(call["rule_groups"] == [] and call["sites"] == [5]
               and call["mediainfo"].season is None and "no_exists" not in call for call in host.search_calls)
    assert diagnostics["rounds"][1]["identified_count"] == 3
    assert diagnostics["rounds"][1]["result_count"] == 2
    assert diagnostics["original_search"]["checked"] is True
    assert media.season is None and media.en_title == "All Creatures Great & Small"


@pytest.mark.parametrize("season", [1, 7])
@pytest.mark.parametrize("method", ["search_site_torrents", "_search_site_torrents_with_budget"])
def test_translated_site_keyword_retains_subscription_season_and_pack_query_stays_broad(
    plugin_host, monkeypatch, season, method,
):
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    calls = []

    def site_search(_self, **kwargs):
        calls.append(kwargs["keyword"])
        return []

    monkeypatch.setattr(host.module.SearchChain, method, site_search, raising=False)
    site = {"id": 5, "name": "英文站点", "language": "en"}
    chain = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site, focused_season=season)
    search = getattr(chain, method)
    # 模拟宿主把带季中文查询词替换为不带季的英文名。
    search(site=site, keyword="All Creatures Great and Small")
    search(site=site, keyword=f"All Creatures Great and Small S{season:02d}")
    pack = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site)
    getattr(pack, method)(site=site, keyword="All Creatures Great and Small")
    assert calls == [f"All Creatures Great and Small S{season:02d}",
                     f"All Creatures Great and Small S{season:02d}", "All Creatures Great and Small"]


def test_search_name_fallback_is_bounded_and_original_search_error_keeps_target_results(plugin_host, monkeypatch):
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    subscribe = _subscribe(2)
    media = SimpleNamespace(media_source="themoviedb", media_id="123", title="样例剧", en_title="Show")
    monkeypatch.setattr(host.module.MediaChain, "recognize_media", lambda _self, **kwargs: media)
    first = _context("Show.S01E01")
    calls = []

    def process(_self, **kwargs):
        calls.append(kwargs["keyword"])
        if kwargs["keyword"] == "Show S01":
            return []
        if kwargs["keyword"] == "样例剧 S01":
            return kwargs["candidate_filter"]([first])
        raise RuntimeError("补搜失败")

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    diagnostics = {}
    assert plugin._search_unfiltered(subscribe, diagnostics=diagnostics) == [first]
    assert calls == ["Show S01", "样例剧 S01", None]
    assert diagnostics["supplemental_failed"] is True
    assert diagnostics["original_search"]["checked"] is False


@pytest.mark.parametrize("case,change_keyword", [
    ("both", False), ("original_only", False), ("season_only", True), ("other_original_resource", True),
])
def test_confirmation_keeps_original_search_unless_only_season_search_recalls_selected_version(
    plugin_host, monkeypatch, case, change_keyword,
):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    subscribe = host.subscriptions[2]
    subscribe.keyword = "Custom Original Search"
    subscribe.search_imdbid = True
    media = SimpleNamespace(media_source="themoviedb", media_id="123", title="样例剧", en_title="Show", imdb_id="tt123")
    monkeypatch.setattr(host.module.MediaChain, "recognize_media", lambda _self, **_kwargs: media)
    another = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM.Other")

    def process(_self, **kwargs):
        if kwargs["keyword"] == "Custom Original Search":
            assert kwargs["area"] == "imdbid"
            contexts = [chosen] if case in {"both", "original_only"} else (
                [another] if case == "other_original_resource" else [])
        else:
            contexts = [] if case == "original_only" else [chosen]
        return kwargs["candidate_filter"](contexts)

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    plugin.refresh_candidates()
    assert plugin.preview_candidate(choice)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["search_keyword_changed"] is change_keyword
    assert preview["search_keyword_verified"] is True
    assert preview["search_keyword"] == ("Show S01" if change_keyword else "Custom Original Search")
    assert subscribe.keyword == "Custom Original Search" and subscribe.search_imdbid is True
    assert preview["matched_count"] == 1
    assert "resource_keywords" not in json.dumps(host.storage)
    assert "resource_ids" not in json.dumps(host.storage)
    if case == "other_original_resource":
        assert preview["rejected"][0]["reason"] == "未由确认后使用的搜索词召回"
    submitted_search = []
    monkeypatch.setattr(host.module.SubscribeChain, "search", lambda _self, **kwargs: submitted_search.append(
        (host.subscriptions[kwargs["sid"]].keyword, host.subscriptions[kwargs["sid"]].search_imdbid)))
    assert plugin.confirm_candidate(choice)[0]
    plugin._search_approved(2)
    assert submitted_search == [("Show S01" if change_keyword else "Custom Original Search", not change_keyword)]


@pytest.mark.parametrize("reason", ["timeout", "budget_wait", "missing_observation", "changed_original_keyword"])
def test_failed_or_unverified_original_search_does_not_replace_subscription_keyword(plugin_host, reason):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    diagnostics = next(iter(plugin._preview_search_cache.values()))["diagnostics"]
    if reason in {"timeout", "budget_wait"}:
        diagnostics["original_search"].update(resource_ids=[], site_failures=[{
            "site_id": 5, "reason": "站点请求超时" if reason == "timeout" else "站点搜索间隔尚未结束，本轮暂缓",
        }])
    elif reason == "missing_observation":
        diagnostics.pop("original_search")
    else:
        diagnostics["original_search"]["keyword"] = "Previous Custom Keyword"
    assert plugin.preview_candidate(choice)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["search_keyword_changed"] is False
    assert preview["search_keyword_verified"] is False
    assert "不能判断必须加季号" in preview["search_keyword_notice"]
    assert plugin.confirm_candidate(choice)[0]
    assert getattr(host.subscriptions[2], "keyword", None) is None


@pytest.mark.parametrize("field,value", [("keyword", "Changed Search"), ("search_imdbid", True)])
def test_native_search_setting_changed_after_preview_requires_another_preview(plugin_host, field, value):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    setattr(host.subscriptions[2], field, value)
    success, message = plugin.confirm_candidate(choice)
    assert success is False
    assert "预览" in message
    assert host.subscriptions[2].state == "S"


@pytest.mark.parametrize("outcome,seconds", [("timeout", 900), ("rate_limited", 14400)])
def test_site_failure_stops_later_queries_and_other_subscriptions_until_cooldown(
    plugin_host, monkeypatch, outcome, seconds,
):
    from contextlib import contextmanager
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    now = [1000.0]
    monkeypatch.setattr(host.module.time, "time", lambda: now[0])
    observations = []
    calls = []

    @contextmanager
    def capture():
        observation = SimpleNamespace(outcome="skipped", error=None)
        observations.append(observation)
        yield observation

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def site_search(_self, **kwargs):
        calls.append(kwargs)
        observations[-1].outcome = outcome
        observations[-1].error = "HTTP 429 https://secret.example/?cookie=cookie-secret" if outcome == "rate_limited" else "超时"
        return []

    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents", site_search, raising=False)
    site = {"id": 5, "name": "天空"}
    chain = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site)
    assert chain.search_site_torrents(site=site, keyword="Show S07") == []
    assert chain.search_site_torrents(site=site, keyword="Show") == []
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    another = host.module.CandidateSearchChain(restarted._site_hold, restarted._hold_site)
    assert another.search_site_torrents(site=site, keyword="Other Show") == []
    assert len(calls) == 1
    assert chain.site_reports[-1]["status"] == "paused"
    assert host.storage["gate_state"]["site_holds"]["5"]["until"] == 1000 + seconds
    assert "cookie-secret" not in json.dumps(host.storage)
    assert "secret.example" not in json.dumps(host.storage)
    now[0] += seconds + 1
    another.search_site_torrents(site=site, keyword="Show S07")
    assert len(calls) == 2


def test_explicit_site_restriction_deadline_is_respected(plugin_host):
    from datetime import datetime
    now = datetime.now().timestamp()
    expected = int(now + 3600)
    deadline = datetime.fromtimestamp(expected).strftime("%Y-%m-%d %H:%M:%S")
    until, reason = plugin_host.module.failure_hold(
        "error", f"系统侦测到您的账户存在异常的浏览行为，限制该功能访问至：{deadline}。", now,
    )
    assert until == expected
    assert reason == "站点限制访问或登录失效"


def test_host_deferral_stops_later_queries_before_waiting_for_site_budget(plugin_host, monkeypatch):
    from contextlib import contextmanager
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    observations = []
    budget_calls = []

    @contextmanager
    def capture():
        observation = SimpleNamespace(outcome="skipped", error=None)
        observations.append(observation)
        yield observation

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def budget_search(_self, **kwargs):
        budget_calls.append(kwargs)
        observations[-1].outcome = "deferred"
        observations[-1].error = "站点已达到请求额度"
        return []

    monkeypatch.setattr(host.module.SearchChain, "_search_site_torrents_with_budget", budget_search, raising=False)
    site = {"id": 5, "name": "天空"}
    chain = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site)
    assert chain._search_site_torrents_with_budget(site=site, keyword="Show S07") == []
    assert chain._search_site_torrents_with_budget(site=site, keyword="Show") == []
    assert len(budget_calls) == 1
    assert plugin._site_hold(site) is not None
    assert chain.site_reports[-1]["status"] == "paused"


def test_candidate_observation_preserves_host_failure_and_pagination_facts(plugin_host, monkeypatch):
    from contextlib import contextmanager
    from contextvars import ContextVar
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    current = ContextVar("test_site_observation", default=None)

    @contextmanager
    def capture():
        observation = SimpleNamespace(attempted=False, outcome="skipped", error=None, raw_count=None, has_more=None)
        token = current.set(observation)
        try:
            yield observation
        finally:
            current.reset(token)

    def report_outcome(**kwargs):
        observation = current.get()
        if observation:
            observation.__dict__.update(kwargs)

    def report_page(**kwargs):
        report_outcome(**kwargs)

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    module.report_site_search_outcome = report_outcome
    module.report_site_search_page = report_page
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def site_search(_self, **_kwargs):
        report_outcome(attempted=True, outcome="timeout", error="请求超时")
        report_page(raw_count=7, has_more=False)
        return []

    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents", site_search, raising=False)
    chain = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site)
    with capture() as outer:
        assert chain.search_site_torrents(site={"id": 5, "name": "天空"}, keyword="Show S07") == []
    assert outer.attempted is True
    assert outer.outcome == "timeout" and outer.error == "请求超时"
    assert outer.raw_count == 7 and outer.has_more is False


def test_manual_scan_selects_empty_pending_show_and_only_refreshes_that_show(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    previous = copy.deepcopy(plugin._state["entries"]["2"])
    host.subscriptions[3] = _subscribe(3)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 3}))
    assert plugin._state["entries"]["3"].get("candidates") == []
    form, _model = plugin.get_form()
    choices = []

    def collect(nodes):
        for node in nodes:
            if node.get("props", {}).get("model") == "scan_subscription":
                choices.extend(node["props"]["items"])
            collect(node.get("content") or [])

    collect(form)
    assert "3" in {item["value"] for item in choices}
    host.search_calls.clear()
    success, message = plugin.scan_subscription("3")
    assert success and "本轮发现候选" in message
    assert plugin._state["entries"]["3"]["candidates"]
    assert plugin._state["entries"]["2"] == previous
    assert plugin._queued_scan_ids == set()
    assert host.search_calls and all("mediainfo" in call for call in host.search_calls)
    assert host.downloads == []
    search_count = len(host.search_calls)
    plugin._run_scheduled_scan()
    assert plugin._state["entries"]["2"] == previous
    assert len(host.search_calls) == search_count


def test_manual_scan_refreshes_cache_and_invalidates_old_preview_without_downloading(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    host.search_results[:] = [_context("New TEAM S01E01")]
    plugin._schedule_scan([2])
    plugin.init_plugin({"enabled": True, "operation": "scan", "scan_subscription": "2",
                        "operation_request_id": "manual-scan"})
    result = plugin._state["last_confirmation"]
    assert result["success"] and result["request_id"] == "manual-scan"
    assert "preview" not in plugin._state["entries"]["2"]
    assert plugin._queued_scan_ids == set()
    assert host.downloads == []
    assert len(plugin._state["entries"]["2"]["candidates"]) == 2
    cached = next(iter(plugin._preview_search_cache.values()))
    assert cached["contexts"] == host.search_results and chosen not in cached["contexts"]
    host.search_calls.clear()
    assert plugin.preview_candidate(choice)[0]
    assert host.search_calls == []


def test_manual_scan_busy_failure_and_invalid_identity_do_not_report_success(plugin_host, monkeypatch):
    host = plugin_host
    plugin, _choice, _chosen = _prepare_directory_confirmation(host)
    plugin._scan_lock.acquire()
    try:
        assert plugin.scan_subscription("2") == (False, "已有候选扫描正在进行，请稍后重试")
    finally:
        plugin._scan_lock.release()

    def fail(*_args, **_kwargs):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(plugin, "_search_unfiltered", fail)
    assert plugin.scan_subscription("2")[0] is False
    assert not plugin._scan_lock.locked()
    assert "搜索失败" in plugin._state["entries"]["2"]["last_error"]
    host.subscriptions[2].activity_key = "reused-id"
    assert plugin.scan_subscription("2")[0] is False
    assert plugin.scan_subscription(True)[0] is False
    assert plugin.scan_subscription("not-an-id")[0] is False
    assert host.downloads == []


def test_manual_empty_scan_suggests_native_search_and_prunes_old_confirmed_records(plugin_host):
    host = plugin_host
    plugin, _choice, _chosen = _prepare_directory_confirmation(host)
    host.search_results.clear()
    plugin._state["entries"]["old"] = {
        "status": "approved", "approved_at": "2020-01-01T00:00:00+00:00",
        "candidates": ["old candidate"], "preview": {"old": True},
    }
    success, message = plugin.scan_subscription("2")
    assert success and "本轮发现候选 0 条" in message and "手动搜索" in message
    assert "candidates" not in plugin._state["entries"]["old"]
    assert "preview" not in plugin._state["entries"]["old"]
    assert plugin._state["entries"]["old"]["status"] == "approved"
    assert next(iter(plugin._preview_search_cache.values()))["contexts"] == []


def test_cancelled_scan_does_not_search_more_keywords_or_replace_cache(plugin_host, monkeypatch):
    host = plugin_host
    plugin, _choice, chosen = _prepare_directory_confirmation(host)
    previous_cache = plugin._preview_search_cache.copy()
    calls = []

    def process(_chain, **kwargs):
        calls.append(kwargs)
        plugin._search_stop.set()
        return [chosen]

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    assert plugin.scan_subscription("2")[0] is False
    assert len(calls) == 1
    assert plugin._preview_search_cache == previous_cache
    assert not plugin._scan_lock.locked()


def _install_budget_contract(host, monkeypatch):
    """装配预算接口替身；用虚拟时钟验证适配器，不访问数据库和网络。"""
    from datetime import datetime, timezone
    now = [1000.0]
    calls = []
    sleeps = []

    class Unavailable(RuntimeError):
        def __init__(self, seconds, reason="cooldown"):
            self.retry_at = datetime.fromtimestamp(now[0] + seconds, timezone.utc).isoformat()
            self.wait_reason = reason

    class Cancelled(RuntimeError):
        pass

    stop = SimpleNamespace(is_system_stopped=False)
    repository = SimpleNamespace(claim=lambda _site, _interval: SimpleNamespace(acquired=True))

    class Budget:
        def __init__(self, *, repository, owner, cancelled, stop_state):
            self.repository, self.owner = repository, owner
            self.cancelled, self.stop_state = cancelled, stop_state

        def acquire(self, site_id, *, minimum_interval=0):
            if self.cancelled() or self.stop_state.is_system_stopped:
                raise Cancelled()
            calls.append((site_id, minimum_interval))
            return self.repository.claim(site_id, minimum_interval)

    module = ModuleType("app.application.subscription.sitebudget")
    module.SubscriptionSiteBudget = Budget
    module.SubscriptionSiteBudgetUnavailable = Unavailable
    monkeypatch.setitem(sys.modules, module.__name__, module)
    runtime = ModuleType("app.runtime.stop")
    runtime.runtime_stop_state = stop
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    monkeypatch.setattr(host.module.SearchChain, "subscription_search_repository", repository, raising=False)
    monkeypatch.setattr(host.module.SearchChain, "configure_subscription_site_budget",
                        lambda chain, budget: setattr(chain, "budget", budget), raising=False)
    adapter = sys.modules[host.module.CandidateSearchChain.__module__]
    monkeypatch.setattr(adapter.time, "time", lambda: now[0])
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(adapter.time, "sleep", sleep)
    monkeypatch.setattr(adapter.random, "uniform", lambda lower, upper: (lower + upper) / 2)
    return SimpleNamespace(now=now, calls=calls, sleeps=sleeps, repository=repository, adapter=adapter,
                           Unavailable=Unavailable, Cancelled=Cancelled, stop=stop)


@pytest.mark.parametrize("site_interval,expected", [(0, 15), (45, 45)])
def test_budget_wait_retries_and_respects_random_and_configured_site_intervals(
    plugin_host, monkeypatch, site_interval, expected,
):
    host = plugin_host
    contract = _install_budget_contract(host, monkeypatch)
    holds = []
    chain = host.module.CandidateSearchChain(lambda _site: None, lambda *args: holds.append(args))
    chain._budget_sites[5] = {"id": 5, "name": "天空", "limit_seconds": site_interval}

    def claim(_site, _interval):
        if contract.now[0] < 1003:
            raise contract.Unavailable(1003 - contract.now[0])
        return "lease"

    contract.repository.claim = claim
    assert host.module.configure_candidate_budget(chain, lambda: False)
    assert chain.budget.acquire(5) == "lease"
    assert contract.calls == [(5, expected)] * 4
    assert chain.site_waits[5] == 3
    assert contract.sleeps == [1, 1, 1]
    assert holds == [] and chain.site_reports == []


def test_budget_busy_waits_for_completion_and_cancel_stops_within_one_second(plugin_host, monkeypatch):
    host = plugin_host
    contract = _install_budget_contract(host, monkeypatch)
    chain = host.module.CandidateSearchChain(lambda _site: None, lambda *_args: None)

    def claim(_site, _interval):
        if contract.now[0] < 1002:
            raise contract.Unavailable(900, "busy")
        return "lease"

    contract.repository.claim = claim
    assert host.module.configure_candidate_budget(chain, lambda: contract.now[0] >= 1003)
    assert chain.budget.acquire(5) == "lease"
    contract.repository.claim = lambda *_args: (_ for _ in ()).throw(contract.Unavailable(10))
    with pytest.raises(contract.Cancelled):
        chain.budget.acquire(5)
    assert contract.now[0] == 1003 and chain.site_waits[5] == 3


@pytest.mark.parametrize("reason,seconds", [("cooldown", 120), ("failing", 10), ("busy", 900)])
def test_long_budget_wait_defers_same_site_for_remaining_queries_without_error_hold(
    plugin_host, monkeypatch, reason, seconds,
):
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    contract = _install_budget_contract(host, monkeypatch)
    shared_deferrals = {}
    calls = []

    def budget_search(chain, **kwargs):
        calls.append(kwargs["site"]["id"])
        try:
            chain.budget.acquire(kwargs["site"]["id"])
        except contract.Unavailable:
            return []
        return ["resource"]

    monkeypatch.setattr(host.module.SearchChain, "_search_site_torrents_with_budget", budget_search, raising=False)
    contract.repository.claim = lambda site, _interval: (_ for _ in ()).throw(
        contract.Unavailable(seconds, reason)) if site == 5 else "lease"
    chain = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site,
                                           budget_deferrals=shared_deferrals)
    assert host.module.configure_candidate_budget(chain, lambda: False)
    site = {"id": 5, "name": "天空"}
    assert chain._search_site_torrents_with_budget(site=site, keyword="Show S01") == []
    assert sum(contract.sleeps) == (60 if reason == "busy" else 0)
    another = host.module.CandidateSearchChain(plugin._site_hold, plugin._hold_site,
                                             budget_deferrals=shared_deferrals)
    assert host.module.configure_candidate_budget(another, lambda: False)
    assert another._search_site_torrents_with_budget(site=site, keyword="Show") == []
    assert another._search_site_torrents_with_budget(site={"id": 7}, keyword="Show") == ["resource"]
    assert calls == [5, 7]
    assert plugin._site_hold(site) is None
    assert another.site_reports[-1]["status"] == "deferred"


def test_budget_missing_interface_safely_keeps_legacy_search(plugin_host, monkeypatch):
    host = plugin_host
    chain = host.module.CandidateSearchChain(lambda _site: None, lambda *_args: None)
    assert host.module.configure_candidate_budget(chain, lambda: False) is False
    contract = _install_budget_contract(host, monkeypatch)
    monkeypatch.setattr(sys.modules["app.application.subscription.sitebudget"].SubscriptionSiteBudget,
                        "acquire", lambda self, site_id: "legacy")
    assert host.module.configure_candidate_budget(chain, lambda: False) is False


def test_stop_event_stays_cancelled_after_plugin_is_enabled_again(plugin_host):
    plugin = plugin_host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    previous = plugin._search_stop
    plugin.stop_service()
    plugin.init_plugin({"enabled": True})
    assert previous.is_set() and not plugin._search_stop.is_set()
    assert plugin._search_stop is not previous

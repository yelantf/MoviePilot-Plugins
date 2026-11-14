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
def plugin_host(monkeypatch, tmp_path):
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
    logs = []
    library_episodes = {}
    library_checks = []
    completion_calls = []
    system_values = {"IndexerSites": []}
    indexers = [
        {"id": 5, "name": "样例站", "is_active": True},
        {"id": 7, "name": "另一个站", "is_active": True},
        {"id": 9, "name": "已停用站", "is_active": False},
    ]

    for name in ("app", "app.application", "app.application.torrent", "app.application.site",
                 "app.application.subscription", "app.chain",
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

        def get_data_path(self):
            return tmp_path

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

        def recognize_media(self, *, media_source, media_id, mtype, **_kwargs):
            """模拟宿主按媒体 ID 识别目标剧集。"""
            return SimpleNamespace(media_source=media_source, media_id=media_id,
                                   type=mtype, season=None, seasons={1: list(range(1, 9))})

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

        def __init__(self):
            self.subscription_repository = SubscribeOper()

        def reconcile_subscription_completion(self, subscribe, meta, mediainfo):
            """订阅完成流程不应由本插件的媒体库核对调用。"""
            completion_calls.append(subscribe.id)
            raise AssertionError("插件不得触发订阅完成流程")

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

        def get_no_exists_info(self, *, meta, mediainfo, totals):
            """模拟宿主只读媒体库接口及整季缺失的空列表约定。"""
            library_checks.append(meta.subscription_id)
            present = library_episodes.get(meta.subscription_id)
            if present is None:
                return False, {}
            season, total = next(iter(totals.items()))
            missing = sorted(set(range(1, total + 1)) - set(present))
            if not missing:
                return True, {}
            key = build_media_key(mediainfo.media_source, mediainfo.media_id)
            return False, {key: {season: SimpleNamespace(
                episodes=[] if len(missing) == total else missing,
                total_episode=total, start_episode=1,
            )}}

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

    class RestoredFacts(SimpleNamespace):
        def from_dict(self, values):
            self.__dict__.update(copy.deepcopy(values))

    exports = {
        "app.application.subscription.contract": {
            "build_subscribe_meta": lambda subscribe: SimpleNamespace(
                type=SimpleNamespace(value=subscribe.type), begin_season=subscribe.season,
                subscription_id=subscribe.id,
            ),
        },
        "app.application.directory": {
            "DirectoryHelper": DirectoryHelper, "build_media_download_path": build_media_download_path,
            "validate_download_save_path": validate_download_save_path,
        },
        "app.schemas.file": {"FileURI": FileURI},
        "app.application.torrent.download": {"TorrentHelper": TorrentHelper},
        "app.application.site.sites": {"SitesHelper": lambda: SimpleNamespace(get_indexers=lambda: indexers)},
        "app.application.configuration": {"get_configured_system_config": lambda: SimpleNamespace(
            get=lambda key: system_values.get(key),
        )},
        "app.chain.download": {"DownloadChain": DownloadChain},
        "app.chain.media": {"MediaChain": MediaChain},
        "app.chain.search": {"SearchChain": SearchChain},
        "app.chain.subscribe": {"SubscribeChain": SubscribeChain},
        "app.domain.context": {"Context": SimpleNamespace, "MediaInfo": RestoredFacts,
                               "TorrentInfo": RestoredFacts},
        "app.schemas.context": {"MetaInfo": SimpleNamespace},
        "app.db.oper.subscribe": {"SubscribeOper": SubscribeOper},
        "app.db.oper.systemconfig": {"SystemConfigOper": SystemConfigOper},
        "app.plugins": {"_PluginBase": PluginBase},
        "app.schemas.media": {"build_media_key": build_media_key,
                              "resolve_media_identity": resolve_media_identity},
        "app.schemas.mediaserver": {"NotExistMediaInfo": NotExistMediaInfo},
        "app.schemas.types": {
            "ChainEventType": SimpleNamespace(ResourceDownload="resource.download"),
            "EventType": SimpleNamespace(SubscribeAdded="subscribe.added",
                                         SubscribeModified="subscribe.modified",
                                         TransferComplete="transfer.complete"),
            "MediaSource": lambda value: value,
            "MediaType": SimpleNamespace(TV=SimpleNamespace(value="电视剧")),
            "MessageType": SimpleNamespace(Plugin="plugin"),
            "SystemConfigKey": SimpleNamespace(
                IndexerSites="IndexerSites",
                UserFilterRuleGroups="UserFilterRuleGroups", CustomFilterRules="CustomFilterRules",
                SubscribeFilterRuleGroups="SubscribeFilterRuleGroups",
                BestVersionFilterRuleGroups="BestVersionFilterRuleGroups",
            ),
        },
        "app.sdk.events": {"Event": SimpleNamespace, "eventmanager": SimpleNamespace(register=register)},
        "app.sdk.logging": {"logger": SimpleNamespace(
            info=lambda message, *_args: logs.append(("info", str(message))),
            warning=lambda message, *_args: logs.append(("warning", str(message))),
            exception=lambda message, *_args: logs.append(("exception", str(message))),
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
    monkeypatch.delitem(sys.modules, f"{name}.cache", raising=False)
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
    adapter = sys.modules[module.CandidateSearchChain.__module__]
    monkeypatch.setattr(adapter, "REQUEST_PACER", SimpleNamespace(
        acquire=lambda _site, _cancelled: ("test-lease", 0), release=lambda *_args: None,
    ))
    return SimpleNamespace(
        module=module, subscriptions=subscriptions, storage=storage, jobs=jobs,
        library_episodes=library_episodes, library_checks=library_checks,
        completion_calls=completion_calls,
        search_results=search_results, search_calls=search_calls, downloads=downloads,
        messages=messages,
        directories=directories, directory_calls=directory_calls,
        directory_module=directory_module,
        group_filter_calls=group_filter_calls, system_values=system_values, indexers=indexers, logs=logs,
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


def _season_only_context(title, season=2, description="", labels=None):
    """模拟只识别季号、副标题总集数而没有具体集号的合集资源。"""
    context = _context(title)
    context.meta_info.begin_season = season
    context.meta_info.season_list = [season]
    context.meta_info.episode_list = []
    context.meta_info.begin_episode = None
    context.meta_info.end_episode = None
    context.meta_info.total_episode = 5
    context.torrent_info.description = description
    context.torrent_info.labels = labels if labels is not None else []
    return context


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
    panel = json.dumps(plugin.get_form()[1]["_candidate_preview"], ensure_ascii=False)
    assert "预计下载目录" in panel
    assert "/PtResourceDir2/bt/downloads/电视剧/国产剧" in panel
    assert "预计下载目录" not in json.dumps(plugin.get_page(), ensure_ascii=False)
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
    assert {item["kind"] for item in entry["candidates"]} == {
        "first_episode", "first_episode_bundle",
    }
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
    候选收单集 E01 与含首集多集资源，并在资源消失后保留发现记录。
    """
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    first = rules.resource_candidate(_context(
        "Show.S01E01.1080p.WEB-DL.HDR-TEAM",
        page_url="https://tracker.example/details.php?id=42&hit=1&passkey=secret",
    ), 1, "2026-11-02T01:00:00+00:00")
    assert first is not None
    bundle = rules.resource_candidate(_context("Show.S01E01-E02", episodes=(1, 2)),
                                      1, "now", total_episode=12)
    assert bundle["kind"] == "first_episode_bundle"
    assert bundle["episodes"] == [1, 2]
    assert plugin_host.module._candidate_label(bundle) == "含首集的多集资源"
    assert plugin_host.module._candidate_coverage(bundle) == "E01–E02/12"
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
    many = [{**first, "id": f"candidate-{number}", "title": f"Show.S01E01.{number}",
             "page_url": f"https://tracker.example/details.php?id={number}"}
            for number in range(101)]
    assert len(rules.merge_candidates([], many)) == 101


def test_subtitle_first_episode_bundle_is_scanned_and_previewed_without_full_season_bypass(plugin_host):
    """宿主从副标题识别第1-3集后，插件展示候选但不当成12集全集。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.total_episode = subscribe.lack_episode = 12
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    resource = _context(
        "Love in Disguise S01 1080p AMZN WEB-DL DDP.2.0 H.264-CHDWEB",
        episodes=(1, 2, 3), page_url="https://tracker.example/details.php?id=123",
    )
    resource.torrent_info.description = "韩剧：我的有罪之人 第1-3集 / Love in Disguise"
    resource.meta_info.resource_team = "CHDWEB"
    host.search_results.append(resource)

    assert plugin.scan_subscription("2")[0]
    candidates = plugin._state["entries"]["2"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "first_episode_bundle"
    assert candidates[0]["episodes"] == [1, 2, 3]
    assert host.module._candidate_coverage(candidates[0]) == "E01–E03/12"
    choice = _form_items(plugin, "candidate")[0]
    assert "含首集的多集资源 E01–E03/12" in choice["title"]
    assert plugin.preview_candidate(choice["value"])[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["kind"] == "first_episode_bundle"
    assert not host.module.is_complete_season_download(
        _download_event(2, context=resource, episodes={1, 2, 3}).event_data, subscribe,
    )


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


@pytest.mark.parametrize("suffix,description,labels,expected_kind", [
    ("", "幸福伽菜子的快乐杀手生活 第二季 全5集", [], "suspected_full"),
    ("", "第二季 5集全", [], "suspected_full"),
    ("", "第二季 全五集", [], "suspected_full"),
    ("", "第二季 全５集", [], "suspected_full"),
    ("", "", ["中字", "完结"], "suspected_full"),
    ("", "", "完结", "suspected_full"),
    ("", "", ("Complete",), "suspected_full"),
    (" Complete", "", [], "suspected_full"),
    ("", "", [], None),
    ("", "更新至5集", [], None),
    ("", "未完结", [], None),
    ("", "非全集", [], None),
    ("", "", ["尚未完结"], None),
    (" NOT COMPLETE", "", [], None),
    ("", "Incomplete", [], None),
    ("", "", ["ONGOING"], None),
])
def test_season_pack_hints_include_subtitle_and_labels_without_inventing_episodes(
    plugin_host, suffix, description, labels, expected_kind,
):
    """合集线索可来自主标题、副标题或标签，缺少集号时始终保留不确定性。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    context = _season_only_context(
        "Happy Kanakos Killer Life 2025 S02 1080p DMM WEB-DL AAC2.0 H.264-DepWeb" + suffix,
        description=description, labels=labels,
    )
    candidate = rules.resource_candidate(context, 2, "now", total_episode=5)
    if expected_kind is None:
        assert candidate is None
    else:
        assert candidate["kind"] == expected_kind
        assert candidate["episodes"] == []
        assert candidate["total_episode"] == 5
        assert plugin_host.module._candidate_label(candidate) == "疑似整季（集数待核实）"
        assert rules.resource_candidate(context, 1, "now", total_episode=5) is None


@pytest.mark.parametrize("episode_token,episodes,expected_kind", [
    ("S02E01", (1,), "first_episode"),
    ("S02", (2,), None),
    ("S02E02", (1, 2, 3, 4, 5), None),
    ("S02", (1, 2, 3, 4, 5), "full_season"),
])
def test_pack_hints_do_not_override_explicit_episode_evidence(
    plugin_host, episode_token, episodes, expected_kind,
):
    """完结标签不能把 E01 或后续单集改判为整季，明确全集仍按集号判定。"""
    rules = importlib.import_module(f"{plugin_host.module.__name__}.rules")
    context = _context(f"Show.{episode_token}.1080p.WEB-DL", episodes=episodes)
    context.meta_info.begin_season = 2
    context.meta_info.season_list = [2]
    context.meta_info.begin_episode = min(episodes)
    context.torrent_info.description = "第二季 全5集"
    context.torrent_info.labels = ["完结"]
    candidate = rules.resource_candidate(context, 2, "now", total_episode=5)
    assert (candidate["kind"] if candidate else None) == expected_kind


def test_subtitle_and_label_packs_survive_scan_and_cached_preview_after_reload(plugin_host):
    """两类漏判合集可扫描选择，重启后复用缓存预览仍不宣称已核实全集。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.season = 2
    subscribe.total_episode = subscribe.lack_episode = 5
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    labeled = _season_only_context(
        "Happy Kanakos Killer Life 2025 S02 1080p DMM WEB-DL AAC2.0 H.264-DepWeb",
        labels=["完结"],
    )
    described = _season_only_context(
        "Happy Kanakos Killer Life S02 2026 1080p DMM WEB-DL AAC2.0 H.264-DepWeb",
        description="幸福伽菜子的快乐杀手生活 第二季 全5集",
    )
    described.torrent_info.site = 7
    described.torrent_info.site_name = "另一个站"
    host.search_results.extend([labeled, described])

    assert plugin.scan_subscription("2")[0]
    entry = host.storage["gate_state"]["entries"]["2"]
    assert entry["last_scan_count"] == 2
    assert {item["site_id"] for item in entry["candidates"]} == {5, 7}
    assert all(item["kind"] == "suspected_full" and item["episodes"] == []
               for item in entry["candidates"])
    choices = _form_items(plugin, "candidate")
    assert len(choices) == 2
    assert all("疑似整季（集数待核实）" in item["title"] for item in choices)
    plugin.stop_service()
    reloaded = host.module.SubscribeOverwatcher()
    reloaded.init_plugin({"enabled": True})
    host.search_calls.clear()

    assert reloaded.preview_candidate(choices[0]["value"], rule_mode="all_sites")[0]
    assert host.search_calls == []
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["search_cached"] is True
    assert preview["matched_count"] == 2
    assert all(item["kind"] == "suspected_full" and item["episodes"] == []
               for item in preview["matched"])
    assert subscribe.state == "S"
    assert host.downloads == []


@pytest.mark.parametrize("description,labels", [
    ("", ["完结"]),
    ("第二季 全5集", []),
])
def test_season_pack_hint_does_not_allow_unverified_full_season_download(
    plugin_host, description, labels,
):
    """关闭全集确认也不能只凭副标题或站点标签自动放行合集。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True, "confirm_full_pack": False})
    subscribe = _subscribe(2)
    subscribe.season = 2
    subscribe.total_episode = 5
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    context = _season_only_context("Show.S02.1080p.WEB-DL", description=description, labels=labels)
    rules = importlib.import_module(f"{host.module.__name__}.rules")
    assert rules.resource_candidate(context, 2, "now", total_episode=5)["kind"] == "suspected_full"
    event = _download_event(2, context=context, episodes={1, 2, 3, 4, 5})
    assert not rules.is_complete_season_download(event.event_data, subscribe)
    plugin.intercept_download(event)
    assert event.event_data.cancel is True


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
    assert {item["id"] for item in plugin.get_service()} == {
        "candidate_refresh", "subscription_progress_check",
    }
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


def test_progress_complete_auto_approval_keeps_rules_and_skips_candidate_search(plugin_host):
    """订阅进度变满后直接批准，即使还没有整理入库。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.quality = "WEB-DL"
    subscribe.sites = [7]
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    assert plugin.approve_progress_complete() == 0
    assert subscribe.state == "S"
    assert plugin._state["entries"]["2"]["status"] == "pending"

    subscribe.lack_episode = 0
    assert plugin.refresh_candidates() is True
    assert plugin._state["entries"]["2"]["status"] == "approved"
    assert subscribe.state == "R"
    assert subscribe.quality == "WEB-DL"
    assert subscribe.sites == [7]
    assert not any("candidate_filter" in call for call in host.search_calls)
    assert any("subscription_search" in call for call in host.search_calls)


def test_first_library_check_updates_full_progress_and_releases_subscription_without_finishing(plugin_host):
    """已全集入库时恢复接管前状态，退出插件管理，但保留活动订阅给其他插件。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = set(range(1, 9))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert subscribe.state == "S"

    assert plugin.approve_progress_complete() == 0
    assert subscribe.total_episode == 8 and subscribe.lack_episode == 0
    assert subscribe.state == "N" and host.subscriptions[2] is subscribe
    assert plugin._state["entries"]["2"]["status"] == "library_complete"
    assert plugin._is_managed(subscribe) is False
    assert host.library_checks == [2] and host.completion_calls == []
    event = _download_event(2)
    plugin.intercept_download(event)
    assert event.event_data.cancel is False
    assert plugin.refresh_candidates() is True
    assert host.library_checks == [2]
    assert not any("candidate_filter" in call for call in host.search_calls)
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted._is_managed(subscribe) is False
    restarted.refresh_candidates()
    assert host.library_checks == [2] and restarted._state["entries"]["2"]["status"] == "library_complete"


def test_first_library_check_updates_partial_progress_then_scans_only_once(plugin_host):
    """部分入库同步缺集数，候选照常扫描；手动和重启后的扫描不重复查库。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = {1, 2, 4}
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    assert plugin.refresh_candidates() is True
    assert subscribe.lack_episode == 5 and subscribe.state == "S"
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert plugin._state["entries"]["2"]["candidates"]
    assert host.library_checks == [2] and host.completion_calls == []
    assert plugin.scan_subscription("2")[0] is True
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    restarted.refresh_candidates()
    assert host.library_checks == [2]


def test_first_library_check_treats_empty_missing_list_as_whole_season_missing(plugin_host):
    """宿主用空 episodes 表示整季缺失，不能误判为进度已满。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.lack_episode = 0
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = set()
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    assert plugin.approve_progress_complete() == 0
    assert subscribe.lack_episode == 8 and subscribe.state == "S"
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert host.library_checks == [2] and host.completion_calls == []


def test_library_check_failure_keeps_candidate_scan_and_does_not_repeat(plugin_host, monkeypatch):
    """媒体库查询失败只影响首次进度核对，后续候选扫描仍可用。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    host.search_results.append(_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM"))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    attempts = []

    def fail(_chain, **_kwargs):
        attempts.append(2)
        raise RuntimeError("media server unavailable")

    monkeypatch.setattr(host.module.DownloadChain, "get_no_exists_info", fail)
    assert plugin.refresh_candidates() is True
    entry = plugin._state["entries"]["2"]
    assert entry["library_checked"] is True and entry["candidates"]
    assert "library_check_error" in entry
    assert host.subscriptions[2].state == "S"
    assert plugin.refresh_candidates() is True
    assert attempts == [2] and host.completion_calls == []


def test_library_complete_scope_growth_starts_a_new_management_cycle(plugin_host):
    """齐集后目标总集数扩张时可以重新接管，并按新范围再查一次。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = set(range(1, 9))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.refresh_candidates() is True
    assert plugin._state["entries"]["2"]["status"] == "library_complete"

    subscribe.total_episode = 10
    subscribe.lack_episode = 2
    assert plugin.refresh_candidates() is True
    assert host.library_checks == [2, 2]
    assert subscribe.lack_episode == 2 and subscribe.state == "S"
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert host.completion_calls == []


def test_first_library_check_preserves_historical_manual_pause(plugin_host):
    """手动暂停的历史订阅即使已齐集，也只刷新进度并保留原暂停。"""
    host = plugin_host
    historical = _subscribe(1)
    historical.state = "S"
    host.subscriptions[1] = historical
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.library_episodes[1] = set(range(1, 9))
    assert plugin.adopt_subscription(1)[0] is True
    assert plugin.refresh_candidates() is True
    assert historical.state == "S" and historical.lack_episode == 0
    assert plugin._state["entries"]["1"]["status"] == "library_complete"
    assert plugin._is_managed(historical) is False
    assert host.completion_calls == []


@pytest.mark.parametrize("state,best_version", [("P", 0), ("N", 1)])
def test_first_library_check_skips_pending_upgrade_and_best_version(plugin_host, state, best_version):
    """待定及洗版订阅保留原有确认流程，不以已有文件判为完成。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.state = state
    subscribe.best_version = best_version
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = set(range(1, 9))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    assert plugin.approve_progress_complete() == 0
    assert plugin.refresh_candidates() is True
    assert host.library_checks == [] and host.completion_calls == []
    assert plugin._state["entries"]["2"]["status"] == "pending"


def test_first_library_check_uses_expanded_media_total_before_treating_show_as_complete(plugin_host, monkeypatch):
    """订阅记录总集数过旧时，新识别的季集数会阻止错误退出管理。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    host.library_episodes[2] = set(range(1, 9))
    monkeypatch.setattr(host.module.MediaChain, "recognize_media", lambda _self, **kwargs: SimpleNamespace(
        media_source=kwargs["media_source"], media_id=kwargs["media_id"],
        type=kwargs["mtype"], season=None, seasons={1: list(range(1, 11))},
    ))
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))

    assert plugin.approve_progress_complete() == 0
    assert subscribe.total_episode == 10 and subscribe.lack_episode == 2
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert host.library_checks == [2] and host.completion_calls == []


@pytest.mark.parametrize("total,start,lack", [
    (0, 1, 0), (8, 9, 0), (8, 1, None), (8, 1, 1),
])
def test_auto_approval_requires_valid_full_progress(plugin_host, total, start, lack):
    """进度字段无效或仍显示缺集时保持待确认。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.total_episode = total
    subscribe.start_episode = start
    subscribe.lack_episode = lack
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.approve_progress_complete() == 0
    assert subscribe.state == "S"


def test_subscribe_modified_schedules_full_progress_approval(plugin_host):
    """订阅缺失集数变为零后立即安排检查，普通修改不安排。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.jobs.pop("progress-check", None)
    plugin.on_subscribe_modified(SimpleNamespace(event_data={"subscribe_id": 2, "fields": ["quality"]}))
    assert "progress-check" not in host.jobs

    subscribe.lack_episode = 0
    plugin.on_subscribe_modified(SimpleNamespace(event_data={"subscribe_id": 2, "fields": ["lack_episode"]}))
    job, delay, kwargs = host.jobs["progress-check"]
    assert delay == 2
    job(**kwargs)
    assert plugin._state["entries"]["2"]["status"] == "approved"
    assert subscribe.state == "R"


def test_transfer_event_checks_only_matching_subscription_progress(plugin_host):
    """手动整包下载的整理事件可触发匹配订阅的进度检查。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.jobs.pop("progress-check", None)
    plugin.on_transfer_complete(SimpleNamespace(event_data={
        "mediainfo": SimpleNamespace(media_source="themoviedb", media_id="999"),
        "meta": SimpleNamespace(begin_season=1),
    }))
    assert "progress-check" not in host.jobs

    subscribe.lack_episode = 0
    plugin.on_transfer_complete(SimpleNamespace(event_data={
        "mediainfo": SimpleNamespace(media_source="themoviedb", media_id="123"),
        "meta": SimpleNamespace(begin_season=1),
    }))
    job, delay, kwargs = host.jobs["progress-check"]
    assert delay == 2
    job(**kwargs)
    assert plugin._state["entries"]["2"]["status"] == "approved"
    assert subscribe.state == "R"


def test_auto_approval_preserves_external_pause_and_pending_guard(plugin_host):
    """进度检查不能覆盖用户暂停、增强版待定或显式撤销。"""
    host = plugin_host
    historical = _subscribe(1)
    historical.state = "S"
    host.subscriptions[1] = historical
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    assert plugin.adopt_subscription(1)[0] is True
    historical.lack_episode = 0
    assert plugin.approve_progress_complete() == 0
    assert plugin._state["entries"]["1"]["status"] == "pending"

    subscribe = _subscribe(2)
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    subscribe.lack_episode = 0
    subscribe.state = "P"
    assert plugin.approve_progress_complete() == 0
    subscribe.state = "S"
    plugin._state["entries"]["2"]["revoked"] = True
    assert plugin.approve_progress_complete() == 0
    assert subscribe.state == "S"


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
    assert "订阅进度 0/8 集" in choices[0]["title"]
    assert "订阅进度 1/8 集" in choices[1]["title"]
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
    assert ["#1", "历史剧 S01", "手动纳入", "已批准", "订阅进度 2/8 集"] in rows
    assert ["#2", "新剧 S01", "新增接管", "待确认", "订阅进度 0/8 集"] in rows


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
    bundle = _context("Show.S01E01-E03.1080p.WEB-DL", episodes=(1, 2, 3))
    cross_season = _context("Show.S01-S02.Complete", episodes=tuple(range(1, 9)))
    cross_season.meta_info.season_list = [1, 2]
    cross_season.meta_info.end_season = 2
    other_season = _context("Show.S03.Complete", episodes=tuple(range(1, 9)))
    other_season.meta_info.begin_season = 3
    other_season.meta_info.season_list = [3]
    host.search_results.extend([first, full_1080, full_2160, suspect, bundle, cross_season, other_season])

    plugin.refresh_candidates()
    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert [item["kind"] for item in candidates] == [
        "full_season", "full_season", "cross_season", "suspected_full",
        "first_episode_bundle", "first_episode",
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


def test_pending_candidate_can_be_downloaded_once_as_full_torrent_without_rule_approval(plugin_host):
    """跨季候选走单资源手动入口整包提交，订阅保持待确认。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.directories.extend([_directory("默认盘", "/default"), _directory("目标盘", "/chosen")])
    context = _context("Show.S01-S05.Complete.1080p", episodes=tuple(range(1, 9)),
                       page_url="https://tracker.example/details.php?id=42")
    context.meta_info.season_list = [1, 2, 3, 4, 5]
    context.meta_info.end_season = 5
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    host.search_results.append(context)
    assert plugin.refresh_candidates() is True
    candidate = plugin._state["entries"]["2"]["candidates"][0]
    assert candidate["kind"] == "cross_season"
    choice = f"2:{candidate['id']}"
    searches_before_download = len(host.search_calls)

    plugin.init_plugin({
        "enabled": True, "candidate": choice, "download_directory": "/chosen",
        "rule_json": "{invalid rule draft",
        "operation": "download_candidate", "operation_request_id": "download-1",
    })

    assert plugin._state["last_confirmation"]["success"] is True
    assert plugin._state["last_confirmation"]["request_id"] == "download-1"
    assert len(host.downloads) == 1
    downloaded, options = host.downloads[0]
    assert downloaded.torrent_info.page_url == context.torrent_info.page_url
    assert downloaded.torrent_info.enclosure == context.torrent_info.enclosure
    assert len(host.search_calls) == searches_before_download
    assert options == {"episodes": None, "source": "Manual", "save_path": "/chosen", "return_detail": True}
    assert plugin._state["entries"]["2"]["status"] == "pending"
    assert plugin._state["entries"]["2"].get("preview") is None
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].sites == []
    assert plugin._state["entries"]["2"]["direct_download_history"][0]["candidate_id"] == candidate["id"]
    assert "最近一次性整包下载提交" in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert host.storage["config"]["operation"] == "save"


def test_one_time_download_uses_cached_link_and_checks_selected_resource(plugin_host):
    """有效内存链接直接提交；链接缺失时重查，媒体身份或详情变化时拒绝。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.directories.append(_directory("默认盘", "/downloads"))
    context = _context("Show.S01.Complete.1080p", episodes=tuple(range(1, 9)),
                       page_url="https://tracker.example/details.php?id=42")
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    host.search_results.append(context)
    assert plugin.refresh_candidates() is True
    candidate = plugin._state["entries"]["2"]["candidates"][0]
    choice = f"2:{candidate['id']}"
    searches_after_scan = len(host.search_calls)

    host.search_results.clear()
    success, message = plugin.download_candidate_once(choice)
    assert success is True, message
    assert len(host.search_calls) == searches_after_scan
    assert host.downloads[0][0].torrent_info.enclosure == context.torrent_info.enclosure
    host.downloads.clear()

    context.torrent_info.page_url = "https://tracker.example/details.php?id=43"
    assert plugin.download_candidate_once(choice)[0] is False
    assert host.downloads == []
    context.torrent_info.page_url = "https://tracker.example/details.php?id=42"
    context.media_info.media_id = "other-show"
    assert plugin.download_candidate_once(choice)[0] is False
    assert host.downloads == []
    context.media_info.media_id = "123"
    context.torrent_info.enclosure = None
    available_online = copy.deepcopy(context)
    available_online.torrent_info.enclosure = "https://secret.example/torrent"
    host.search_results.append(available_online)
    success, message = plugin.download_candidate_once(choice)
    assert success is False and "重新扫描此剧集" in message
    assert host.downloads == []
    host.search_results.clear()
    context.torrent_info.enclosure = "https://secret.example/torrent"
    candidate["page_url"] = None
    assert "详情链接" in plugin.download_candidate_once(choice)[1]
    assert host.downloads == []
    assert len(host.search_calls) == searches_after_scan


def test_historical_candidate_keeps_memory_link_after_empty_scan(plugin_host):
    """旧种子不在本轮结果里时，仍可使用内存历史资源的真实下载地址。"""
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    host.directories.append(_directory("默认盘", "/downloads"))
    context = _context("Show.S01.Complete.1080p", episodes=tuple(range(1, 9)),
                       page_url="https://tracker.example/details.php?id=42")
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    host.search_results.append(context)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    assert plugin.refresh_candidates() is True
    candidate = plugin._state["entries"]["2"]["candidates"][0]
    choice = f"2:{candidate['id']}"

    host.search_results.clear()
    success, message = plugin.scan_subscription("2", selected_candidate=choice)
    assert success and "所选版本本轮未再次找到" in message
    assert plugin._state["entries"]["2"]["candidates"][0]["page_url"] == candidate["page_url"]
    assert "历史保留" in _form_items(plugin, "candidate")[0]["title"]
    assert "历史保留" in json.dumps(plugin.get_page(), ensure_ascii=False)
    cached = next(iter(plugin._preview_search_cache.values()))
    assert cached["contexts"] == [] and cached["historical_contexts"] == [context]
    searches_before_download = len(host.search_calls)
    success, message = plugin.download_candidate_once(choice)
    assert success is True
    assert len(host.search_calls) == searches_before_download
    assert host.downloads[0][0] is context

    host.system_values["IndexerSites"] = [7]
    assert plugin.scan_subscription("2")[0] is True
    assert next(iter(plugin._preview_search_cache.values()))["covered_sites"] == [7]
    searches_before_download = len(host.search_calls)
    assert plugin.download_candidate_once(choice)[0] is True
    assert len(host.search_calls) == searches_before_download
    host.system_values["IndexerSites"] = []

    host.search_results.append(context)
    success, message = plugin.scan_subscription("2", selected_candidate=choice)
    assert success and "所选版本本轮未再次找到" not in message
    assert next(iter(plugin._preview_search_cache.values()))["historical_contexts"] == []
    current_without_link = copy.deepcopy(context)
    current_without_link.torrent_info.enclosure = None
    host.search_results[:] = [current_without_link]
    assert plugin.scan_subscription("2")[0] is True
    cached = next(iter(plugin._preview_search_cache.values()))
    assert cached["contexts"] == [current_without_link]
    assert cached["historical_contexts"] == [context]
    searches_before_download = len(host.search_calls)
    assert plugin.download_candidate_once(choice)[0] is True
    assert len(host.search_calls) == searches_before_download
    assert host.downloads[-1][0] is context


def test_historical_search_cache_restores_without_credentials_and_previews_separately(plugin_host):
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.directories.append(_directory("默认盘", "/downloads"))
    context = _context("Show.S01.Complete.1080p", episodes=tuple(range(1, 9)),
                       page_url="https://tracker.example/details.php?id=42")
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    context.torrent_info.enclosure = "https://secret.example/torrent?passkey=private"
    host.search_results.append(context)
    assert plugin.refresh_candidates() is True
    candidate = plugin._state["entries"]["2"]["candidates"][0]
    choice = f"2:{candidate['id']}"
    host.search_results.clear()
    assert plugin.scan_subscription("2")[0] is True
    cache_file = next((plugin.get_data_path() / "search-cache").glob("*.json"))
    saved = json.loads(cache_file.read_text())
    assert saved["contexts"] == [] and len(saved["historical_contexts"]) == 1
    assert "enclosure" not in json.dumps(saved) and "passkey=private" not in json.dumps(saved)

    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted.preview_candidate(choice)[0] is True
    preview = restarted._state["entries"]["2"]["preview"]
    assert preview["matched_count"] == preview["rejected_count"] == 0
    assert preview["historical_candidates"][0]["id"] == candidate["id"]
    searches_before_download = len(host.search_calls)
    success, message = restarted.download_candidate_once(choice)
    assert success is False and "重新扫描此剧集" in message
    assert len(host.search_calls) == searches_before_download
    assert host.downloads == []
    host.search_results.append(context)
    assert restarted.download_candidate_once(choice)[0] is False
    assert len(host.search_calls) == searches_before_download
    assert restarted.scan_subscription("2")[0] is True
    searches_after_manual_scan = len(host.search_calls)
    assert restarted.download_candidate_once(choice)[0] is True
    assert len(host.search_calls) == searches_after_manual_scan
    assert host.downloads[-1][0].torrent_info.enclosure == context.torrent_info.enclosure
    saved["historical_contexts"][0]["torrent_info"]["enclosure"] = context.torrent_info.enclosure
    cache_file.write_text(json.dumps(saved))
    legacy = host.module.SubscribeOverwatcher()
    legacy.init_plugin({"enabled": True})
    assert not cache_file.exists()


def test_historical_download_link_survives_newer_empty_single_site_cache(plugin_host):
    host = plugin_host
    plugin, choice, context = _prepare_directory_confirmation(host)
    page_url = "https://tracker.example/details.php?id=42"
    context.torrent_info.page_url = page_url
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    plugin._state["entries"]["2"]["candidates"][0]["page_url"] = page_url
    host.directories.append(_directory("默认盘", "/downloads"))
    plugin._remember_search(host.subscriptions[2], [], [5], "规则预览搜索", "空搜索")
    assert plugin._cache_parts(host.subscriptions[2], [5])[0][0]["entry"]["contexts"] == []
    searches_before_download = len(host.search_calls)
    success, message = plugin.download_candidate_once(choice, download_directory="/downloads")
    assert success is True, message
    assert len(host.search_calls) == searches_before_download
    assert host.downloads[-1][0] is context


def test_one_time_download_requires_manual_scan_after_restart_without_persisting_credentials(plugin_host):
    """重启后没有内存下载链接，须手动扫描；持久缓存不得保存下载凭据。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    host.subscriptions[2] = _subscribe(2)
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    host.directories.append(_directory("默认盘", "/downloads"))
    context = _context("Show.S01.Complete.1080p", episodes=tuple(range(1, 9)),
                       page_url="https://tracker.example/details.php?id=42")
    context.torrent_info.enclosure = "https://secret.example/torrent?passkey=private"
    context.media_info = SimpleNamespace(media_source="themoviedb", media_id="123", type="电视剧")
    host.search_results.append(context)
    assert plugin.refresh_candidates() is True
    candidate = plugin._state["entries"]["2"]["candidates"][0]
    choice = f"2:{candidate['id']}"
    searches_after_scan = len(host.search_calls)
    cache_dir = plugin.get_data_path() / "search-cache"
    cache_file = next(cache_dir.glob("*.json"))
    cache_text = cache_file.read_text()
    assert "passkey=private" not in cache_text and "cookie-secret" not in cache_text

    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert "secret.example" not in json.dumps(host.storage)
    visible = json.dumps((restarted.get_form(), restarted.get_page()))
    assert "cookie-secret" not in visible and "passkey=private" not in visible
    host.search_results.clear()
    success, message = restarted.download_candidate_once(choice)
    assert success is False and "重新扫描此剧集" in message
    assert host.downloads == []
    host.search_results.append(context)
    assert restarted.download_candidate_once(choice)[0] is False
    assert len(host.search_calls) == searches_after_scan
    assert restarted.scan_subscription("2")[0] is True
    searches_after_manual_scan = len(host.search_calls)
    assert restarted.download_candidate_once(choice)[0] is True
    assert len(host.search_calls) == searches_after_manual_scan
    assert len(host.downloads) == 1

    # 如果磁盘上残留过渡版本写入的凭据缓存，启动时删除整个文件。
    cached = json.loads(cache_text)
    cached["contexts"][0]["torrent_info"]["enclosure"] = context.torrent_info.enclosure
    cached["contexts"][0]["torrent_info"]["site_cookie"] = context.torrent_info.site_cookie
    cache_file.write_text(json.dumps(cached))
    legacy = host.module.SubscribeOverwatcher()
    legacy.init_plugin({"enabled": True})
    assert not cache_file.exists()
    assert legacy.download_candidate_once(choice)[0] is False
    assert legacy.scan_subscription("2")[0] is True
    assert legacy.download_candidate_once(choice)[0] is True
    assert len(host.downloads) == 2
    assert host.downloads[-1][0].torrent_info.enclosure == context.torrent_info.enclosure
    assert host.downloads[-1][0].torrent_info.site_cookie == context.torrent_info.site_cookie


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
    first.torrent_info.seeders = 0
    first.torrent_info.downloadvolumefactor = 0
    first.torrent_info.uploadvolumefactor = 2
    second.torrent_info.seeders = 19
    second.torrent_info.downloadvolumefactor = 0.5
    second.torrent_info.uploadvolumefactor = 1
    host.search_results.extend([first, second, third])
    plugin.refresh_candidates()

    candidates = host.storage["gate_state"]["entries"]["2"]["candidates"]
    assert len(candidates) == 3
    by_site = {item["site_name"]: item for item in candidates}
    first_candidate = by_site["样例站"]
    second_candidate = by_site["另一个站"]
    third_candidate = by_site["第三个站"]
    assert first_candidate["seeders"] == 0 and first_candidate["promotion"] == "免费 · 上传 2×"
    assert second_candidate["seeders"] == 19 and second_candidate["promotion"] == "下载计费 50%"
    assert third_candidate["seeders"] is None and third_candidate["promotion"] == "未知"
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
    first_choice = next(item for item in choices if item["site_id"] == 5)
    assert "做种 0 人" in first_choice["title"] and "免费 · 上传 2×" in first_choice["title"]
    assert first_choice["seeders_label"] == "0 人" and first_choice["promotion"] == "免费 · 上传 2×"
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
    assert {node["text"] for node in grouped_row["content"][5]["content"]} == {"样例站：0 人", "另一个站：19 人"}
    assert {node["text"] for node in grouped_row["content"][6]["content"]} == {
        "样例站：免费 · 上传 2×", "另一个站：下载计费 50%",
    }
    assert other_row["content"][5]["content"][0]["text"] == "未知"
    assert other_row["content"][6]["content"][0]["text"] == "未知"
    site_links = [item for item in grouped_row["content"][2]["content"] if item["component"] == "a"]
    assert {item["props"]["href"] for item in site_links} == {
        "https://one.example/details.php?id=11", "https://two.example/details.php?id=22",
    }
    assert other_row["content"][2]["content"][0]["props"]["href"] == \
        "https://three.example/details.php?id=33"
    assert all(row["content"][3] == {"component": "td", "text": title} for row in rows)
    actions = grouped_row["content"][-1]["content"]
    config_actions = [action for action in actions if action["content"][0]["text"].startswith("配置·")]
    download_actions = [action for action in actions if action["content"][0]["text"].startswith("下载·")]
    assert {action["props"]["data-candidate-id"] for action in config_actions} == {
        f"2:{first_candidate['id']}", f"2:{second_candidate['id']}",
    }
    assert {action["content"][0]["text"] for action in config_actions} == {"配置·样例站", "配置·另一个站"}
    assert {action["props"]["data-candidate-id"] for action in download_actions} == {
        f"2:{first_candidate['id']}", f"2:{second_candidate['id']}",
    }
    assert all("前往配置页下载" in action["props"]["aria-label"] for action in download_actions)
    assert other_row["content"][-1]["content"][0]["props"]["data-candidate-id"] == \
        f"2:{third_candidate['id']}"

    choice = next(item["value"] for item in choices if "另一个站" in item["title"])
    assert plugin.preview_candidate(choice, '{"sites": []}')[0] is True
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 3
    preview_table = plugin.get_form()[1]["_candidate_preview"]["table"]
    preview_rows = preview_table["content"][1]["content"]
    assert len(preview_rows) == 2
    grouped_preview = next(row for row in preview_rows
                           if row["content"][5]["content"][1]["text"] == "1,073,741,824 B")
    assert any(row["content"][5]["content"][1]["text"] == "1,073,741,825 B"
               for row in preview_rows)
    assert all(row["content"][5]["content"][0]["text"] == "1.00 GiB" for row in preview_rows)
    assert grouped_preview["content"][5]["props"]["title"] == "精确大小：1,073,741,824 B"
    assert {node["text"] for node in grouped_preview["content"][6]["content"]} == {"样例站：0 人", "另一个站：19 人"}
    assert {node["text"] for node in grouped_preview["content"][7]["content"]} == {
        "样例站：免费 · 上传 2×", "另一个站：下载计费 50%",
    }
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


def test_custom_sites_can_confirm_without_the_candidates_original_site(plugin_host):
    """候选仅作规则模板；所选其他站点有符合规则的资源时允许确认。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    other_site = copy.deepcopy(chosen)
    other_site.torrent_info.site = 7
    other_site.torrent_info.site_name = "另一个站"
    host.search_results.append(other_site)
    plugin.refresh_candidates()
    assert _form_items(plugin, "rule_sites") == [
        {"title": "另一个站", "value": 7}, {"title": "样例站", "value": 5},
    ]
    config = {"operation": "preview", "candidate": choice, "rule_mode": "custom", "rule_sites": [7]}
    assert plugin.preview_search_status(config)["search_sites"] == [7]
    plugin.init_plugin({"enabled": True, **config})
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["site_id"] == 7
    assert preview["search_keyword_verified"] is True
    assert preview["search_cached"] is True
    assert preview["rules"]["sites"] == [7]
    plugin.init_plugin({**host.storage["config"], "operation": "confirm"})
    assert host.subscriptions[2].sites == [7]


def test_custom_sites_preview_uses_selected_subset_and_keeps_full_cache(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    other_site = copy.deepcopy(chosen)
    other_site.torrent_info.site = 7
    other_site.torrent_info.site_name = "另一个站"
    host.search_results.append(other_site)
    plugin.refresh_candidates()
    host.search_calls.clear()
    assert plugin.preview_candidate(choice, '{"sites":[7,5,7]}', "custom", refresh_search=True)[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["rules"]["sites"] == [5, 7]
    assert preview["matched_count"] == 2
    assert host.search_calls[0]["sites"] == [5, 7]
    assert plugin.preview_search_status({"operation": "preview", "candidate": choice,
                                         "rule_mode": "all_sites"})["status"] == "cached"
    calls = len(host.search_calls)
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    assert len(host.search_calls) == calls
    assert plugin.confirm_candidate(choice, rule_mode="all_sites")[0]
    assert host.subscriptions[2].sites == []


def test_custom_site_auto_switches_keyword_using_selected_site_resource(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    other_site = copy.deepcopy(chosen)
    other_site.torrent_info.site = 7
    other_site.torrent_info.site_name = "另一个站"
    host.search_results.append(other_site)
    plugin.refresh_candidates()
    cached = next(iter(plugin._preview_search_cache.values()))
    other_id = host.module.preview_resource(other_site, 1, 8, host.module._utc_now())["id"]
    cached["diagnostics"]["original_search"]["resource_ids"] = []
    assert other_id in cached["diagnostics"]["resource_keywords"]
    success, _message, draft = plugin.preview_candidate(choice, '{"sites":[7]}', "custom")
    assert success
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["keyword_choice_auto_changed"] is True
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["site_id"] == 7
    assert plugin.confirm_candidate(choice, draft, "custom", keyword_choice="seasonal")[0]
    assert host.subscriptions[2].sites == [7]
    assert host.subscriptions[2].keyword == preview["search_keyword"]


def test_custom_sites_requires_selection(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_search_status({"operation": "preview", "candidate": choice,
                                         "rule_mode": "custom", "rule_sites": []})["success"] is False
    assert plugin.preview_candidate(choice, '{"sites":[]}', "custom")[0] is False


def test_data_page_handoff_does_not_refresh_but_all_sites_needs_full_cache(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    host.search_calls.clear()
    assert choice in json.dumps(plugin.get_page(), ensure_ascii=False)
    plugin.get_form()
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    assert plugin.preview_search_status(config)["status"] == "cached"
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    assert host.search_calls == []

    plugin._forget_search_cache(lambda _key: True)
    assert choice in json.dumps(plugin.get_page(), ensure_ascii=False)
    plugin.get_form()
    assert plugin.preview_search_status(config)["status"] == "cache_miss"
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    assert len(host.search_calls) == 2


def test_default_site_change_only_searches_new_sites_and_persists_coverage(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    new_site = copy.deepcopy(chosen)
    new_site.torrent_info.site = 11
    new_site.torrent_info.site_name = "新增站点"
    host.search_results.append(new_site)
    host.indexers.append({"id": 11, "name": "新增站点", "is_active": True})
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    status = plugin.preview_search_status(config)
    assert status["status"] == "partial_cache"
    assert status["missing_sites"] == [{"id": 11, "name": "新增站点"}]
    assert {item["id"] for item in status["reused_sites"]} == {5, 7, 9}
    host.search_calls.clear()
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    assert [call["sites"] for call in host.search_calls] == [[11], [11]]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert preview["search_trigger"] == "partial_cache"
    assert preview["search_searched_sites"] == [11]
    assert preview["search_reused_sites"] == [5, 7, 9]
    assert preview["matched_count"] == 2
    assert preview["search_keyword_verified"] is True
    assert any("缺失" in message and "新增站点" in message for _level, message in host.logs)

    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted.preview_search_status(config)["status"] == "cached"
    host.indexers.append({"id": 13, "name": "再新增站", "is_active": True})
    assert restarted.preview_search_status(config)["missing_sites"] == [{"id": 13, "name": "再新增站"}]


def test_all_sites_follows_host_default_site_selection(plugin_host):
    host = plugin_host
    host.system_values["IndexerSites"] = [5]
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    assert plugin.preview_search_status(config)["status"] == "cached"
    host.system_values["IndexerSites"] = [5, 7]
    status = plugin.preview_search_status(config)
    assert status["status"] == "partial_cache"
    assert status["missing_sites"] == [{"id": 7, "name": "另一个站"}]
    assert status["reused_sites"] == [{"id": 5, "name": "样例站"}]
    host.search_calls.clear()
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    assert [call["sites"] for call in host.search_calls] == [[7], [7]]


def test_legacy_default_cache_without_coverage_is_removed_and_researched(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    store = plugin._disk_search_cache()
    file = next(store.path.glob("*.json"))
    payload = json.loads(file.read_text())
    payload.pop("covered_sites")
    payload.pop("site_sources")
    file.write_text(json.dumps(payload))

    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    status = restarted.preview_search_status(config)
    assert status["status"] == "cache_miss"
    assert {site["id"] for site in status["missing_sites"]} == {5, 7, 9}
    assert not file.exists()
    assert any("旧默认范围缓存未记录实际覆盖站点" in message for _level, message in host.logs)


def test_all_site_preview_merges_single_site_results_and_search_evidence(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    host.system_values["IndexerSites"] = [5, 7]
    second = copy.deepcopy(chosen)
    second.torrent_info.site = 7
    second.torrent_info.site_name = "另一个站"
    first_id = host.module.preview_resource(chosen, 1, host.subscriptions[2].total_episode,
                                            host.module._utc_now())["id"]
    second_id = host.module.preview_resource(second, 1, host.subscriptions[2].total_episode,
                                             host.module._utc_now())["id"]
    settings = {"checked": True, "keyword": None, "search_imdbid": False,
                "custom_words": None, "site_failures": []}
    plugin._forget_search_cache(lambda _key: True)
    for site_id, context, resource_id in ((5, chosen, first_id), (7, second, second_id)):
        diagnostics = {
            "resource_keywords": {resource_id: ["Show S01"]}, "site_failures": [],
            "original_search": {**settings, "resource_ids": [resource_id]},
        }
        plugin._remember_search(host.subscriptions[2], [context], [site_id], "单站预览", "搜索时间",
                                diagnostics)
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    assert plugin.preview_search_status(config)["status"] == "cached"
    host.search_calls.clear()
    assert plugin.preview_candidate(choice, rule_mode="all_sites")[0]
    preview = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert host.search_calls == []
    assert preview["matched_count"] == 2
    assert preview["search_keyword_verified"] is True
    assert preview["search_reused_sites"] == [5, 7]
    assert preview["search_searched_sites"] == []


def test_candidate_labels_do_not_claim_unrecognized_or_sparse_episodes_are_complete(plugin_host):
    """标题整季词和已识别集号分别展示，跳号不能渲染成连续覆盖。"""
    module = plugin_host.module
    candidate = {"kind": "suspected_full", "episodes": [], "total_episode": 8}
    assert module._candidate_label(candidate) == "疑似整季（集数待核实）"
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
    rejected.torrent_info.seeders = 4
    rejected.torrent_info.volume_factor = "50%"
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
    assert preview["rejected"][0]["seeders"] == 4
    assert preview["rejected"][0]["promotion"] == "50%"
    assert "secret.example" not in json.dumps(preview)
    assert host.subscriptions[2].state == "S"
    assert host.subscriptions[2].quality is None
    page_text = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "规则预览" not in page_text
    assert "符合当前规则" not in page_text
    preview_table = plugin.get_form()[1]["_candidate_preview"]["table"]
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
    assert host.module._full_season_version_count(
        host.storage["gate_state"]["entries"]["2"]["preview"]["matched"]
    ) == 2
    assert "规则匹配多个集数覆盖全集" not in json.dumps(plugin.get_page(), ensure_ascii=False)

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
    assert plugin.get_form()[1]["_replacement_preview"]["table"]["component"] == "VTable"
    assert "回刷预览于" not in json.dumps(plugin.get_page(), ensure_ascii=False)
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
    assert host.search_calls[2]["sites"] == [7, 9]
    assert host.storage["gate_state"]["entries"]["2"]["preview"]["search_cached"] is False
    clock_value[0] += 3
    plugin.init_plugin({**host.storage["config"], "operation": "preview"})
    cached = host.storage["gate_state"]["entries"]["2"]["preview"]
    assert len(host.search_calls) == 4
    assert cached["search_cached"] is True
    assert cached["search_age_seconds"] == 6
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
    assert "确认时的规则预览（历史记录）" not in json.dumps(plugin.get_page(), ensure_ascii=False)
    assert next(service for service in plugin.get_service() if service["id"] == "candidate_refresh")["kwargs"] == {"hours": 4}


def test_manual_scan_then_preview_shows_one_row_per_site_torrent_link(plugin_host, monkeypatch):
    """带季和原订阅搜索召回同一链接时，只展示一个候选和一条预览结果。"""
    host = plugin_host
    plugin, choice, focused = _prepare_directory_confirmation(host)
    focused.torrent_info.page_url = "https://tracker.example/details.php?id=123"
    original = copy.deepcopy(focused)
    original.torrent_info.description = "原搜索返回的不同描述"
    original.torrent_info.enclosure = "https://secret.example/another-download-token"

    def process(_chain, **kwargs):
        result = [focused] if kwargs["keyword"] else [original]
        return kwargs["candidate_filter"](result)

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    assert plugin.scan_subscription("2")[0]
    entry = plugin._state["entries"]["2"]
    assert len(entry["candidates"]) == 1
    assert plugin.preview_candidate(choice)[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["search_cached"] is True
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 0
    assert preview["search_keyword_changed"] is False


@pytest.mark.parametrize("description,labels", [
    ("", ["完结"]),
    ("第二季 全5集", []),
    ("第二季 全5集", ["完结", "中字"]),
])
def test_scan_merges_pack_evidence_from_duplicate_search_results(plugin_host, monkeypatch, description, labels):
    """同链接后返回的合集线索进入一个候选和缓存预览，原搜索结果保持原样。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.season = 2
    subscribe.total_episode = subscribe.lack_episode = 5
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    focused = _season_only_context("Show S02 1080p WEB-DL", labels=["中字"])
    focused.torrent_info.page_url = "https://tracker.example/details.php?id=123"
    original = copy.deepcopy(focused)
    original.torrent_info.description = description
    original.torrent_info.labels = labels
    original.torrent_info.enclosure = "https://secret.example/another-download-token"
    initial = copy.deepcopy([focused, original])

    def process(_chain, **kwargs):
        return kwargs["candidate_filter"]([focused] if kwargs["keyword"] else [original])

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    assert plugin.scan_subscription("2")[0]
    candidates = plugin._state["entries"]["2"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "suspected_full"
    assert candidates[0]["episodes"] == []
    cached = next(iter(plugin._preview_search_cache.values()))["contexts"]
    assert len(cached) == 1
    assert "中字" in cached[0].torrent_info.labels
    assert description in (cached[0].torrent_info.description or "")
    assert set(labels).issubset(cached[0].torrent_info.labels)
    choice = _form_items(plugin, "candidate")[0]["value"]
    host.search_calls.clear()
    assert plugin.preview_candidate(choice)[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert host.search_calls == []
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["kind"] == "suspected_full"
    assert preview["search_keyword_changed"] is False
    # 提交下载前的资源唯一性检查仍能看到同链接的不同原始记录。
    assert len(plugin._search_unfiltered(subscribe, preserve_duplicates=True)) == 2
    assert [focused, original] == initial


def test_duplicate_metadata_merge_preserves_episode_evidence_and_distinct_torrents(plugin_host):
    """补充文本与标签不能拼接集号伪造全集，不同站点或详情链接不混合。"""
    module = plugin_host.module
    primary = _context("Show.S01.1080p.WEB-DL", episodes=(1, 3),
                       page_url="https://tracker.example/details.php?id=123")
    primary.torrent_info.description = "原副标题"
    primary.torrent_info.labels = ["中字"]
    supplemental = copy.deepcopy(primary)
    supplemental.torrent_info.description = "原副标题\n全5集"
    supplemental.torrent_info.labels = ["中字", "完结", "完结"]
    supplemental.meta_info.episode_list = [2, 4, 5]
    other_site = copy.deepcopy(primary)
    other_site.torrent_info.site = 7
    other_torrent = copy.deepcopy(primary)
    other_torrent.torrent_info.page_url = "https://tracker.example/details.php?id=456"
    initial = copy.deepcopy([primary, supplemental, other_site, other_torrent])

    merged = module._distinct_contexts([primary, supplemental, supplemental, other_site, other_torrent])
    assert len(merged) == 3
    assert merged[0].torrent_info.description == "原副标题\n全5集"
    assert merged[0].torrent_info.labels == ["中字", "完结"]
    assert merged[0].meta_info.episode_list == [1, 3]
    assert module.resource_candidate(merged[0], 1, "now", 5)["kind"] == "suspected_full"
    assert merged[1].torrent_info.labels == merged[2].torrent_info.labels == ["中字"]
    assert [primary, supplemental, other_site, other_torrent] == initial
    again = module._distinct_contexts([merged[0], supplemental])
    assert again[0].torrent_info.description == merged[0].torrent_info.description
    assert again[0].torrent_info.labels == merged[0].torrent_info.labels


def test_legacy_cache_merge_preserves_selected_pack_and_keyword_provenance_after_restart(plugin_host):
    """旧缓存合并副标题后保留所选标题及原搜索命中映射，不误切换搜索方式。"""
    host = plugin_host
    host.subscriptions[1] = _subscribe(1)
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscribe(2)
    subscribe.season = 2
    subscribe.total_episode = subscribe.lack_episode = 5
    host.subscriptions[2] = subscribe
    plugin.on_subscribe_added(SimpleNamespace(event_data={"subscribe_id": 2}))
    selected = _season_only_context("Show S02 1080p WEB-DL", labels=["完结"])
    selected.torrent_info.page_url = "https://tracker.example/details.php?id=123"
    host.search_results.append(selected)
    assert plugin.scan_subscription("2")[0]
    choice = _form_items(plugin, "candidate")[0]["value"]
    selected.torrent_info.labels = []
    other = copy.deepcopy(selected)
    other.torrent_info.title += " Alternate"
    other.torrent_info.description = "第二季 全5集"
    other_id = host.module.preview_resource(other, 2, 5, "now")["id"]
    diagnostics = next(iter(plugin._preview_search_cache.values()))["diagnostics"]
    diagnostics["original_search"]["resource_ids"] = [other_id]
    plugin._remember_search(subscribe, [other, selected], [], "旧扫描缓存", "扫描时间", diagnostics)
    plugin.stop_service()
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    host.search_calls.clear()

    assert restarted.preview_candidate(choice)[0]
    preview = restarted._state["entries"]["2"]["preview"]
    assert host.search_calls == []
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["id"] == choice.split(":", 1)[1]
    assert preview["matched"][0]["title"] == selected.torrent_info.title
    assert preview["matched"][0]["kind"] == "suspected_full"
    assert preview["matched"][0]["episodes"] == []
    assert preview["search_keyword_changed"] is False
    assert preview["search_keyword_verified"] is True


def test_manual_scan_replaces_old_candidate_when_same_link_title_changes(plugin_host):
    """站点更新标题后，重新扫描不会把同一详情页保留成两个候选。"""
    host = plugin_host
    plugin, _choice, original = _prepare_directory_confirmation(host)
    link = "https://tracker.example/details.php?id=123"
    original.torrent_info.page_url = link
    assert plugin.scan_subscription("2")[0]
    first_seen = plugin._state["entries"]["2"]["candidates"][0]["first_seen"]

    renamed = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM.Renamed", page_url=link)
    host.search_results[:] = [renamed]
    assert plugin.scan_subscription("2")[0]
    candidates = plugin._state["entries"]["2"]["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["title"] == renamed.torrent_info.title
    assert candidates[0]["first_seen"] == first_seen


def test_preview_deduplicates_existing_cache_and_keeps_selected_candidate(plugin_host):
    """升级前缓存若保存同链接的不同解析，预览沿用所选候选并合并搜索证据。"""
    host = plugin_host
    plugin, choice, selected = _prepare_directory_confirmation(host)
    selected.torrent_info.page_url = "https://tracker.example/details.php?id=123"
    other = copy.deepcopy(selected)
    other.torrent_info.title += ".Alternate"
    other_id = host.module.preview_resource(other, 1, 8, host.module._utc_now())["id"]
    cached = next(iter(plugin._preview_search_cache.values()))
    cached["contexts"] = [other, selected]
    cached["diagnostics"]["original_search"]["resource_ids"] = [other_id]

    assert plugin.preview_candidate(choice)[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["matched"][0]["id"] == choice.split(":", 1)[1]
    assert preview["search_keyword_changed"] is False
    assert plugin.confirm_candidate(choice)[0]


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
    assert host.search_calls[-1]["sites"] == [5, 7, 9]
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


def test_search_cache_ignores_removed_duration_setting_and_is_bounded(plugin_host, monkeypatch):
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
    assert host.module.MAX_CACHE_ENTRIES == 256
    monkeypatch.setattr(host.module, "MAX_CACHE_ENTRIES", 4)
    for number in range(10):
        subscribe = _subscribe(number + 10)
        subscribe.media_id = str(number)
        plugin._remember_search(subscribe, [chosen], [], "候选扫描", "now")
    assert len(plugin._preview_search_cache) == 4
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
    all_status = plugin.preview_search_status({**config, "rule_mode": "all_sites"})
    assert all_status["status"] == "partial_cache"
    assert {site["id"] for site in all_status["missing_sites"]} == {7, 9}
    assert [site["id"] for site in all_status["reused_sites"]] == [5]
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
    assert "已自动选择" in json.dumps(plugin.get_form()[1]["_candidate_preview"], ensure_ascii=False)
    assert "已自动选择" not in json.dumps(plugin.get_page(), ensure_ascii=False)
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
    chain = host.module.CandidateSearchChain(focused_season=season)
    search = getattr(chain, method)
    # 模拟宿主把带季中文查询词替换为不带季的英文名。
    search(site=site, keyword="All Creatures Great and Small")
    search(site=site, keyword=f"All Creatures Great and Small S{season:02d}")
    pack = host.module.CandidateSearchChain()
    getattr(pack, method)(site=site, keyword="All Creatures Great and Small")
    assert calls == [f"All Creatures Great and Small S{season:02d}", "All Creatures Great and Small"]
    assert [report["status"] for report in chain.site_reports] == ["unknown", "reused_in_round"]


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
    assert preview["keyword_choice"] == ("seasonal" if change_keyword else "original")
    assert preview["keyword_choice_auto_changed"] is change_keyword
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
    if change_keyword:
        assert plugin.confirm_candidate(choice)[0] is False
    assert plugin.confirm_candidate(choice, keyword_choice=preview["keyword_choice"])[0]
    plugin._search_approved(2)
    assert submitted_search == [("Show S01" if change_keyword else "Custom Original Search", not change_keyword)]


def test_manual_seasonal_keyword_can_override_recalled_original_search(plugin_host):
    """用户可选本轮带季词，预览核对后才覆盖原订阅搜索方式。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    subscribe = host.subscriptions[2]
    subscribe.search_imdbid = True
    option = next(item for item in _form_items(plugin, "candidate") if item["value"] == choice)
    keyword = option["verified_keywords"][0]
    selection = "seasonal"
    assert {item["value"] for item in _form_items(plugin, "keyword_choice")} == {"original", "seasonal"}

    success, _message, draft = plugin.preview_candidate(choice, keyword_choice=selection)
    assert success
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["keyword_choice"] == selection
    assert preview["keyword_choice_auto_changed"] is False
    assert preview["search_keyword"] == keyword
    assert preview["search_keyword_changed"] is True
    assert preview["matched_count"] == 1
    assert getattr(subscribe, "keyword", None) is None and subscribe.search_imdbid is True
    assert plugin.confirm_candidate(choice, draft)[0] is False
    assert plugin.confirm_candidate(choice, draft, keyword_choice=selection)[0]
    assert subscribe.keyword == keyword and subscribe.search_imdbid is False


def test_preview_operation_saves_automatically_selected_seasonal_choice(plugin_host, monkeypatch):
    """操作入口把预览自动切换后的选项保存给界面，确认不再提交旧选项。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    subscribe = host.subscriptions[2]
    subscribe.keyword = "Original Only"

    def process(_self, **kwargs):
        result = [] if kwargs["keyword"] == "Original Only" else [chosen]
        return kwargs["candidate_filter"](result)

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    plugin.init_plugin({"enabled": True, "operation": "preview", "candidate": choice,
                        "keyword_choice": "original", "refresh_search": True})
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["keyword_choice"] == "seasonal"
    assert preview["keyword_choice_auto_changed"] is True
    assert host.storage["config"]["keyword_choice"] == "seasonal"
    assert subscribe.keyword == "Original Only"


@pytest.mark.parametrize("old_choice,new_choice", [
    ("auto", "original"), ("keyword:Show S01", "seasonal"),
])
def test_legacy_search_choice_is_migrated_to_one_of_two_options(plugin_host, old_choice, new_choice):
    plugin = plugin_host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True, "keyword_choice": old_choice})
    assert plugin_host.storage["config"]["keyword_choice"] == new_choice


def test_seasonal_keyword_must_have_recalled_selected_version(plugin_host):
    """带季词必须由当前站点范围的搜索实际召回版本。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    diagnostics = next(iter(plugin._preview_search_cache.values()))["diagnostics"]
    diagnostics["resource_keywords"] = {}
    success, message, _draft = plugin.preview_candidate(choice, keyword_choice="seasonal")
    assert success is False and "未在最新搜索中召回" in message
    success, message, _draft = plugin.preview_candidate(choice, keyword_choice="keyword:unverified search")
    assert success is False and "只能选择" in message
    assert getattr(host.subscriptions[2], "keyword", None) is None


def test_manual_keyword_preview_excludes_resources_only_found_by_other_queries(plugin_host, monkeypatch):
    """手动选词预览只计入该词召回的资源。"""
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    other = _context("Show.S01E01.1080p.WEB-DL.HDR-TEAM.Other")

    def process(_chain, **kwargs):
        result = [chosen] if kwargs["keyword"] else [chosen, other]
        return kwargs["candidate_filter"](result)

    monkeypatch.setattr(host.module.SearchChain, "process", process)
    plugin.refresh_candidates()
    assert plugin.preview_candidate(choice, keyword_choice="seasonal")[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["matched_count"] == 1
    assert preview["rejected_count"] == 1
    assert preview["rejected"][0]["reason"] == "未由确认后使用的搜索词召回"


def test_keep_original_keyword_choice_requires_original_search_to_recall_candidate(plugin_host):
    """显式保留原搜索方式时，须先证实它能找回所选版本。"""
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice, keyword_choice="original")[0]
    assert plugin._state["entries"]["2"]["preview"]["search_keyword_changed"] is False
    host.search_results.clear()
    assert plugin.scan_subscription("2")[0]
    assert plugin.preview_candidate(choice, keyword_choice="original")[0]
    preview = plugin._state["entries"]["2"]["preview"]
    assert preview["keyword_choice"] == "original"
    assert preview["matched_count"] == 0
    assert plugin.confirm_candidate(choice)[0] is False


@pytest.mark.parametrize("reason", ["timeout", "budget_wait", "missing_observation", "changed_original_keyword"])
def test_failed_or_unverified_original_search_does_not_auto_replace_subscription_keyword(plugin_host, reason):
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
    assert preview["matched_count"] == 0
    assert plugin.confirm_candidate(choice)[0] is False
    assert plugin.preview_candidate(choice, keyword_choice="seasonal")[0]
    seasonal_keyword = plugin._state["entries"]["2"]["preview"]["search_keyword"]
    assert plugin.confirm_candidate(choice, keyword_choice="seasonal")[0]
    assert host.subscriptions[2].keyword == seasonal_keyword


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


@pytest.mark.parametrize("outcome", ["timeout", "rate_limited"])
def test_failed_site_is_only_skipped_for_current_subscription_round(plugin_host, monkeypatch, outcome):
    from contextlib import contextmanager
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    host.storage["gate_state"] = {"baseline": 1, "entries": {},
                                  "site_holds": {"5": {"until": 9999999999, "reason": "旧共享冷却"}}}
    plugin.init_plugin({"enabled": True})
    observations, calls = [], []

    @contextmanager
    def capture():
        observation = SimpleNamespace(attempted=False, outcome="skipped", error=None)
        observations.append(observation)
        yield observation

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def site_search(_self, **kwargs):
        calls.append(kwargs)
        observations[-1].attempted = True
        observations[-1].outcome = outcome
        observations[-1].error = "HTTP 429 https://secret.example/?cookie=cookie-secret" if outcome == "rate_limited" else "超时"
        return []

    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents", site_search, raising=False)
    site = {"id": 5, "name": "天空"}
    failed_sites = {}
    chain = host.module.CandidateSearchChain(failed_sites=failed_sites)
    assert chain.search_site_torrents(site=site, keyword="Show S07") == []
    later_query = host.module.CandidateSearchChain(failed_sites=failed_sites)
    assert later_query.search_site_torrents(site=site, keyword="Show") == []
    assert later_query.search_site_torrents(site=site, keyword="Show S07", page=1) == []
    assert len(calls) == 1
    assert later_query.site_reports[-1]["status"] == "skipped_in_round"
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    another_subscription = host.module.CandidateSearchChain()
    assert another_subscription.search_site_torrents(site=site, keyword="Other Show") == []
    assert len(calls) == 2
    assert "site_holds" not in host.storage["gate_state"]
    assert "cookie-secret" not in json.dumps(failed_sites)
    assert "secret.example" not in json.dumps(failed_sites)


def test_host_access_deferral_only_stops_current_subscription_round(plugin_host, monkeypatch):
    from contextlib import contextmanager
    host = plugin_host
    observations, calls = [], []

    @contextmanager
    def capture():
        observation = SimpleNamespace(attempted=False, outcome="skipped", error=None)
        observations.append(observation)
        yield observation

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def search(_self, **kwargs):
        calls.append(kwargs)
        observations[-1].outcome = "deferred"
        observations[-1].error = "站点已达到请求额度"
        return []

    monkeypatch.setattr(host.module.SearchChain, "_search_site_torrents_with_budget", search, raising=False)
    failed_sites = {}
    site = {"id": 5, "name": "天空"}
    chain = host.module.CandidateSearchChain(failed_sites=failed_sites)
    assert chain._search_site_torrents_with_budget(site=site, keyword="Show S07") == []
    assert chain._search_site_torrents_with_budget(site=site, keyword="Show") == []
    assert len(calls) == 1 and chain.site_reports[0]["attempted"] is False
    assert host.module.CandidateSearchChain()._search_site_torrents_with_budget(site=site, keyword="Other") == []
    assert len(calls) == 2


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
    chain = host.module.CandidateSearchChain()
    with capture() as outer:
        assert chain.search_site_torrents(site={"id": 5, "name": "天空"}, keyword="Show S07") == []
    assert outer.attempted is True
    assert outer.outcome == "timeout" and outer.error == "请求超时"
    assert outer.raw_count == 7 and outer.has_more is False


@pytest.mark.parametrize("outcome", ["success", "timeout", "deferred"])
def test_candidate_native_page_path_uses_real_permit_once_and_preserves_outcome(plugin_host, monkeypatch, outcome):
    from contextlib import contextmanager
    from contextvars import ContextVar
    host = plugin_host
    current = ContextVar("native_page_observation", default=None)
    calls = []

    @contextmanager
    def capture():
        observation = SimpleNamespace(attempted=False, outcome="skipped", error=None, raw_count=None, has_more=None)
        token = current.set(observation)
        try:
            yield observation
        finally:
            current.reset(token)

    def report(**kwargs):
        if current.get() is not None:
            current.get().__dict__.update(kwargs)

    observation_module = ModuleType("app.application.site.observation")
    observation_module.capture_site_search_observation = capture
    observation_module.report_site_search_outcome = report
    observation_module.report_site_search_page = report
    monkeypatch.setitem(sys.modules, observation_module.__name__, observation_module)
    exceptions = ModuleType("app.schemas.exception")
    exceptions.RateLimitExceededException = type("RateLimitExceededException", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)

    def permit(site):
        calls.append(("permit", site["id"]))
        if outcome == "deferred":
            raise exceptions.RateLimitExceededException("站点明确限制访问")

    throttle = ModuleType("app.application.site.throttle")
    throttle.wait_for_site_request = permit
    monkeypatch.setitem(sys.modules, throttle.__name__, throttle)

    def request(_self, **kwargs):
        calls.append(("request", kwargs["keyword"]))
        report(attempted=True, outcome=outcome, error="超时" if outcome == "timeout" else None)
        report(raw_count=1, has_more=False)
        return ["resource"] if outcome == "success" else []

    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents", request, raising=False)
    monkeypatch.setattr(host.module.SearchChain, "_search_site_torrents_with_budget",
                        lambda *_args, **_kwargs: pytest.fail("不应进入共享预算或嵌套活动观察"), raising=False)
    site = {"id": 5, "name": "天空"}
    chain = host.module.CandidateSearchChain()
    with capture() as outer:
        result = chain._search_site_torrents_with_budget(site=site, keyword="Show S07")
    assert result == (["resource"] if outcome == "success" else [])
    assert outer.outcome == outcome and outer.attempted is (outcome != "deferred")
    assert chain.site_reports[0]["status"] == outcome
    if outcome != "deferred":
        assert outer.raw_count == 1 and outer.has_more is False
    chain._search_site_torrents_with_budget(site=site, keyword="Show S07")
    assert calls == ([("permit", 5)] if outcome == "deferred"
                     else [("permit", 5), ("request", "Show S07")])
    host.module.CandidateSearchChain()._search_site_torrents_with_budget(site=site, keyword="Other")
    assert sum(call[0] == "permit" for call in calls) == 2


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


def _install_pacing_clock(host, monkeypatch):
    adapter = sys.modules[host.module.CandidateSearchChain.__module__]
    now = [1000.0]
    sleeps = []
    monkeypatch.setattr(adapter.time, "time", lambda: now[0])
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(adapter.random, "uniform", lambda lower, upper: (lower + upper) / 2)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(adapter.time, "sleep", sleep)
    return SimpleNamespace(adapter=adapter, now=now, sleeps=sleeps)


@pytest.mark.parametrize("site_interval,expected", [(0, 15), (45, 45)])
def test_pacing_preserves_request_start_interval_across_subscription_rounds(plugin_host, monkeypatch, site_interval, expected):
    contract = _install_pacing_clock(plugin_host, monkeypatch)
    pacer = contract.adapter.CandidateRequestPacer()
    site = {"id": 5, "name": "天空", "limit_seconds": site_interval}
    token, waited = pacer.acquire(site, lambda: False)
    assert waited == 0
    contract.now[0] += 3
    pacer.release(site, token)
    token, waited = pacer.acquire(site, lambda: False)
    assert contract.now[0] == 1000 + expected and waited == expected - 3
    pacer.release(site, token)
    token, waited = pacer.acquire({"id": 7}, lambda: False)
    assert waited == 0
    pacer.release({"id": 7}, token)


def test_pacing_wait_can_be_cancelled_without_a_network_request(plugin_host, monkeypatch):
    contract = _install_pacing_clock(plugin_host, monkeypatch)
    pacer = contract.adapter.CandidateRequestPacer()
    site = {"id": 5}
    token, _waited = pacer.acquire(site, lambda: False)
    pacer.release(site, token)
    with pytest.raises(RuntimeError, match="插件搜索已停止"):
        pacer.acquire(site, lambda: contract.now[0] >= 1002)
    assert contract.now[0] == 1002


def test_long_pacing_wait_only_defers_that_site_in_current_round(plugin_host, monkeypatch):
    host = plugin_host
    contract = _install_pacing_clock(host, monkeypatch)
    pacer = contract.adapter.CandidateRequestPacer()
    site = {"id": 5, "name": "天空", "limit_seconds": 120}
    token, _waited = pacer.acquire(site, lambda: False)
    pacer.release(site, token)
    calls = []
    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents",
                        lambda _self, **kwargs: calls.append(kwargs) or ["resource"], raising=False)
    chain = host.module.CandidateSearchChain(pacer=pacer)
    assert chain.search_site_torrents(site=site, keyword="Show") == []
    assert chain.search_site_torrents(site=site, keyword="Other alias") == []
    assert chain.search_site_torrents(site={"id": 7}, keyword="Show") == ["resource"]
    assert len(calls) == 1 and chain.site_reports[0]["status"] == "deferred"
    contract.now[0] = 1120
    assert host.module.CandidateSearchChain(pacer=pacer).search_site_torrents(site=site, keyword="Another subscription") == ["resource"]
    assert len(calls) == 2


def test_active_request_wait_is_bounded_and_does_not_invent_a_retry_time(plugin_host, monkeypatch):
    contract = _install_pacing_clock(plugin_host, monkeypatch)
    pacer = contract.adapter.CandidateRequestPacer()
    site = {"id": 5}
    token, _waited = pacer.acquire(site, lambda: False)
    with pytest.raises(contract.adapter.RequestDeferred) as error:
        pacer.acquire(site, lambda: False)
    assert contract.now[0] == 1060 and error.value.until is None
    pacer.release(site, token)
    token, waited = pacer.acquire(site, lambda: False)
    assert waited == 0
    pacer.release(site, token)


def test_same_actual_query_is_reused_in_round_but_other_pages_and_subscriptions_search(plugin_host, monkeypatch):
    host = plugin_host
    calls = []
    monkeypatch.setattr(host.module.SearchChain, "search_site_torrents",
                        lambda _self, **kwargs: calls.append(kwargs) or [f"page-{kwargs['page']}"], raising=False)
    cache = {}
    site = {"id": 5}
    first = host.module.CandidateSearchChain(focused_season=7, request_cache=cache)
    second = host.module.CandidateSearchChain(focused_season=7, request_cache=cache)
    assert first.search_site_torrents(site=site, keyword="Show S07") == ["page-0"]
    assert second.search_site_torrents(site=site, keyword="Show S07") == ["page-0"]
    assert len(calls) == 1 and second.site_reports[-1]["status"] == "reused_in_round"
    assert second.search_site_torrents(site=site, keyword="Show S07", page=1) == ["page-1"]
    assert host.module.CandidateSearchChain().search_site_torrents(site=site, keyword="Show S07") == ["page-0"]
    assert len(calls) == 3


def test_independent_search_does_not_configure_host_failure_budget_and_counts_unique_failures(plugin_host, monkeypatch):
    from contextlib import contextmanager
    host = plugin_host
    plugin = host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    calls, observations = [], []
    monkeypatch.setattr(host.module.MediaChain, "recognize_media", lambda _self, **kwargs: SimpleNamespace(
        title="剧集", en_title="Show", original_title="Show", names=[], season=None,
        media_source=kwargs["media_source"], media_id=kwargs["media_id"], type=kwargs["mtype"],
    ))
    monkeypatch.setattr(host.module.SearchChain, "configure_subscription_site_budget",
                        lambda *_args: pytest.fail("不应接入系统共享失败预算"), raising=False)

    @contextmanager
    def capture():
        observation = SimpleNamespace(attempted=False, outcome="skipped", error=None)
        observations.append(observation)
        yield observation

    module = ModuleType("app.application.site.observation")
    module.capture_site_search_observation = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def request(_chain, **kwargs):
        assert _chain._subscription_site_budget is None
        calls.append(kwargs)
        observations[-1].attempted = True
        observations[-1].outcome = "timeout" if len(calls) == 1 else "success"
        observations[-1].error = "超时" if len(calls) == 1 else None
        return [] if len(calls) == 1 else [_context("Show.S01E01.1080p.WEB-DL.HDR-TEAM")]

    def process(chain, **kwargs):
        contexts = chain._search_site_torrents_with_budget(site={"id": 5, "name": "天空"},
                                                          keyword=kwargs.get("keyword") or "剧集")
        return kwargs["candidate_filter"](contexts)

    monkeypatch.setattr(host.module.SearchChain, "_search_site_torrents_with_budget", request, raising=False)
    monkeypatch.setattr(host.module.SearchChain, "process", process)
    first_diagnostics, second_diagnostics = {}, {}
    assert plugin._search_unfiltered(_subscribe(2), diagnostics=first_diagnostics) == []
    assert len(calls) == 1 and len(first_diagnostics["site_failures"]) == 1
    assert sum(item["request_count"] for item in first_diagnostics["rounds"]) == 1
    assert plugin._search_unfiltered(_subscribe(3), diagnostics=second_diagnostics)
    assert len(calls) == 3 and second_diagnostics["site_failures"] == []


def test_stop_event_stays_cancelled_after_plugin_is_enabled_again(plugin_host):
    plugin = plugin_host.module.SubscribeOverwatcher()
    plugin.init_plugin({"enabled": True})
    previous = plugin._search_stop
    plugin.stop_service()
    plugin.init_plugin({"enabled": True})
    assert previous.is_set() and not plugin._search_stop.is_set()
    assert plugin._search_stop is not previous


def test_persistent_cache_survives_stop_and_restart_with_keyword_provenance_and_discrete_episodes(
    plugin_host, monkeypatch,
):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    host.directories.append(_directory("原目录", "/old/downloads"))
    chosen.meta_info.episode_list = [1, 3]
    chosen.torrent_info.seeders = 12
    chosen.torrent_info.downloadvolumefactor = 0
    chosen.torrent_info.uploadvolumefactor = 2
    cached = next(iter(plugin._preview_search_cache.values()))
    cached["diagnostics"]["original_search"]["resource_ids"] = []
    clock = [1000.0, 2000.0]
    monkeypatch.setattr(host.module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host.module.time, "time", lambda: clock[1])
    plugin._remember_search(host.subscriptions[2], [chosen], [], "候选扫描", "扫描时间",
                            cached["diagnostics"])
    plugin.stop_service()
    assert plugin._preview_search_cache == {}
    files = list(plugin.get_data_path().joinpath("search-cache").glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "cookie-secret" not in text and "secret.example/torrent" not in text
    assert "cookie-secret" not in json.dumps(host.storage)
    clock[:] = [5.0, 2000.0 + 90 * 24 * 3600]
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    host.search_calls.clear()
    assert restarted.preview_search_status({"operation": "preview", "candidate": choice,
                                             "rule_mode": "all_sites"})["status"] == "cached"
    assert restarted.preview_candidate(choice, rule_mode="all_sites")[0]
    assert host.search_calls == []
    preview = restarted._state["entries"]["2"]["preview"]
    assert preview["search_source"] == "候选扫描"
    assert preview["search_age_seconds"] == 90 * 24 * 3600
    assert preview["matched"][0]["episodes"] == [1, 3]
    assert preview["matched"][0]["id"] == choice.split(":", 1)[1]
    assert preview["search_keyword_changed"] is True
    assert preview["matched"][0]["download_path"].endswith("电视剧/国产剧")
    assert preview["matched"][0]["seeders"] == 12
    assert preview["matched"][0]["promotion"] == "免费 · 上传 2×"


def test_reloaded_persistent_cache_rechecks_current_rules(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    plugin.stop_service()
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    host.search_calls.clear()
    assert restarted.preview_candidate(choice, '{"include":"OTHER_TEAM"}')[0]
    preview = restarted._state["entries"]["2"]["preview"]
    assert preview["search_cached"] and preview["matched_count"] == 0 and preview["rejected_count"] == 1
    assert host.search_calls == []


def test_empty_and_single_site_caches_restore_without_becoming_all_site_cache(plugin_host):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    plugin._forget_search_cache(lambda _key: True)
    plugin._remember_search(host.subscriptions[2], [], [5], "规则预览搜索", "空搜索")
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    host.search_calls.clear()
    config = {"operation": "preview", "candidate": choice}
    assert restarted.preview_search_status(config)["status"] == "cached_empty"
    all_status = restarted.preview_search_status({**config, "rule_mode": "all_sites"})
    assert all_status["status"] == "partial_cache"
    assert {site["id"] for site in all_status["missing_sites"]} == {7, 9}
    assert restarted.preview_candidate(choice)[0]
    assert host.search_calls == []
    assert restarted._state["entries"]["2"]["preview"]["search_result_count"] == 0


@pytest.mark.parametrize("damage", ["invalid_json", "unsupported_schema", "bad_context", "deep_json"])
def test_corrupt_persistent_cache_is_removed_and_does_not_look_like_successful_empty_search(plugin_host, damage):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)
    file = next(plugin.get_data_path().joinpath("search-cache").glob("*.json"))
    data = json.loads(file.read_text())
    if damage == "unsupported_schema":
        data["schema"] = 999
    elif damage == "bad_context":
        data["contexts"] = [{}]
    file.write_text("broken" if damage == "invalid_json" else
                    "[" * 2000 + "0" + "]" * 2000 if damage == "deep_json" else json.dumps(data))
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert not file.exists()
    assert any("无法恢复或订阅身份已变化" in message for _level, message in host.logs)
    config = {"operation": "preview", "candidate": choice, "rule_mode": "all_sites"}
    assert restarted.preview_search_status(config)["status"] == "cache_miss"
    host.search_calls.clear()
    assert restarted.preview_candidate(choice, rule_mode="all_sites")[0]
    assert len(host.search_calls) == 2


def test_approval_and_disabling_remove_persistent_cache_and_reload_cannot_restore_it(plugin_host):
    host = plugin_host
    plugin, choice, chosen = _prepare_directory_confirmation(host)
    assert plugin.preview_candidate(choice)[0]
    assert plugin.confirm_candidate(choice)[0]
    folder = plugin.get_data_path() / "search-cache"
    assert list(folder.glob("*.json")) == []
    assert any("订阅批准" in message for _level, message in host.logs)
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted._preview_search_cache == {}
    restarted._remember_search(host.subscriptions[2], [chosen], [], "回刷预览", "回刷时间")
    assert list(folder.glob("*.json"))
    restarted.init_plugin({"enabled": False})
    assert not list(folder.glob("*.json"))
    assert any("插件停用" in message and "缓存清理" in message for _level, message in host.logs)


def test_deleted_or_changed_subscription_cache_is_removed_from_disk_at_scan_or_startup(plugin_host):
    host = plugin_host
    plugin, _choice, chosen = _prepare_directory_confirmation(host)
    folder = plugin.get_data_path() / "search-cache"
    del host.subscriptions[2]
    plugin.refresh_candidates()
    assert list(folder.glob("*.json")) == []
    plugin._remember_search(host.subscriptions[1], [chosen], [], "旧身份搜索", "旧时间")
    host.subscriptions[1].media_id = "changed"
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted._preview_search_cache == {} and list(folder.glob("*.json")) == []


def test_cache_entry_and_disk_byte_limits_remove_oldest_results_from_memory_and_disk(plugin_host):
    import os
    host = plugin_host
    plugin, _choice, chosen = _prepare_directory_confirmation(host)
    store = plugin._disk_search_cache()
    store.max_entries = 2
    for number in (3, 4, 5):
        host.subscriptions[number] = _subscribe(number)
        plugin._remember_search(host.subscriptions[number], [chosen], [], "搜索", "时间")
    expected = {host.module._identity(host.subscriptions[number]) for number in (4, 5)}
    assert {key[0] for key in plugin._preview_search_cache} == expected
    assert len(list(store.path.glob("*.json"))) == 2
    assert any("超过 2 组上限" in message for _level, message in host.logs)
    store.max_entries = 64
    store.max_bytes = sum(file.stat().st_size for file in store.path.glob("*.json")) + 100
    # 宿主时钟回调后，较早文件可能仍带未来的 mtime；当前新搜索必须保留。
    for rank, key in enumerate(sorted(plugin._preview_search_cache,
                                     key=lambda item: plugin._preview_search_cache[item]["at"])):
        stamp = host.module.time.time() + 86400 + rank
        os.utime(store.path / store.filename(key), (stamp, stamp))
    host.subscriptions[6] = _subscribe(6)
    plugin._remember_search(host.subscriptions[6], [chosen], [], "搜索", "时间")
    assert len(plugin._preview_search_cache) == 2
    assert sum(file.stat().st_size for file in store.path.glob("*.json")) <= store.max_bytes
    assert {key[0] for key in plugin._preview_search_cache} == {
        host.module._identity(host.subscriptions[number]) for number in (5, 6)
    }
    assert any("超过" in message and "总容量" in message for _level, message in host.logs)


def test_failed_manual_refresh_keeps_old_resource_only_as_history_after_restart(plugin_host, monkeypatch):
    host = plugin_host
    plugin, choice, _chosen = _prepare_directory_confirmation(host)

    def fail(*_args, **_kwargs):
        raise RuntimeError("search failed")

    monkeypatch.setattr(plugin, "_search_unfiltered", fail)
    assert plugin.preview_candidate(choice, refresh_search=True)[0] is False
    selected = plugin._cache_parts(host.subscriptions[2], [5])[0][0]["entry"]
    assert selected["contexts"] == []
    assert any(entry["contexts"] for entry in plugin._preview_search_cache.values())
    cache_files = list(plugin.get_data_path().joinpath("search-cache").glob("*.json"))
    assert all("enclosure" not in file.read_text() for file in cache_files)
    restarted = host.module.SubscribeOverwatcher()
    restarted.init_plugin({"enabled": True})
    assert restarted.preview_search_status({"operation": "preview", "candidate": choice})["status"] == "cached_empty"
    assert restarted.preview_candidate(choice)[0] is True
    assert restarted._state["entries"]["2"]["preview"]["historical_candidates"]


def test_disk_write_failure_keeps_new_memory_result_without_restoring_old_disk_result(plugin_host, monkeypatch):
    host = plugin_host
    plugin, _choice, _chosen = _prepare_directory_confirmation(host)
    cache_module = sys.modules[host.module.SearchCacheStore.__module__]

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(cache_module.os, "replace", fail)
    newest = _context("Newest S01E01 TEAM")
    plugin._remember_search(host.subscriptions[2], [newest], [], "新搜索", "新时间")
    assert next(iter(plugin._preview_search_cache.values()))["contexts"] == [newest]
    assert list(plugin.get_data_path().joinpath("search-cache").iterdir()) == []

"""
剧集守门员在下载模块入口识别部分合集，并通过现有 qB 客户端暂停添加。
"""

import secrets
from pathlib import Path
from typing import Any, Optional, Union

from app.application.torrent.download import TorrentHelper


def needs_manual_selection(
    content: Union[Path, str, bytes], episodes: set[int],
    custom_words: Optional[str], episode_selection: Any,
) -> bool:
    """
    比较目标集和种子内的视频文件；无文件清单的磁力任务按待确认处理。

    :param content: 已由下载链取得的种子内容或磁力链接
    :param episodes: 本次订阅需要的集数
    :param custom_words: 下载链使用的来源识别词
    :param episode_selection: 下载链确认的季集映射
    :return: 需要留给用户手动选择文件时为 True
    :raises ValueError: 种子内容存在但无法解析文件清单
    """
    from app.domain import torrent as torrent_rules
    from app.sdk.config import settings

    if isinstance(content, (str, bytes)) and torrent_rules.is_magnet_link(content):
        return True
    if not isinstance(content, bytes):
        return False
    _, names = TorrentHelper().get_fileinfo_from_torrent_content(content)
    if not names:
        raise ValueError("无法解析种子文件清单")
    video_extensions = {str(extension).lower() for extension in settings.RMT_MEDIAEXT}
    _, unwanted, matched = torrent_rules.select_torrent_files(
        enumerate(names), episodes, video_extensions, custom_words, episode_selection,
    )
    if not episodes.issubset(matched):
        return False
    return bool(matched - episodes) or any(
        index in unwanted and Path(name).suffix.lower() in video_extensions
        for index, name in enumerate(names)
    )


def _existing_hash(server: Any, expected_hashes: frozenset[str]) -> Optional[str]:
    """
    在添加前查找已有同 Hash 任务，避免暂停或重选正在运行的任务。

    :param server: qBittorrent 已配置客户端
    :param expected_hashes: 种子内容解析出的 v1/v2 身份集合
    :return: 已有任务的 Hash，未找到时为 None
    :raises RuntimeError: 下载器无法查询任务
    """
    torrents, error = server.get_torrents()
    if error:
        raise RuntimeError("无法查询 qB 现有任务")
    for torrent in torrents:
        hashes = {
            str(torrent.get(key) or "").lower()
            for key in ("hash", "infohash_v1", "infohash_v2")
        }
        if expected_hashes.intersection(hashes):
            return str(torrent.get("hash") or "") or None
    return None


def add_paused_qb_download(
    module: Any, server: Any, content: Union[str, bytes], download_dir: Path,
    cookie: str, category: Optional[str], label: Optional[str], downloader: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """
    通过宿主已配置的 qB 客户端添加暂停任务，不修改文件优先级或启动任务。

    :param module: 当前运行的 qB 模块，仅复用路径映射与默认实例名
    :param server: 由该模块取得的 qB 客户端
    :param content: 种子字节或磁力链接
    :param download_dir: 下载链选定的原保存目录
    :param cookie: 站点 Cookie
    :param category: 下载器分类
    :param label: 下载任务标签
    :param downloader: 下载器实例名称
    :return: 与宿主下载模块相同的结果元组
    :raises RuntimeError: 无法确认种子身份或查询已有任务
    """
    from app.domain import torrent as torrent_rules
    from app.schemas.download import DOWNLOAD_TASK_ALREADY_EXISTS
    from app.sdk.config import settings

    expected_hashes = torrent_rules.download_hashes(content)
    if not expected_hashes:
        raise RuntimeError("无法确认种子 Hash，未添加任务")
    actual_downloader = downloader or module.get_default_config_name()
    existing = _existing_hash(server, expected_hashes)
    if existing:
        return actual_downloader, existing, server.get_content_layout(), DOWNLOAD_TASK_ALREADY_EXISTS

    temporary_tag = f"sow-{secrets.token_hex(6)}"
    tags = [tag.strip() for tag in (label or "").split(",") if tag.strip()]
    default_tag = str(settings.TORRENT_TAG or "").strip()
    if default_tag and default_tag not in tags:
        tags.append(default_tag)
    tags.append(temporary_tag)
    torrent_hash = None
    try:
        added, identifiers = server.add_torrent(
            content=content,
            download_dir=module.normalize_path(download_dir, downloader),
            is_paused=True,
            tag=tags,
            cookie=cookie,
            category=category,
            ignore_category_check=False,
        )
        if not added:
            existing = _existing_hash(server, expected_hashes)
            if existing:
                return actual_downloader, existing, server.get_content_layout(), DOWNLOAD_TASK_ALREADY_EXISTS
            return None, None, None, "qB 未接受暂停种子任务"
        torrent_hash = next(iter(identifiers or []), None) or server.get_torrent_id_by_tag(tags=temporary_tag)
        if not torrent_hash:
            return None, None, None, "qB 已接受种子但任务 Hash 未确认，请手动检查暂停状态"
        if not server.stop_torrents(torrent_hash):
            return None, None, None, f"qB 任务 {torrent_hash} 暂停请求未确认，请手动检查"
        return (
            actual_downloader, torrent_hash, server.get_content_layout(),
            f"添加下载成功，任务 {torrent_hash} 保持暂停，请在 qB 手动选集后启动",
        )
    finally:
        server.delete_torrents_tag(torrent_hash, temporary_tag)

"""
提供下载根目录选项，并按宿主规则预览完整下载路径。
"""

from pathlib import Path
from typing import Any

from app.application.directory import DirectoryHelper, build_media_download_path, validate_download_save_path
from app.schemas.file import FileURI


def directory_root(directory: Any) -> str:
    """
    返回宿主允许写入的资源根目录 URI，保留存储类型。

    :param directory: 宿主目录配置
    :return: 通过下载路径白名单校验的根目录
    """
    uri = FileURI(storage=directory.storage or "local", path=directory.download_path).uri
    return validate_download_save_path(uri)


def directory_choices() -> list[dict[str, str]]:
    """
    从支持剧集的目录卡片中列出资源根目录，同一 URI 只列一次。

    :return: 以根目录 URI 为值、卡片名称和路径为标题的选项
    """
    choices = []
    for directory in DirectoryHelper().get_download_dirs():
        if directory.media_type not in (None, "", "电视剧", "tv"):
            continue
        try:
            root = directory_root(directory)
        except ValueError:
            continue
        if not any(item["value"] == root for item in choices):
            choices.append({"title": f"{directory.name or '未命名目录'} · {root}", "value": root})
    return choices


def selected_directory_root(value: str) -> str:
    """
    校验表单选择仍属于当前可选根目录，确认时只回填该根目录。

    :param value: 表单选择的资源根目录 URI
    :return: 当前配置中的根目录 URI
    :raises ValueError: 所选根目录已不在配置中
    """
    if not any(item["value"] == value for item in directory_choices()):
        raise ValueError("所选下载目录已不在当前配置中，请重新选择并预览")
    return value


def preview_download_path(media: Any, save_path: str | None = None) -> str:
    """
    沿用宿主的显式路径或默认目录选择规则，只计算预览，不修改下载参数。

    :param media: 本次下载的完整媒体及生效分类信息
    :param save_path: 所选根目录或订阅原保存目录；留空使用宿主默认目录
    :return: 包含生效分类子目录的完整保存 URI
    :raises ValueError: 缺少媒体信息、保存路径无效或没有可用默认目录
    """
    if media is None:
        raise ValueError("缺少媒体信息，无法预览下载目录")
    helper = DirectoryHelper()
    if str(save_path or "").strip():
        uri = FileURI.from_uri(validate_download_save_path(save_path))
        directory = helper.get_download_dir_by_save_path(media, uri.uri)
    else:
        directory = helper.get_download_dir(media)
        if directory is None:
            raise ValueError("未找到下载目录")
        uri = FileURI(storage=directory.storage or "local", path=directory.download_path)
    path = Path(uri.path)
    if directory:
        path = build_media_download_path(path, directory, media, helper)
    return FileURI(storage=uri.storage, path=path.as_posix()).uri

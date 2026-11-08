"""有容量上限的预览搜索缓存，保存资源事实而不保存下载凭据。"""

import json
import math
import os
import tempfile
import time
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.sdk.logging import logger

from .rules import public_page_url


MAX_CACHE_ENTRIES = 64
MAX_CACHE_BYTES = 64 * 1024 * 1024
_TORRENT_FIELDS = (
    "site", "site_name", "site_order", "title", "description", "media_source", "media_id",
    "size", "seeders", "peers", "grabs", "pubdate", "date_elapsed", "freedate",
    "uploadvolumefactor", "downloadvolumefactor", "hit_and_run", "labels", "pri_order", "category",
    "volume_factor", "freedate_diff",
)
_CONTEXT_FIELDS = ("resource_source", "match_source", "candidate_recognized", "media_info_is_target",
                   "match_status", "match_reason")
_MEDIA_OMIT = {"tmdb_info", "douban_info", "bangumi_info", "anilist_info", "source_data"}


def _values(value: Any) -> dict:
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    return vars(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _plain(_values(value))


def context_snapshot(context: Any) -> dict:
    """保留所有预览筛选事实、媒体分类和离散季集，下载地址在提交时重新获取。"""
    meta = getattr(context, "meta_info", None)
    torrent = getattr(context, "torrent_info", None)
    if meta is None or torrent is None:
        raise ValueError("搜索资源缺少识别信息")
    media = getattr(context, "media_info", None)
    return _plain({
        "meta_info": _values(meta), "seasons": list(getattr(meta, "season_list", None) or []),
        "media_info": {key: value for key, value in _values(media).items()
                       if key not in _MEDIA_OMIT} if media is not None else None,
        "torrent_info": {**{field: getattr(torrent, field, None) for field in _TORRENT_FIELDS},
                         "page_url": public_page_url(getattr(torrent, "page_url", None),
                                                     getattr(torrent, "enclosure", None))},
        **{field: getattr(context, field, None) for field in _CONTEXT_FIELDS},
    })


def restore_context(values: dict) -> Any:
    """恢复宿主的筛选与目录对象；不重新解析标题或访问站点。"""
    from app.schemas.context import MetaInfo
    try:
        from app.domain.context import Context, MediaInfo, TorrentInfo
    except ImportError:
        from app.core.context import Context, MediaInfo, TorrentInfo
    meta = MetaInfo(**values["meta_info"])
    meta.season_list = values["seasons"]
    torrent = TorrentInfo()
    torrent.from_dict(values["torrent_info"])
    media = None
    if values.get("media_info") is not None:
        media = MediaInfo()
        media.from_dict(values["media_info"])
    context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
    for field in _CONTEXT_FIELDS:
        setattr(context, field, values.get(field))
    return context


class SearchCacheStore:
    """每个身份和站点范围一个 JSON 文件；新搜索原子替换，按容量淘汰旧文件。"""

    def __init__(self, path: Path, *, max_entries: int = MAX_CACHE_ENTRIES,
                 max_bytes: int = MAX_CACHE_BYTES):
        self.path = Path(path)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for file in self.path.glob(".write-*"):
            if file.is_file() and file.stat().st_mtime < time.time() - 300:
                file.unlink(missing_ok=True)

    @staticmethod
    def filename(key: tuple[str, tuple[int, ...]]) -> str:
        return sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() + ".json"

    def _files(self) -> list[Path]:
        return [file for file in self.path.glob("*.json") if len(file.stem) == 64 and file.is_file()]

    def delete(self, key: tuple[str, tuple[int, ...]]) -> None:
        (self.path / self.filename(key)).unlink(missing_ok=True)

    def clear(self) -> None:
        for file in self._files():
            file.unlink(missing_ok=True)

    def prune(self, *, newest: str = "") -> set[str]:
        files = sorted(self._files(), key=lambda file: (file.name == newest, file.stat().st_mtime_ns, file.name),
                       reverse=True)
        total = 0
        removed = set()
        kept = 0
        for file in files:
            size = file.stat().st_size
            if kept >= self.max_entries or total + size > self.max_bytes:
                file.unlink(missing_ok=True)
                removed.add(file.name)
            else:
                kept += 1
                total += size
        return removed

    def save(self, key: tuple[str, tuple[int, ...]], entry: dict) -> set[str]:
        temporary = None
        try:
            payload = {"schema": 1, "identity": key[0], "sites": list(key[1]),
                       "searched_ts": entry["searched_ts"], "searched_at": entry["searched_at"],
                       "source": entry["source"], "sequence": entry["sequence"],
                       "diagnostics": entry.get("diagnostics"),
                       "contexts": [context_snapshot(context) for context in entry["contexts"]]}
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            if len(data) > self.max_bytes:
                self.delete(key)
                logger.warning("本轮搜索超过持久缓存容量上限，仅在内存中复用")
                return set()
            with tempfile.NamedTemporaryFile(dir=self.path, prefix=".write-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path / self.filename(key))
            return self.prune(newest=self.filename(key))
        except (OSError, TypeError, ValueError, AttributeError, RecursionError):
            # 不允许重启后恢复上一轮结果，尤其是用户明确要求重新搜索的时候。
            self.delete(key)
            logger.warning("搜索缓存无法写入磁盘，本轮仍可在内存中复用，详情请检查插件数据目录")
            return set()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, identities: set[str]) -> dict:
        restored = {}
        self.prune()
        for file in self._files():
            try:
                values = json.loads(file.read_text())
                identity = values["identity"]
                sites = values["sites"]
                stamp = values["searched_ts"]
                if values.get("schema") != 1 or identity not in identities \
                        or not isinstance(sites, list) or any(type(site) is not int or site <= 0 for site in sites) \
                        or type(stamp) not in (float, int) or not math.isfinite(stamp) \
                        or not isinstance(values["contexts"], list) or type(values["sequence"]) is not int:
                    raise ValueError("缓存格式或订阅身份已变化")
                key = (identity, tuple(sorted(set(sites))))
                if self.filename(key) != file.name:
                    raise ValueError("缓存索引不匹配")
                restored[key] = {
                    "at": time.monotonic() - max(0, time.time() - stamp), "searched_ts": stamp,
                    "searched_at": values["searched_at"], "source": values["source"],
                    "sequence": values["sequence"], "diagnostics": values.get("diagnostics"),
                    "contexts": [restore_context(context) for context in values["contexts"]],
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, RecursionError):
                file.unlink(missing_ok=True)
                logger.info("已移除无法恢复或订阅身份已变化的搜索缓存")
        return restored

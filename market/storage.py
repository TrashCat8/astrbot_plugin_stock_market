"""分组账本读写：每群一个 JSON，写入用临时文件 + os.replace 原子替换。

另外提供全局股票名称表（所有群共用）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GroupStore:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.groups_dir = self.base / "groups"
        self.cache_dir = self.base / "cache"
        self.groups_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, namespace: str) -> asyncio.Lock:
        """同一命名空间的账本操作串行化，不同群互不阻塞。"""
        lock = self._locks.get(namespace)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[namespace] = lock
        return lock

    def path(self, namespace: str) -> Path:
        safe = "".join(ch for ch in namespace if ch.isalnum() or ch in "_-") or "unknown"
        return self.groups_dir / f"{safe}.json"

    def load(self, namespace: str) -> dict[str, Any] | None:
        path = self.path(namespace)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict) or "stocks" not in data:
                raise ValueError("账本结构不正确")
            return data
        except (OSError, ValueError) as exc:
            backup = path.with_name(f"{path.stem}.broken.{int(time.time())}.json")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            logger.error("账本 %s 读取失败，已备份为 %s：%s", path, backup, exc)
            return None

    def save(self, namespace: str, state: dict[str, Any]) -> None:
        path = self.path(namespace)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(state, fp, ensure_ascii=False, separators=(",", ":"))
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def cache_file(self, name: str) -> Path:
        return self.cache_dir / name

    def cleanup_cache(self, keep: int = 80, max_age: float = 86400.0) -> None:
        """清掉过期的渲染图片，避免长期占用磁盘。"""
        now = time.time()
        files = sorted(
            self.cache_dir.glob("*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for index, file in enumerate(files):
            try:
                if index >= keep or now - file.stat().st_mtime > max_age:
                    file.unlink()
            except OSError:
                continue


class NameStore:
    """全局股票名称：所有群共用一份，改名后各群下次指令即生效。"""

    def __init__(self, base: Path) -> None:
        self.path = Path(base) / "names.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._cache: dict[str, str] | None = None
        self._stamp: tuple[int, int] | None = None

    def load(self) -> dict[str, str]:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache = {}
            self._stamp = None
            return {}

        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._stamp == stamp:
            return dict(self._cache)

        try:
            with self.path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            names = {
                str(key).upper(): str(value)
                for key, value in data.items()
                if isinstance(value, str)
            }
        except (OSError, ValueError, AttributeError) as exc:
            logger.error("股票名称表 %s 读取失败：%s", self.path, exc)
            names = {}

        self._cache = names
        self._stamp = stamp
        return dict(names)

    def apply(self, state: dict[str, Any]) -> None:
        """把全局名称覆盖到某个群的账本快照上。"""
        names = self.load()
        if not names:
            return
        for code, stock in state.get("stocks", {}).items():
            if code in names:
                stock["name"] = names[code]

    async def set_name(self, code: str, name: str) -> None:
        async with self._lock:
            names = self.load()
            names[code.upper()] = name
            self._write(names)
            self._cache = names
            try:
                stat = self.path.stat()
                self._stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self._stamp = None

    def _write(self, names: dict[str, str]) -> None:
        path = self.path
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(names, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

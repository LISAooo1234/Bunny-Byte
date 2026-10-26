"""输入框里的 `@path` 补全：列出工作区文件，按输入的片段排序。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ..core.workspace import IGNORED_PATH_NAMES

MAX_FILES = 20000
CACHE_SECONDS = 30.0


def mention_at_cursor(text: str, cursor: int) -> tuple[int, str] | None:
    """光标所在的词以 `@` 开头（且 `@` 在开头或空白后）时，返回 (`@` 的下标, `@` 后面的片段)。"""
    text = str(text or "")
    cursor = max(0, min(int(cursor), len(text)))
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    word = text[start:cursor]
    if not word.startswith("@") or word.startswith('@"'):
        return None
    return start, word[1:]


def _hidden(relative: str) -> bool:
    parts = relative.split("/")
    # 补全里不主动列出 .env 这类机密文件；手动写 @.env 仍然会预读（会发给模型）。
    return any(part in IGNORED_PATH_NAMES for part in parts) or parts[-1].startswith(".env")


def list_workspace_files(root) -> list[str]:
    """git 仓库用 `git ls-files`（含未跟踪但没被忽略的文件），否则遍历目录；目录以 `/` 结尾。"""
    root = Path(root)
    files: list[str] = []
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0:
            files = [item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item]
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        for current, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_PATH_NAMES and d != "node_modules")
            base = Path(current).relative_to(root).as_posix()
            for name in sorted(names):
                files.append(name if base == "." else f"{base}/{name}")
            if len(files) >= MAX_FILES:
                break
    files = [item for item in files[:MAX_FILES] if not _hidden(item)]
    directories = {item.rsplit("/", 1)[0] + "/" for item in files if "/" in item}
    for directory in list(directories):
        parts = directory.rstrip("/").split("/")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]) + "/")
    return sorted(directories) + sorted(files)


def match_files(files: list[str], query: str, limit: int = 8) -> list[str]:
    """排序：文件名前缀 < 路径前缀 < 文件名包含 < 路径包含；同级按路径短的优先。"""
    query = str(query or "").lower()
    ranked = []
    for path in files:
        lowered = path.lower()
        if lowered == query:
            continue
        name = lowered.rstrip("/").rsplit("/", 1)[-1]
        if not query:
            rank = 0 if "/" not in path.rstrip("/") else 1
        elif name.startswith(query):
            rank = 0
        elif lowered.startswith(query):
            rank = 1
        elif query in name:
            rank = 2
        elif query in lowered:
            rank = 3
        else:
            continue
        ranked.append((rank, len(path), path))
    ranked.sort()
    return [path for _rank, _length, path in ranked[:limit]]


def mention_token(path: str) -> str:
    return f'@"{path}"' if any(char.isspace() for char in path) else f"@{path}"


class FileIndex:
    """按需列文件并缓存 30 秒，避免每敲一个字都跑一次 git。"""

    def __init__(self) -> None:
        self.root: Path | None = None
        self._files: list[str] = []
        self._loaded_at = 0.0

    def set_root(self, root) -> None:
        root = Path(root) if root else None
        if root != self.root:
            self.root = root
            self._loaded_at = 0.0

    def files(self) -> list[str]:
        if self.root is None:
            return []
        if time.monotonic() - self._loaded_at > CACHE_SECONDS:
            self._files = list_workspace_files(self.root)
            self._loaded_at = time.monotonic()
        return self._files

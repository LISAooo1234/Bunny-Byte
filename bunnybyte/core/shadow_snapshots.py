"""Content-addressed workspace snapshots kept in a private git object store.

The store lives in ``.bunnybyte/shadow.git`` and is driven with GIT_DIR /
GIT_WORK_TREE, so the user's own repository (index, refs, config, hooks) is never
touched. A snapshot is a git tree id: ``git add -A`` through a private index only
re-hashes files whose stat data changed, so taking one costs roughly the number
of changed files instead of the size of the repository.

Known limits: files ignored by the workspace's .gitignore are not tracked (they
are neither diffed nor undone, except explicit write targets that callers force
in), and a workspace that contains nested git repositories disables the store so
callers fall back to hashing.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import IGNORED_PATH_NAMES

SNAPSHOT_SKIP_DIRS = ("node_modules", ".mypy_cache", ".tox", ".nox")
MAX_UNTRACKED_BYTES = 8_000_000
ADD_TIMEOUT_SECONDS = 180
GIT_TIMEOUT_SECONDS = 60
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SNAPSHOT_REF = "refs/bb/snapshots"
MAX_CONSECUTIVE_FAILURES = 3
BLOB_BATCH = 200
STALE_LOCK_SECONDS = 120
RETRY_COOLDOWN_SECONDS = 60
DISABLED_MARKER = "DISABLED"

_LOCK = threading.RLock()
_STORES: dict[str, "ShadowStore"] = {}


def _escape_glob(path):
    escaped = "".join("\\" + ch if ch in "*?[]\\" else ch for ch in path)
    if escaped.startswith(("#", "!")):
        escaped = "\\" + escaped
    stripped = escaped.rstrip(" ")
    return (stripped + "\\ " * (len(escaped) - len(stripped))).replace("\n", "")


class ShadowError(RuntimeError):
    """A git call failed or the store cannot be used."""


@dataclass
class ApplyResult:
    applied: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


class ShadowStore:
    @classmethod
    def for_root(cls, root):
        resolved = Path(root).resolve()
        key = str(resolved)
        with _LOCK:
            store = _STORES.get(key)
            if store is None:
                store = _STORES[key] = cls(resolved)
            return store

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.git_dir = self.root / ".bunnybyte" / "shadow.git"
        self.disabled_reason = ""
        self.skipped_large = []
        self._failures = 0
        self._retry_at = 0.0
        self._ready = False
        self._last_tree = ""
        self._last_snapshot_tree = ""
        if os.environ.get("BUNNYBYTE_SHADOW", "").strip().lower() in {"0", "off", "false", "no"}:
            self.disabled_reason = "disabled by BUNNYBYTE_SHADOW"

    @property
    def available(self):
        return not self.disabled_reason

    def disable(self, reason):
        self.disabled_reason = str(reason)

    def _env(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        home = self.git_dir / "_home"
        env.update(
            GIT_DIR=str(self.git_dir),
            GIT_WORK_TREE=str(self.root),
            GIT_INDEX_FILE=str(self.git_dir / "index"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            HOME=str(home),
            XDG_CONFIG_HOME=str(home),
            LC_ALL="C",
        )
        return env

    def _run(self, args, *, input_bytes=None, timeout=GIT_TIMEOUT_SECONDS, check=True):
        command = [
            "git",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "-c", "core.quotepath=off",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull,
            "-c", "gc.auto=0",
            "-c", "advice.addEmbeddedRepo=false",
            "-c", "safe.directory=*",
            *args,
        ]
        try:
            proc = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                env=self._env(),
                cwd=str(self.root),
                timeout=timeout,
                stdin=None if input_bytes is not None else subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            self.disable("git not found")
            raise ShadowError("git not found") from exc
        except subprocess.TimeoutExpired as exc:
            self.disable(f"git {args[0]} timed out after {timeout}s")
            raise ShadowError(self.disabled_reason) from exc
        if check and proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
            raise ShadowError(f"git {args[0]} failed ({proc.returncode}): {detail}")
        return proc

    def _ensure_ready(self):
        if self._ready:
            return
        marker = self.git_dir / DISABLED_MARKER
        if marker.is_file():
            self.disable(marker.read_text(encoding="utf-8").strip() or "disabled")
            raise ShadowError(self.disabled_reason)
        if not (self.git_dir / "HEAD").is_file():
            (self.git_dir / "_home").mkdir(parents=True, exist_ok=True)
            self._run(["init", "-q"])
        (self.git_dir / "_home").mkdir(parents=True, exist_ok=True)
        (self.git_dir / "info").mkdir(parents=True, exist_ok=True)
        self._write_exclude()
        (self.git_dir / "info" / "attributes").write_text(
            "* -text -filter -ident !eol !working-tree-encoding\n", encoding="utf-8"
        )
        (self.git_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        self._ready = True

    def _write_exclude(self):
        names = sorted(IGNORED_PATH_NAMES | set(SNAPSHOT_SKIP_DIRS))
        lines = [f"{name}/" for name in names] + ["/" + _escape_glob(path) for path in self.skipped_large]
        (self.git_dir / "info" / "exclude").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", errors="surrogateescape"
        )

    def _disable_for_good(self, reason):
        self.disable(reason)
        try:
            (self.git_dir / DISABLED_MARKER).write_text(reason + "\n", encoding="utf-8")
        except OSError:
            pass
        raise ShadowError(reason)

    # ---- snapshots -----------------------------------------------------

    def snapshot(self, extra_paths=(), scan_untracked=True):
        """Return the tree id of the current workspace, or None when unavailable."""
        if not self.available or time.monotonic() < self._retry_at:
            return None
        with _LOCK:
            try:
                tree = self._snapshot(extra_paths, scan_untracked)
            except (ShadowError, OSError):
                self._failures += 1
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    self._failures = 0
                    self._retry_at = time.monotonic() + RETRY_COOLDOWN_SECONDS
                return None
            self._failures = 0
            return tree

    def _snapshot(self, extra_paths, scan_untracked):
        self._ensure_ready()
        self._clear_stale_lock()
        if scan_untracked:
            self._exclude_large_untracked()
        added = self._run(["add", "-A", "--ignore-errors"], timeout=ADD_TIMEOUT_SECONDS, check=False)
        for line in added.stderr.decode("utf-8", "replace").splitlines():
            if "adding embedded git repository" in line:
                self._disable_for_good("nested git repository: " + line.split("repository:", 1)[-1].strip())
        forced = [p for p in extra_paths if p and os.path.lexists(self.root / p) and not (self.root / p).is_dir()]
        if forced:
            self._run(["add", "-f", "--ignore-errors", "--", *forced], check=False)
        tree = self._run(["write-tree"]).stdout.decode().strip()
        self._last_snapshot_tree = tree
        self._anchor(tree)
        return tree

    def _clear_stale_lock(self):
        lock = self.git_dir / "index.lock"
        try:
            if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                lock.unlink()
        except OSError:
            pass

    def _exclude_large_untracked(self):
        changed = False
        for relative in list(self.skipped_large):
            try:
                size = os.lstat(self.root / relative).st_size
            except OSError:
                size = -1
            if size <= MAX_UNTRACKED_BYTES:
                self.skipped_large.remove(relative)
                changed = True
        if changed:
            self._write_exclude()
        listing = self._run(["ls-files", "-o", "--exclude-standard", "-z"]).stdout
        for raw in listing.split(b"\0"):
            if not raw:
                continue
            relative = raw.decode("utf-8", "surrogateescape")
            try:
                size = os.lstat(self.root / relative).st_size
            except OSError:
                continue
            if size > MAX_UNTRACKED_BYTES:
                self.skipped_large.append(relative)
                changed = True
        if changed:
            self._write_exclude()

    def _anchor(self, tree):
        """Keep every snapshot reachable from one ref so a gc can never drop it.

        The commit is written as a loose object directly: it costs no git process.
        """
        if tree == self._last_tree:
            return
        ref_file = self.git_dir / SNAPSHOT_REF
        try:
            parent = ref_file.read_text(encoding="utf-8").strip() if ref_file.is_file() else ""
            lines = [f"tree {tree}"]
            if parent:
                lines.append(f"parent {parent}")
            stamp = f"{int(time.time())} +0000"
            lines += [f"author bunnybyte <bunnybyte@localhost> {stamp}", f"committer bunnybyte <bunnybyte@localhost> {stamp}"]
            body = ("\n".join(lines) + "\n\nbunnybyte snapshot\n").encode()
            payload = b"commit %d\0" % len(body) + body
            commit = hashlib.sha1(payload).hexdigest()
            target = self.git_dir / "objects" / commit[:2] / commit[2:]
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
                temporary.write_bytes(zlib.compress(payload))
                os.replace(temporary, target)
            ref_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = ref_file.with_name(ref_file.name + f".{os.getpid()}.tmp")
            temporary.write_text(commit + "\n", encoding="utf-8")
            os.replace(temporary, ref_file)
            self._last_tree = tree
        except OSError:
            return

    def file_count(self, tree):
        """Number of files in ``tree``; free for the newest snapshot (read from the index header)."""
        if tree and tree == self._last_snapshot_tree:
            try:
                with open(self.git_dir / "index", "rb") as handle:
                    header = handle.read(12)
                if header[:4] == b"DIRC":
                    return int.from_bytes(header[8:12], "big")
            except OSError:
                pass
        return len(self.tree_blobs(tree))

    def has_tree(self, tree):
        if not tree or not self.available:
            return False
        with _LOCK:
            try:
                self._ensure_ready()
                return self._run(["cat-file", "-e", f"{tree}^{{tree}}"], check=False).returncode == 0
            except (ShadowError, OSError):
                return False

    # ---- reading trees -------------------------------------------------

    def diff(self, old_tree, new_tree):
        """Changed entries between two trees as dicts: status A/M/D, path, new mode/oid, old oid."""
        with _LOCK:
            raw = self._run(
                ["diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-abbrev", old_tree or EMPTY_TREE, new_tree or EMPTY_TREE]
            ).stdout
        tokens = raw.split(b"\0")
        entries = []
        index = 0
        while index + 1 < len(tokens):
            meta, path = tokens[index], tokens[index + 1]
            index += 2
            if not meta.startswith(b":"):
                continue
            old_mode, new_mode, old_oid, new_oid, status = meta[1:].decode().split(" ")[:5]
            if "160000" in (old_mode, new_mode):
                continue
            entries.append(
                {
                    "status": status[0],
                    "path": path.decode("utf-8", "surrogateescape"),
                    "mode": new_mode,
                    "oid": new_oid,
                    "old_oid": old_oid,
                }
            )
        return entries

    def diff_paths(self, old_tree, new_tree):
        return sorted(item["path"] for item in self.diff(old_tree, new_tree))

    def tree_blobs(self, tree):
        """Map of path -> blob id for every file in a tree (gitlinks skipped)."""
        with _LOCK:
            raw = self._run(["ls-tree", "-r", "-z", "--full-tree", tree or EMPTY_TREE]).stdout
        blobs = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            if kind == "blob":
                blobs[path.decode("utf-8", "surrogateescape")] = oid
        return blobs

    def _read_blobs(self, oids):
        found = {}
        wanted = list(dict.fromkeys(oids))
        for start in range(0, len(wanted), BLOB_BATCH):
            chunk = wanted[start : start + BLOB_BATCH]
            out = self._run(["cat-file", "--batch"], input_bytes=("\n".join(chunk) + "\n").encode()).stdout
            position = 0
            for oid in chunk:
                newline = out.index(b"\n", position)
                header = out[position:newline].split(b" ")
                if len(header) != 3 or header[1] != b"blob":
                    raise ShadowError(f"blob unavailable: {oid}")
                size = int(header[2])
                found[oid] = out[newline + 1 : newline + 1 + size]
                position = newline + 1 + size + 1
        return found

    # ---- applying ------------------------------------------------------

    def apply(self, from_tree, to_tree, *, force=False, protect_ignored=False):
        """Make the workspace match ``to_tree`` for every path that differs from ``from_tree``.

        ``from_tree`` is the state the caller expects the workspace to be in. If one of
        the affected paths no longer matches it (someone edited it since), nothing is
        written unless ``force`` is set, and the paths are returned as conflicts.
        ``protect_ignored`` keeps files that .gitignore hides from being deleted.
        """
        result = ApplyResult()
        with _LOCK:
            changes = self.diff(from_tree, to_tree)
            current = self.snapshot()
            if current is None:
                raise ShadowError(self.disabled_reason or "snapshot failed")
            drifted = set(self.diff_paths(from_tree, current))
            result.conflicts = sorted(item["path"] for item in changes if item["path"] in drifted)
            if result.conflicts and not force:
                return result
            if protect_ignored:
                ignored = self._ignored_paths([c["path"] for c in changes if c["status"] == "D"])
                result.skipped = sorted(ignored)
                changes = [c for c in changes if c["path"] not in ignored]
            blobs = self._read_blobs([item["oid"] for item in changes if item["status"] != "D"])
        root_real = os.path.realpath(self.root)
        emptied = set()
        for item in (c for c in changes if c["status"] == "D"):
            target = self._safe_target(item["path"], root_real)
            if target is None:
                result.failed.append(item["path"])
                continue
            try:
                if os.path.lexists(target):
                    if os.path.isdir(target) and not os.path.islink(target):
                        result.failed.append(item["path"])
                        continue
                    os.unlink(target)
                emptied.add(os.path.dirname(target))
                result.applied.append(item["path"])
            except OSError:
                result.failed.append(item["path"])
        for directory in sorted(emptied, key=len, reverse=True):
            self._prune_empty_dirs(directory, root_real)
        for item in (c for c in changes if c["status"] != "D"):
            target = self._safe_target(item["path"], root_real)
            try:
                if target is None:
                    raise OSError("path escapes workspace")
                self._write_entry(target, item["mode"], blobs[item["oid"]])
                result.applied.append(item["path"])
            except OSError:
                result.failed.append(item["path"])
        return result

    def _ignored_paths(self, paths):
        if not paths:
            return set()
        data = b"\0".join(path.encode("utf-8", "surrogateescape") for path in paths) + b"\0"
        out = self._run(["check-ignore", "--no-index", "-z", "--stdin"], input_bytes=data, check=False).stdout
        return {item.decode("utf-8", "surrogateescape") for item in out.split(b"\0") if item}

    def new_entries(self, old_tree, new_tree, paths):
        """Entries for ``paths`` that exist in ``new_tree`` but not in ``old_tree``."""
        if not paths or old_tree == new_tree:
            return []
        with _LOCK:
            raw = self._run(
                ["diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-abbrev", old_tree, new_tree, "--", *paths]
            ).stdout
        entries = []
        tokens = raw.split(b"\0")
        for index in range(0, len(tokens) - 1, 2):
            meta, path = tokens[index].decode(), tokens[index + 1]
            parts = meta[1:].split(" ")
            if parts[-1].startswith("A") and "160000" not in parts[:2]:
                entries.append((parts[1], parts[3], path))
        return entries

    def tree_with_entries(self, base_tree, entries):
        """``base_tree`` plus the given (mode, oid, path) blobs, built in a throwaway index."""
        if not entries:
            return base_tree
        scratch = self.git_dir / f"index.scratch-{os.getpid()}"
        env = self._env()
        env["GIT_INDEX_FILE"] = str(scratch)
        lines = b"".join(f"{mode} {oid}\t".encode() + path + b"\0" for mode, oid, path in entries)
        try:
            with _LOCK:
                for args, data in ((["read-tree", base_tree], None), (["update-index", "--add", "-z", "--index-info"], lines)):
                    proc = subprocess.run(
                        ["git", *args], input=data, capture_output=True, env=env, cwd=str(self.root),
                        timeout=GIT_TIMEOUT_SECONDS, stdin=None if data is not None else subprocess.DEVNULL,
                    )
                    if proc.returncode != 0:
                        return base_tree
                proc = subprocess.run(
                    ["git", "write-tree"], capture_output=True, env=env, cwd=str(self.root),
                    timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                )
            tree = proc.stdout.decode().strip()
            return tree if proc.returncode == 0 and tree else base_tree
        except (OSError, subprocess.SubprocessError):
            return base_tree
        finally:
            try:
                scratch.unlink()
            except OSError:
                pass

    def _safe_target(self, relative, root_real):
        if not relative or os.path.isabs(relative) or ".." in Path(relative).parts:
            return None
        target = os.path.join(root_real, relative)
        parent = os.path.realpath(os.path.dirname(target))
        if parent != root_real and not parent.startswith(root_real + os.sep):
            return None
        return os.path.join(parent, os.path.basename(target))

    @staticmethod
    def _write_entry(target, mode, data):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        permissions = 0o755 if mode == "100755" else 0o644
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                raise OSError("a directory is in the way")
            if os.path.islink(target) or mode == "120000":
                os.unlink(target)
            else:
                # keep restrictive permissions (a 0600 .env) and only follow git's exec bit
                current = stat.S_IMODE(os.stat(target).st_mode)
                permissions = (current | 0o111) if mode == "100755" else (current & ~0o111)
        if mode == "120000":
            os.symlink(data.decode("utf-8", "surrogateescape"), target)
            return
        temporary = f"{target}.bb-{os.getpid()}.tmp"
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
            os.chmod(temporary, permissions)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _prune_empty_dirs(directory, root_real):
        while directory.startswith(root_real + os.sep):
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)

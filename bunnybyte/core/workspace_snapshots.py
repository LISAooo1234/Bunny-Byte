"""Workspace snapshots taken around risky tools and at checkpoints.

A snapshot is a git tree id from the shadow store when git works, otherwise a
``{path: blob sha1}`` dict built by a stat-cached walk. Both flavours hash file
content the way git does, so a tree and a dict can still be compared when the
shadow store fails between a "before" and an "after" snapshot.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time

from .shadow_snapshots import SNAPSHOT_SKIP_DIRS, ShadowError, ShadowStore
from .undo_log import observe_snapshot
from .workspace import IGNORED_PATH_NAMES

RACY_WINDOW_NS = 2_000_000_000
_HASH_CACHE: dict[str, dict[str, tuple]] = {}


def shadow_store(agent):
    return ShadowStore.for_root(agent.root)


def capture_snapshot(agent, extra_paths=(), *, scan_untracked=True, adopt_existing=False):
    store = shadow_store(agent)
    extra_paths = tuple(extra_paths)
    tree = store.snapshot(extra_paths, scan_untracked=scan_untracked) if store.available else None
    if tree:
        observe_snapshot(agent, tree, extra_paths if adopt_existing else ())
        return tree
    return legacy_snapshot(agent.root)


def legacy_snapshot(root):
    root_text = str(root)
    cache = _HASH_CACHE.setdefault(root_text, {})
    skip = IGNORED_PATH_NAMES | set(SNAPSHOT_SKIP_DIRS)
    now_ns = time.time_ns()
    snapshot = {}
    for directory, subdirs, names in os.walk(root_text):
        subdirs[:] = [name for name in subdirs if name not in skip]
        for name in names:
            full = os.path.join(directory, name)
            try:
                info = os.stat(full)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = os.path.relpath(full, root_text).replace(os.sep, "/")
            key = (info.st_mtime_ns, info.st_size)
            cached = cache.get(relative)
            if cached and cached[0] == key and now_ns - info.st_mtime_ns > RACY_WINDOW_NS:
                snapshot[relative] = cached[1]
                continue
            try:
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            digest = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            cache[relative] = (key, digest)
            snapshot[relative] = digest
    for stale in set(cache) - set(snapshot):
        del cache[stale]
    return snapshot


def diff_snapshots(agent, before, after):
    """Return (changed paths, "created:/modified:/deleted:" summaries) for two snapshots."""
    if isinstance(before, str) and isinstance(after, str):
        if before == after:
            return [], []
        try:
            entries = shadow_store(agent).diff(before, after)
        except ShadowError:
            entries = None
        if entries is not None:
            words = {"A": "created", "D": "deleted"}
            entries.sort(key=lambda item: item["path"])
            return (
                [item["path"] for item in entries],
                [f"{words.get(item['status'], 'modified')}:{item['path']}" for item in entries],
            )
    return _diff_dicts(_as_dict(agent, before), _as_dict(agent, after))


def _as_dict(agent, snapshot):
    if isinstance(snapshot, dict):
        return snapshot
    try:
        return shadow_store(agent).tree_blobs(snapshot)
    except ShadowError:
        return {}


def _diff_dicts(before, after):
    changed_paths = []
    summaries = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        changed_paths.append(path)
        if path not in before:
            summaries.append(f"created:{path}")
        elif path not in after:
            summaries.append(f"deleted:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries

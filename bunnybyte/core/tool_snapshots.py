"""Workspace snapshots around a tool call."""

from __future__ import annotations

from .workspace import IGNORED_PATH_NAMES

WRITE_TARGET_TOOLS = {"write_file", "patch_file"}


def take_snapshot(agent, tool, args, phase):
    """Snapshot around a risky tool; ``phase`` is "before" or "after" the tool ran."""
    targets = write_targets(agent, tool, args)
    # Only the pre-tool scan guards against huge new untracked files; a write tool cannot create one.
    snapshot = agent.capture_workspace_snapshot(
        targets, scan_untracked=not (phase == "after" and targets), adopt_existing=phase == "before"
    )
    if isinstance(snapshot, str):
        agent._tool_tree_hint = snapshot
    return snapshot


def write_targets(agent, tool, args):
    """Workspace-relative paths a file tool is about to write.

    Gitignored files are invisible to the snapshot store, so the tools that name
    their target explicitly pass it along to be tracked anyway.
    """
    if getattr(tool, "name", "") not in WRITE_TARGET_TOOLS or not isinstance(args, dict):
        return ()
    raw = args.get("path")
    if not raw:
        return ()
    try:
        relative = agent.path(raw).relative_to(agent.root)
    except (ValueError, OSError):
        return ()
    if any(part in IGNORED_PATH_NAMES for part in relative.parts):
        return ()
    return (relative.as_posix(),)

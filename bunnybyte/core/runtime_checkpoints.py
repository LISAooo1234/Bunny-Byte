"""Runtime workspace snapshot and checkpoint helpers."""

import json
import shutil
import uuid

from ..features import memory as memorylib
from .shadow_snapshots import SNAPSHOT_SKIP_DIRS, ShadowError
from .workspace import IGNORED_PATH_NAMES, clip, now
from .workspace_snapshots import capture_snapshot, diff_snapshots, shadow_store

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
ROLLBACK_SNAPSHOT_MAX_BYTES = 20_000_000
SKIPPED_NAMES = IGNORED_PATH_NAMES | set(SNAPSHOT_SKIP_DIRS)


class RuntimeCheckpointsMixin:
    def capture_workspace_snapshot(self, extra_paths=(), **options):
        return capture_snapshot(self, extra_paths, **options)

    def diff_workspace_snapshots(self, before, after):
        return diff_snapshots(self, before, after)

    def create_checkpoint(self, task_state, user_message, trigger):
        state = self.checkpoint_state()
        current = self.current_checkpoint()
        checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
        key_files = []
        freshness = {}
        for path in self.memory.to_dict()["working"]["recent_files"]:
            file_freshness = memorylib.file_freshness(path, self.root)
            freshness[path] = file_freshness
            key_files.append({"path": path, "freshness": file_freshness})
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "created_at": now(),
            "current_goal": str(user_message),
            "completed": [task_state.final_answer] if task_state.final_answer else [],
            "excluded": [],
            "current_blocker": "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason),
            "next_step": self.infer_next_step(task_state),
            "key_files": key_files,
            "freshness": freshness,
            "summary": f"{trigger}: {clip(str(user_message), 120)}",
            "runtime_identity": self.current_runtime_identity(),
            "after_event_id": self._latest_history_event_id(),
            "after_turn_id": str(getattr(self, "current_turn_id", "") or ""),
            "after_run_id": str(getattr(self, "current_run_id", "") or ""),
            "checkpoint_backend": "session",
        }
        checkpoint.update(self.capture_rollback_snapshot(checkpoint_id, reuse_tool_tree=trigger == "tool_executed"))
        state["items"][checkpoint_id] = checkpoint
        state["current_id"] = checkpoint_id
        task_state.checkpoint_id = checkpoint_id
        self.session["runtime_identity"] = checkpoint["runtime_identity"]
        self.session_path = self.session_store.save(self.session)
        return checkpoint

    def capture_rollback_snapshot(self, checkpoint_id, reuse_tool_tree=False):
        hint = self.__dict__.pop("_tool_tree_hint", None)
        tree = hint if reuse_tool_tree and hint else self.capture_workspace_snapshot()
        if isinstance(tree, str):
            return self._record_tree_checkpoint(checkpoint_id, tree)
        return self._copy_rollback_snapshot(checkpoint_id)

    def _record_tree_checkpoint(self, checkpoint_id, tree):
        store = shadow_store(self)
        try:
            files = store.file_count(tree)
        except ShadowError:
            files = 0
        manifest_path = self.root / ".bunnybyte" / "checkpoints" / checkpoint_id / "manifest.json"
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {"checkpoint_id": checkpoint_id, "created_at": now(), "shadow_tree": tree, "file_count": files},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            return {"rollback_snapshot_available": False, "rollback_snapshot_truncated": True}
        return {
            "rollback_snapshot_available": True,
            "rollback_snapshot_truncated": False,
            "rollback_snapshot_path": f".bunnybyte/checkpoints/{checkpoint_id}/manifest.json",
            "rollback_file_count": files,
            "shadow_tree": tree,
        }

    def _copy_rollback_snapshot(self, checkpoint_id):
        snapshot_dir = self.root / ".bunnybyte" / "checkpoints" / checkpoint_id / "workspace"
        manifest_path = snapshot_dir.parent / "manifest.json"
        files = []
        total_bytes = 0
        truncated = False
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            for path in self.root.rglob("*"):
                try:
                    relative = path.relative_to(self.root)
                except ValueError:
                    continue
                if any(part in SKIPPED_NAMES for part in relative.parts) or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                total_bytes += size
                if total_bytes > ROLLBACK_SNAPSHOT_MAX_BYTES:
                    truncated = True
                    break
                target = snapshot_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(path, target)
                    files.append(relative.as_posix())
                except OSError:
                    continue
            manifest = {
                "checkpoint_id": checkpoint_id,
                "created_at": now(),
                "files": files,
                "truncated": truncated,
                "total_bytes": total_bytes,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            return {"rollback_snapshot_available": False, "rollback_snapshot_truncated": True}
        return {
            "rollback_snapshot_available": not truncated,
            "rollback_snapshot_truncated": truncated,
            "rollback_snapshot_path": f".bunnybyte/checkpoints/{checkpoint_id}/manifest.json",
            "rollback_file_count": len(files),
        }

    def restore_checkpoint(self, checkpoint_id):
        """Restore workspace files from a rollback snapshot, if available."""
        checkpoint_id = str(checkpoint_id or "").strip()
        if not checkpoint_id:
            return False
        checkpoint = self.checkpoint_state().get("items", {}).get(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"checkpoint not found: {checkpoint_id}")
        if not checkpoint.get("rollback_snapshot_available"):
            return False
        if checkpoint.get("shadow_tree"):
            return self._restore_from_tree(checkpoint)
        manifest_path = self.root / str(checkpoint.get("rollback_snapshot_path", ""))
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        snapshot_dir = manifest_path.parent / "workspace"
        expected = {str(path) for path in manifest.get("files", []) or []}
        if not snapshot_dir.is_dir():
            return False
        self._save_rollback_safety_backup(checkpoint_id)
        self._restore_workspace_from_snapshot(snapshot_dir, expected)
        self.refresh_prefix(force=True)
        return True

    def _restore_from_tree(self, checkpoint):
        store = shadow_store(self)
        target = str(checkpoint.get("shadow_tree", ""))
        if not store.has_tree(target):
            return False
        current = store.snapshot()
        if current is None:
            return False
        try:
            result = store.apply(current, target, force=True, protect_ignored=True)
        except ShadowError:
            return False
        self.refresh_prefix(force=True)
        if result.failed:
            listed = "、".join(result.failed[:10])
            raise ValueError(f"回滚只完成了一部分，这些文件没能还原：{listed}（回滚前的状态已保存在 .bunnybyte/shadow.git）")
        return True

    def _restore_workspace_from_snapshot(self, snapshot_dir, expected):
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in SKIPPED_NAMES for part in relative.parts):
                continue
            relative_text = relative.as_posix()
            if path.is_file() and relative_text not in expected:
                path.unlink()
        for relative_text in expected:
            source = snapshot_dir / relative_text
            if not source.is_file():
                continue
            target = self.path(relative_text)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in SKIPPED_NAMES for part in relative.parts):
                continue
            if path.is_dir():
                try:
                    next(path.iterdir())
                except StopIteration:
                    shutil.rmtree(path)

    def _save_rollback_safety_backup(self, checkpoint_id):
        backup_id = "rollback_safety_" + uuid.uuid4().hex[:8]
        backup_dir = self.root / ".bunnybyte" / "checkpoints" / "_safety" / backup_id / "workspace"
        files = []
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for path in self.root.rglob("*"):
                try:
                    relative = path.relative_to(self.root)
                except ValueError:
                    continue
                if any(part in SKIPPED_NAMES for part in relative.parts) or not path.is_file():
                    continue
                target = backup_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                files.append(relative.as_posix())
            manifest = {"checkpoint_id": checkpoint_id, "created_at": now(), "files": files}
            (backup_dir.parent / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            return

    def _latest_history_event_id(self):
        for item in reversed(self.session.get("history", []) or []):
            event_id = str(item.get("event_id", "") or "")
            if event_id:
                return event_id
        return ""

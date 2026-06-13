"""Runtime workspace snapshot and checkpoint helpers."""

import hashlib
import json
import shutil
import subprocess
import uuid

from ..features import memory as memorylib
from .workspace import IGNORED_PATH_NAMES, clip, now

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_INLINE_MAX_BYTES = 2_000_000


class RuntimeCheckpointsMixin:
    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts) or not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
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
        }
        checkpoint.update(self.capture_workspace_checkpoint(checkpoint_id))
        state["items"][checkpoint_id] = checkpoint
        state["current_id"] = checkpoint_id
        task_state.checkpoint_id = checkpoint_id
        self.session["runtime_identity"] = checkpoint["runtime_identity"]
        self.session_path = self.session_store.save(self.session)
        return checkpoint

    def capture_workspace_checkpoint(self, checkpoint_id):
        git_checkpoint = self._capture_git_checkpoint(checkpoint_id)
        if git_checkpoint:
            return git_checkpoint
        workspace_snapshot, workspace_snapshot_truncated = self.capture_workspace_contents()
        return {
            "checkpoint_backend": "inline",
            "workspace_snapshot": workspace_snapshot,
            "workspace_snapshot_truncated": workspace_snapshot_truncated,
        }

    def _capture_git_checkpoint(self, checkpoint_id):
        base_head = self._git_stdout(["rev-parse", "--verify", "HEAD"])
        if not base_head:
            return None
        checkpoint_dir = self.root / ".bunnybyte" / "checkpoints" / checkpoint_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        tracked_patch = checkpoint_dir / "tracked.patch"
        diff = self._git_bytes(["diff", "--binary", "HEAD", "--"])
        tracked_patch.write_bytes(diff or b"")
        untracked_paths = self._git_untracked_paths()
        untracked_root = checkpoint_dir / "untracked"
        for relative_text in untracked_paths:
            source = self.root / relative_text
            if not source.is_file():
                continue
            target = untracked_root / relative_text
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest = {
            "checkpoint_id": checkpoint_id,
            "backend": "git",
            "base_head": base_head,
            "branch": self._git_stdout(["branch", "--show-current"]),
            "tracked_patch": "tracked.patch",
            "untracked_paths": untracked_paths,
            "created_at": now(),
        }
        (checkpoint_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "checkpoint_backend": "git",
            "checkpoint_path": f".bunnybyte/checkpoints/{checkpoint_id}/manifest.json",
            "git_base_head": base_head,
            "git_untracked_paths": untracked_paths,
        }

    def capture_workspace_contents(self):
        snapshot = {}
        total_bytes = 0
        for path in self.root.rglob("*"):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative.parts) or not path.is_file():
                continue
            try:
                content = path.read_bytes()
            except Exception:
                continue
            total_bytes += len(content)
            if total_bytes > CHECKPOINT_INLINE_MAX_BYTES:
                return {}, True
            snapshot[relative.as_posix()] = content.hex()
        return snapshot, False

    def restore_checkpoint(self, checkpoint_id):
        checkpoint_id = str(checkpoint_id or "").strip()
        if not checkpoint_id:
            return False
        checkpoint = self.checkpoint_state().get("items", {}).get(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"checkpoint not found: {checkpoint_id}")
        if checkpoint.get("checkpoint_backend") == "git" and self._restore_git_checkpoint(checkpoint):
            self.refresh_prefix(force=True)
            return True
        snapshot = checkpoint.get("workspace_snapshot")
        if not isinstance(snapshot, dict):
            return False
        if not snapshot and checkpoint.get("workspace_snapshot_truncated"):
            return False
        self._restore_workspace_contents(snapshot)
        self.refresh_prefix(force=True)
        return True

    def _restore_git_checkpoint(self, checkpoint):
        manifest_path = self.root / str(checkpoint.get("checkpoint_path", ""))
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        base_head = str(manifest.get("base_head", "")).strip()
        if not base_head:
            return False
        self._save_git_safety_backup(str(checkpoint.get("checkpoint_id", "")))
        if not self._git_ok(["reset", "--hard", base_head]):
            return False
        self._git_ok(["clean", "-fd", "--exclude=.bunnybyte"])
        patch_path = manifest_path.parent / str(manifest.get("tracked_patch", "tracked.patch"))
        if patch_path.is_file() and patch_path.stat().st_size > 0:
            if not self._git_ok(["apply", "--binary", str(patch_path)]):
                return False
        untracked_root = manifest_path.parent / "untracked"
        for relative_text in manifest.get("untracked_paths", []) or []:
            source = untracked_root / str(relative_text)
            if not source.is_file():
                continue
            target = self.path(str(relative_text))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return True

    def _save_git_safety_backup(self, checkpoint_id):
        backup_id = "restore_" + uuid.uuid4().hex[:8]
        backup_dir = self.root / ".bunnybyte" / "checkpoints" / "_safety" / backup_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "tracked.patch").write_bytes(self._git_bytes(["diff", "--binary", "HEAD", "--"]) or b"")
        untracked_paths = self._git_untracked_paths()
        for relative_text in untracked_paths:
            source = self.root / relative_text
            if not source.is_file():
                continue
            target = backup_dir / "untracked" / relative_text
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest = {"checkpoint_id": checkpoint_id, "created_at": now(), "untracked_paths": untracked_paths}
        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    def _restore_workspace_contents(self, snapshot):
        expected = {str(path) for path in snapshot}
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative.parts):
                continue
            relative_text = relative.as_posix()
            if path.is_file() and relative_text not in expected:
                path.unlink()
        for relative_text, hex_content in snapshot.items():
            target = self.path(relative_text)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes.fromhex(str(hex_content)))
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative.parts):
                continue
            if path.is_dir():
                try:
                    next(path.iterdir())
                except StopIteration:
                    shutil.rmtree(path)

    def _git_untracked_paths(self):
        raw = self._git_bytes(["ls-files", "--others", "--exclude-standard", "-z"])
        if not raw:
            return []
        paths = []
        for item in raw.decode("utf-8", errors="replace").split("\0"):
            item = item.strip()
            if item and not item.startswith(".bunnybyte/"):
                paths.append(item)
        return paths

    def _git_stdout(self, args):
        output = self._git_bytes(args)
        return output.decode("utf-8", errors="replace").strip() if output is not None else ""

    def _git_bytes(self, args):
        try:
            result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=False, timeout=10)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def _git_ok(self, args):
        try:
            result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=False, timeout=20)
        except Exception:
            return False
        return result.returncode == 0

    def _latest_history_event_id(self):
        for item in reversed(self.session.get("history", []) or []):
            event_id = str(item.get("event_id", "") or "")
            if event_id:
                return event_id
        return ""

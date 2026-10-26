import os
import subprocess

import pytest

from bunnybyte.core import shadow_snapshots
from bunnybyte.core.shadow_snapshots import ShadowStore

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git is required"
)


def write(root, relative, content, mode=None):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if mode:
        path.chmod(mode)
    return path


def test_snapshot_is_stable_and_skips_dependency_dirs_and_gitignored_files(tmp_path):
    write(tmp_path, "src/a.py", "a\n")
    write(tmp_path, "node_modules/pkg/index.js", "x\n")
    write(tmp_path, ".gitignore", "*.log\nbuild/\n")
    write(tmp_path, "debug.log", "noise\n")
    write(tmp_path, "build/out.bin", "o\n")
    write(tmp_path, ".bunnybyte/sessions/s.json", "{}")
    store = ShadowStore(tmp_path)

    first = store.snapshot()

    assert first and first == store.snapshot()
    assert sorted(store.tree_blobs(first)) == [".gitignore", "src/a.py"]
    write(tmp_path, "debug.log", "more noise\n")
    write(tmp_path, "node_modules/pkg/index.js", "y\n")
    assert store.snapshot() == first


def test_the_users_repository_is_never_touched(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write(tmp_path, "a.txt", "a\n")
    before = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    index_before = (tmp_path / ".git" / "index").read_bytes() if (tmp_path / ".git" / "index").exists() else b""

    assert ShadowStore(tmp_path).snapshot()

    after = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert after == before == "?? a.txt\n"
    index_after = (tmp_path / ".git" / "index").read_bytes() if (tmp_path / ".git" / "index").exists() else b""
    assert index_after == index_before
    refs = subprocess.run(["git", "for-each-ref"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert refs == ""


def test_apply_round_trips_crlf_files_even_when_the_repo_sets_text_auto(tmp_path):
    write(tmp_path, ".gitattributes", "* text=auto eol=crlf\n*.bat text eol=crlf\n")
    write(tmp_path, "win.bat", b"echo a\r\necho b\r\n")
    write(tmp_path, "unix.sh", b"echo a\necho b\n")
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    write(tmp_path, "win.bat", b"changed\n")
    (tmp_path / "unix.sh").unlink()
    after = store.snapshot()

    result = store.apply(after, before)

    assert result.conflicts == [] and result.failed == []
    assert (tmp_path / "win.bat").read_bytes() == b"echo a\r\necho b\r\n"
    assert (tmp_path / "unix.sh").read_bytes() == b"echo a\necho b\n"
    assert store.snapshot() == before


def test_non_ascii_paths_come_back_unquoted(tmp_path):
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    write(tmp_path, "文档/说明 书.md", "你好\n")
    after = store.snapshot()

    assert store.diff_paths(before, after) == ["文档/说明 书.md"]
    store.apply(after, before)
    assert not (tmp_path / "文档").exists()
    store.apply(before, after)
    assert (tmp_path / "文档" / "说明 书.md").read_text(encoding="utf-8") == "你好\n"


def test_apply_handles_create_modify_delete_exec_bit_symlink_and_empty_dirs(tmp_path):
    write(tmp_path, "keep.txt", "keep\n")
    write(tmp_path, "mod.txt", "old\n")
    write(tmp_path, "gone.txt", "gone\n")
    write(tmp_path, "run.sh", "#!/bin/sh\n", mode=0o755)
    os.symlink("keep.txt", tmp_path / "link")
    store = ShadowStore(tmp_path)
    before = store.snapshot()

    write(tmp_path, "mod.txt", "new\n")
    (tmp_path / "gone.txt").unlink()
    write(tmp_path, "deep/er/new.txt", "n\n")
    (tmp_path / "run.sh").chmod(0o644)
    (tmp_path / "link").unlink()
    os.symlink("mod.txt", tmp_path / "link")
    after = store.snapshot()

    undone = store.apply(after, before)

    assert sorted(undone.applied) == ["deep/er/new.txt", "gone.txt", "link", "mod.txt", "run.sh"]
    assert (tmp_path / "mod.txt").read_text() == "old\n"
    assert (tmp_path / "gone.txt").read_text() == "gone\n"
    assert not (tmp_path / "deep").exists()
    assert os.access(tmp_path / "run.sh", os.X_OK)
    assert os.readlink(tmp_path / "link") == "keep.txt"
    assert store.snapshot() == before

    store.apply(before, after)
    assert (tmp_path / "deep" / "er" / "new.txt").read_text() == "n\n"
    assert not os.access(tmp_path / "run.sh", os.X_OK)


def test_apply_keeps_directories_it_did_not_empty(tmp_path):
    write(tmp_path, "pkg/a.txt", "a\n")
    write(tmp_path, "pkg/b.txt", "b\n")
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    (tmp_path / "pkg" / "a.txt").unlink()
    (tmp_path / "pkg" / "b.txt").unlink()
    (tmp_path / "pkg").rmdir()
    (tmp_path / "empty_dir_of_the_user").mkdir()
    after = store.snapshot()

    store.apply(after, before)

    assert (tmp_path / "pkg" / "b.txt").exists()
    assert (tmp_path / "empty_dir_of_the_user").is_dir()


def test_apply_refuses_when_a_touched_file_changed_since_and_force_overrides(tmp_path):
    write(tmp_path, "a.txt", "one\n")
    write(tmp_path, "b.txt", "one\n")
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    write(tmp_path, "a.txt", "agent\n")
    write(tmp_path, "b.txt", "agent\n")
    after = store.snapshot()
    write(tmp_path, "a.txt", "user edit after the turn\n")

    refused = store.apply(after, before)

    assert refused.conflicts == ["a.txt"]
    assert refused.applied == []
    assert (tmp_path / "a.txt").read_text() == "user edit after the turn\n"
    assert (tmp_path / "b.txt").read_text() == "agent\n"

    forced = store.apply(after, before, force=True)

    assert sorted(forced.applied) == ["a.txt", "b.txt"]
    assert (tmp_path / "a.txt").read_text() == "one\n"


def test_apply_never_writes_through_a_symlinked_parent_outside_the_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "ws"
    root.mkdir()
    write(root, "d/f.txt", "old\n")
    store = ShadowStore(root)
    before = store.snapshot()
    write(root, "d/f.txt", "new\n")
    after = store.snapshot()
    # after the turn, d is swapped for a symlink to a directory outside the workspace
    (root / "d" / "f.txt").unlink()
    (root / "d").rmdir()
    os.symlink(outside, root / "d")

    result = store.apply(after, before, force=True)

    assert not (outside / "f.txt").exists()
    assert "d/f.txt" in result.failed


def test_nested_git_repository_disables_the_store(tmp_path):
    write(tmp_path, "a.txt", "a\n")
    nested = tmp_path / "vendor" / "lib"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    write(nested, "x.txt", "x\n")
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *identity, "add", "x.txt"], cwd=nested, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "x"], cwd=nested, check=True)
    store = ShadowStore(tmp_path)

    assert store.snapshot() is None
    assert not store.available
    assert "nested git repository" in store.disabled_reason


def test_large_untracked_files_are_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_snapshots, "MAX_UNTRACKED_BYTES", 1000)
    write(tmp_path, "small.txt", "s\n")
    write(tmp_path, "dataset.bin", b"x" * 5000)
    write(tmp_path, "we[ird] *name.bin", b"x" * 5000)
    store = ShadowStore(tmp_path)

    tree = store.snapshot()

    assert sorted(store.tree_blobs(tree)) == ["small.txt"]
    assert sorted(store.skipped_large) == ["dataset.bin", "we[ird] *name.bin"]
    assert store.snapshot() == tree


def test_explicit_write_targets_are_tracked_even_when_gitignored(tmp_path):
    write(tmp_path, ".gitignore", ".env\ndist/\n")
    write(tmp_path, ".env", "KEY=1\n")
    store = ShadowStore(tmp_path)
    before = store.snapshot(extra_paths=(".env",))
    write(tmp_path, ".env", "KEY=2\n")
    write(tmp_path, "dist/app.js", "js\n")
    after = store.snapshot(extra_paths=(".env", "dist/app.js"))

    assert store.diff_paths(before, after) == [".env", "dist/app.js"]


def test_snapshots_survive_a_git_gc(tmp_path):
    write(tmp_path, "a.txt", "a\n")
    store = ShadowStore(tmp_path)
    first = store.snapshot()
    write(tmp_path, "a.txt", "b\n")
    second = store.snapshot()
    env = {**os.environ, "GIT_DIR": str(store.git_dir)}
    subprocess.run(["git", "gc", "-q", "--prune=now"], env=env, cwd=tmp_path, check=True)

    assert store.has_tree(first) and store.has_tree(second)


def test_stale_index_lock_is_cleared(tmp_path):
    write(tmp_path, "a.txt", "a\n")
    store = ShadowStore(tmp_path)
    assert store.snapshot()
    lock = store.git_dir / "index.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - 600
    os.utime(lock, (old, old))
    write(tmp_path, "a.txt", "changed\n")

    assert store.snapshot()
    assert not lock.exists()


def test_missing_git_disables_the_store_instead_of_raising(tmp_path, monkeypatch):
    write(tmp_path, "a.txt", "a\n")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    store = ShadowStore(tmp_path)

    assert store.snapshot() is None
    assert not store.available
    assert store.disabled_reason == "git not found"


def test_shadow_store_can_be_switched_off_with_an_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("BUNNYBYTE_SHADOW", "off")

    assert ShadowStore(tmp_path).snapshot() is None


def test_concurrent_snapshots_from_several_threads_do_not_break_each_other(tmp_path):
    import threading

    write(tmp_path, "a.txt", "a\n")
    store = ShadowStore(tmp_path)
    results = []

    def worker(index):
        write(tmp_path, f"file{index}.txt", f"{index}\n")
        results.append(store.snapshot())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 6 and all(results)
    assert store.available
    final = store.snapshot()
    assert sorted(store.tree_blobs(final)) == ["a.txt"] + [f"file{i}.txt" for i in range(6)]


def test_file_replaced_by_a_directory_comes_back_as_a_file(tmp_path):
    write(tmp_path, "foo", "i am a file\n")
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    (tmp_path / "foo").unlink()
    write(tmp_path, "foo/bar.py", "print(1)\n")
    after = store.snapshot()

    result = store.apply(after, before)

    assert result.failed == []
    assert (tmp_path / "foo").is_file() and (tmp_path / "foo").read_text() == "i am a file\n"


def test_restrictive_permissions_survive_an_apply(tmp_path):
    secret = write(tmp_path, "secret.cfg", "a=1\n", mode=0o600)
    store = ShadowStore(tmp_path)
    before = store.snapshot()
    secret.write_text("a=2\n")
    after = store.snapshot()

    store.apply(after, before)

    assert secret.read_text() == "a=1\n"
    assert secret.stat().st_mode & 0o777 == 0o600


def test_protect_ignored_keeps_gitignored_files_that_the_target_tree_lacks(tmp_path):
    write(tmp_path, ".gitignore", ".env\n")
    write(tmp_path, "a.txt", "a\n")
    store = ShadowStore(tmp_path)
    older = store.snapshot()
    write(tmp_path, ".env", "SECRET=1\n")
    write(tmp_path, "later.txt", "x\n")
    newer = store.snapshot(extra_paths=(".env",))

    result = store.apply(newer, older, force=True, protect_ignored=True)

    assert (tmp_path / ".env").read_text() == "SECRET=1\n"
    assert not (tmp_path / "later.txt").exists()
    assert result.skipped == [".env"]


def test_a_nested_repository_keeps_the_store_disabled_after_a_restart(tmp_path):
    write(tmp_path, "a.txt", "a\n")
    nested = tmp_path / "vendor" / "lib"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    write(nested, "x.txt", "x\n")
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *identity, "add", "x.txt"], cwd=nested, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "x"], cwd=nested, check=True)
    assert ShadowStore(tmp_path).snapshot() is None

    restarted = ShadowStore(tmp_path)

    assert restarted.snapshot() is None
    assert "nested git repository" in restarted.disabled_reason


def test_a_large_file_that_shrinks_is_tracked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_snapshots, "MAX_UNTRACKED_BYTES", 1000)
    write(tmp_path, "data.bin", b"x" * 5000)
    write(tmp_path, "padded name .bin ", b"x" * 5000)
    store = ShadowStore(tmp_path)
    first = store.snapshot()
    assert store.tree_blobs(first) == {}
    assert sorted(store.skipped_large) == ["data.bin", "padded name .bin "]

    write(tmp_path, "data.bin", b"small")
    second = store.snapshot()

    assert sorted(store.tree_blobs(second)) == ["data.bin"]
    assert store.skipped_large == ["padded name .bin "]


def test_repeated_git_failures_only_pause_the_store_for_a_while(tmp_path, monkeypatch):
    write(tmp_path, "a.txt", "a\n")
    store = ShadowStore(tmp_path)
    assert store.snapshot()
    real = store._run
    monkeypatch.setattr(store, "_run", lambda *a, **k: (_ for _ in ()).throw(shadow_snapshots.ShadowError("boom")))
    assert [store.snapshot() for _ in range(3)] == [None, None, None]
    assert store.available
    monkeypatch.setattr(store, "_run", real)
    assert store.snapshot() is None  # cooling down
    store._retry_at = 0
    assert store.snapshot()

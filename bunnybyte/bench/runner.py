"""bench 运行器：为每个任务搭一个临时 git 仓库，驱动真实 agent 跑完，再用 verifier 判分。"""

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from ..core.runtime import BunnyByte
from ..core.session_store import SessionStore
from ..core.workspace import WorkspaceContext
from .tasks import PY

VERIFY_TIMEOUT = 60
DEFAULT_TASK_TIMEOUT = 300
ABORT_GRACE_SECONDS = 5  # 请求中止后再等多久；agent 只在步与步之间检查中止标志
DEFAULT_MAX_STEPS = 50  # 与正常 CLI 的 --max-steps 默认值一致，分数才代表真实使用
IGNORED_PARTS = {".git", ".bunnybyte", "__pycache__"}
GIT_IDENTITY = ["-c", "user.name=bunny-bench", "-c", "user.email=bench@example.invalid"]


def python_command():
    return shlex.quote(sys.executable)


def write_tree(root, files):
    for relative, content in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def init_git(root):
    """给 fixture 初始化 git，让 agent 看到的是真实仓库；失败不影响判分。"""
    steps = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", *GIT_IDENTITY, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"],
    )
    try:
        for argv in steps:
            subprocess.run(argv, cwd=root, capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        pass


def snapshot(root):
    state = {}
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if path.is_dir() or IGNORED_PARTS & set(relative.parts):
            continue
        state[relative.as_posix()] = path.read_bytes()
    return state


@contextlib.contextmanager
def isolated_home(path):
    """让 ~/.bunnybyte/skills 等用户级状态不影响 bench，保证结果可比。"""
    with mock.patch.object(Path, "home", return_value=Path(path)):
        yield


def trace_usage(trace_path):
    """汇总 trace 里每次模型调用上报的 token；provider 没上报就返回 None，而不是 0。"""
    totals = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    path = Path(trace_path)
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "model_parsed":
            continue
        metadata = event.get("completion_metadata") or {}
        for key in totals:
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = (totals[key] or 0) + value
    return totals


def _tail(text, limit=300):
    text = str(text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _remove_bytecode(root):
    for cache in Path(root).rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def verify(task, root, answer, before):
    """返回失败原因列表；空列表表示通过。"""
    problems = []
    now = snapshot(root)
    for relative in task.frozen:
        if now.get(relative) != task.files[relative].encode("utf-8"):
            problems.append(f"改动了不该动的文件：{relative}")
    if task.read_only and now != before:
        changed = sorted(set(now) ^ set(before) | {k for k in now if now.get(k) != before.get(k)})
        problems.append("只读任务却改动了文件：" + ", ".join(changed[:5]))
    for pattern in task.answer_regex:
        if not re.search(pattern, str(answer or ""), re.IGNORECASE | re.DOTALL):
            problems.append(f"最终回答缺少：{pattern}")
    if task.verifier:
        problems.extend(_run_verifier(task, root))
    return problems


def _run_verifier(task, root):
    write_tree(root, task.hidden_files)
    _remove_bytecode(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(
            task.verifier.replace(PY, python_command()),
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ["verifier 超时"]
    finally:
        for relative in task.hidden_files:
            (Path(root) / relative).unlink(missing_ok=True)
    if done.returncode != 0:
        return ["verifier 未通过：" + _tail(done.stderr or done.stdout)]
    return []


def scrub(text, root, home):
    """去掉临时目录和用户目录，避免本机路径写进可提交的结果文件。"""
    text = str(text)
    temp = tempfile.gettempdir()
    for prefix, label in (
        (str(root), "<repo>"),
        (str(Path(root).resolve()), "<repo>"),
        (str(Path(temp).resolve()), "<tmp>"),
        (temp, "<tmp>"),
        (str(home), "~"),
    ):
        if prefix and prefix != "/":
            text = text.replace(prefix, label)
    return text


def _drive(agent, prompt, timeout, result):
    """在工作线程里跑 agent.ask：超时先请求中止，宽限期后仍不返回就放弃该线程。"""
    outcome = {}

    def work():
        try:
            outcome["answer"] = agent.ask(prompt)
        except Exception as exc:  # 模型/网络故障算这一题失败，但不能让整轮 bench 中断
            outcome["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        result["timed_out"] = True
        agent.abort_current_turn()
        worker.join(ABORT_GRACE_SECONDS)
    return outcome.get("answer", ""), outcome.get("error"), worker.is_alive()


def run_task(
    task, client, *, attempt=1, timeout=DEFAULT_TASK_TIMEOUT, max_steps=DEFAULT_MAX_STEPS, keep_dir=None
):
    """在全新的临时仓库里跑一次任务，返回一行可序列化的结果。

    max_steps 是 agent 的硬上限；task.step_budget 只是“高效完成应有的步数”，超出记为 over_budget，不直接判负。
    """
    real_home = Path.home()
    root = Path(keep_dir or tempfile.mkdtemp(prefix=f"bunny-bench-{task.id}-"))
    write_tree(root, task.files)
    init_git(root)
    before = snapshot(root)
    result = {
        "task": task.id,
        "attempt": attempt,
        "passed": False,
        "failure": "",
        "steps": 0,
        "over_budget": False,
        "model_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "wall_seconds": 0.0,
        "stop_reason": "",
        "timed_out": False,
    }
    started = time.monotonic()
    agent = None
    answer, error, stuck = "", None, False
    try:
        with isolated_home(root / ".home"):
            agent = BunnyByte(
                model_client=client,
                workspace=WorkspaceContext.build(root),
                session_store=SessionStore(root / ".bunnybyte" / "sessions"),
                approval_policy="auto",
                max_steps=max_steps,
                auto_dream=False,
            )
            answer, error, stuck = _drive(agent, task.prompt, timeout, result)
    except Exception as exc:
        error = exc
    result["wall_seconds"] = round(time.monotonic() - started, 2)
    state = getattr(agent, "current_task_state", None)
    if state is not None:
        result["steps"] = state.tool_steps
        result["over_budget"] = state.tool_steps > task.step_budget
        result["model_calls"] = state.attempts
        result["stop_reason"] = state.stop_reason
        result.update(trace_usage(agent.run_store.trace_path(state)))
    problems = []
    if stuck:
        problems.append(f"超过 {timeout}s 墙钟上限，且中止请求未能生效")
    elif error is not None:
        problems.append(f"运行出错：{type(error).__name__}: {_tail(error, 160)}")
    else:
        problems = verify(task, root, answer, before)
        if result["timed_out"]:
            problems.insert(0, f"超过 {timeout}s 墙钟上限")
    result["failure"] = scrub("；".join(problems), root, real_home)
    result["passed"] = not result["failure"]
    if keep_dir is None:
        shutil.rmtree(root, ignore_errors=True)
    return result


def run_bench(
    tasks, client_factory, *, repeat=1, timeout=DEFAULT_TASK_TIMEOUT, max_steps=DEFAULT_MAX_STEPS, on_result=None
):
    results = []
    for task in tasks:
        for attempt in range(1, repeat + 1):
            row = run_task(
                task, client_factory(task), attempt=attempt, timeout=timeout, max_steps=max_steps
            )
            results.append(row)
            if on_result is not None:
                on_result(row)
    return results

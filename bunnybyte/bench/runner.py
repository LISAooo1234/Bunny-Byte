"""bench 运行器：为每个任务搭一个临时 git 仓库，驱动真实 agent 跑完，再用 verifier 判分。"""

import contextlib
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

from ..core.critic import CriticConfig
from ..core.run_usage import trace_prompt_reuse, trace_usage
from ..core.runtime import BunnyByte
from ..core.session_store import SessionStore
from ..core.shadow_snapshots import _LOCK, ShadowError, ShadowStore
from ..core.verification import VerifyConfig, claims_verified
from ..core.workspace import WorkspaceContext
from .tasks import PY

VERIFY_TIMEOUT = 60
DEFAULT_TASK_TIMEOUT = 300
# 单次闸门远小于单题墙钟上限；修复证明默认关，`--verify` 的历史结果才和以前可比
BENCH_VERIFY = VerifyConfig(mode="auto", timeout=60, max_repair_rounds=1, fix_proof="off")
FIX_PROOF_MODES = ("off", "report", "repair")
BENCH_CRITIC = CriticConfig(mode="auto", max_rounds=1)
ABORT_GRACE_SECONDS = 5  # 请求中止后再等多久；agent 只在步与步之间检查中止标志
DEFAULT_MAX_STEPS = 50  # 与正常 CLI 的 --max-steps 默认值一致，分数才代表真实使用
IGNORED_PARTS = {".git", ".bunnybyte", "__pycache__"}
GIT_IDENTITY = ["-c", "user.name=bunny-bench", "-c", "user.email=bench@example.invalid"]


def python_command():
    return shlex.quote(sys.executable)


@contextlib.contextmanager
def bench_path():
    """修复证明用 PATH 上的 python3 跑测试，判分用当前解释器：把当前解释器的目录放到 PATH 最前面，两边才是同一个 Python。"""
    folder = os.path.dirname(sys.executable)
    with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([folder, os.environ.get("PATH", "")])}):
        yield


def pytest_preflight():
    """修复证明会用 pytest 跑裸 `def test_` 文件：PATH 上的 python3 没装 pytest 时这些行全会变成 no_pytest，花了钱却量不到东西。"""
    with bench_path():
        python = shutil.which("python3") or sys.executable
        try:
            done = subprocess.run([python, "-c", "import pytest"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{python} 无法运行：{exc}"
    if done.returncode != 0:
        return f"{python} 没有安装 pytest（修复证明和判分都要用它跑 pytest 风格的测试）：pip install pytest"
    return ""


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


def materialize_tree(root, tree, target):
    """把影子快照里的某个 tree 原样写到 target，用来回答“审查提意见那一刻，这份代码能不能过判分”。"""
    store = ShadowStore.for_root(root)
    blobs = store.tree_blobs(tree)
    with _LOCK:
        data = store._read_blobs(list(blobs.values())) if blobs else {}
    for relative, oid in blobs.items():
        path = Path(target) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data[oid])


def grade_objections(task, root, state, answer, before):
    """对每次“提出反对”的审查，用当时的快照重跑判分：本来就能过的，说明这条反对是多余的（不一定错，但没帮上忙）。"""
    passing = 0
    for review in (state.critic or {}).get("reviews", []):
        if review.get("verdict") != "object" or not review.get("tree"):
            continue
        with tempfile.TemporaryDirectory(prefix="bunny-bench-critic-") as scratch:
            try:
                materialize_tree(root, review["tree"], scratch)
            except (ShadowError, OSError, KeyError):
                continue
            if not verify(task, scratch, answer, before):
                passing += 1
    return passing


def fix_proof_label(result):
    """一行结果里的修复证明：proven / theater / weak，跳过时是 skipped:原因，没跑是空串。"""
    if not result:
        return ""
    status = str(result.get("status", ""))
    return f"skipped:{result.get('reason', '')}" if status == "skipped" else status


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
    task,
    client,
    *,
    attempt=1,
    timeout=DEFAULT_TASK_TIMEOUT,
    max_steps=DEFAULT_MAX_STEPS,
    keep_dir=None,
    verify_gate=False,
    critic=False,
    prices=None,
    fix_proof="off",
):
    """在全新的临时仓库里跑一次任务，返回一行可序列化的结果。

    fix_proof 为 report / repair 时同时打开验证闸门，并按该模式跑修复证明。

    max_steps 是 agent 的硬上限；task.step_budget 只是“高效完成应有的步数”，超出记为 over_budget，不直接判负。
    """
    if fix_proof not in FIX_PROOF_MODES:
        raise ValueError(f"fix_proof 必须是 {', '.join(FIX_PROOF_MODES)} 之一")
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
        "wasted_steps": 0,
        "over_budget": False,
        "model_calls": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "cost_usd": None,
        "wall_seconds": 0.0,
        "stop_reason": "",
        "timed_out": False,
        "verify_status": "",
        "verify_rounds": 0,
        "verify_repairs": 0,
        "false_claim": False,
        "critic_rounds": 0,
        "critic_objections": 0,
        "objection_on_passing": 0,
        "critic_regression": False,
        "critic_issues": [],
        "fix_proof": "",
        "fix_proof_repairs": 0,
    }
    verify_config = None
    if fix_proof != "off":
        verify_config = VerifyConfig(mode="auto", timeout=60, max_repair_rounds=1, fix_proof=fix_proof)
    elif verify_gate:
        verify_config = BENCH_VERIFY
    started = time.monotonic()
    agent = None
    answer, error, stuck = "", None, False
    try:
        with isolated_home(root / ".home"), bench_path() if fix_proof != "off" else contextlib.nullcontext():
            agent = BunnyByte(
                model_client=client,
                workspace=WorkspaceContext.build(root),
                session_store=SessionStore(root / ".bunnybyte" / "sessions"),
                approval_policy="auto",
                max_steps=max_steps,
                auto_dream=False,
                verify_config=verify_config,
                critic_config=BENCH_CRITIC if critic else None,
            )
            agent.cost_meter.prices = dict(prices or {})
            answer, error, stuck = _drive(agent, task.prompt, timeout, result)
    except Exception as exc:
        error = exc
    result["wall_seconds"] = round(time.monotonic() - started, 2)
    state = getattr(agent, "current_task_state", None)
    if state is not None:
        result["steps"] = state.tool_steps
        result["wasted_steps"] = state.wasted_steps
        result["over_budget"] = state.tool_steps > task.step_budget
        result["model_calls"] = state.attempts
        result["stop_reason"] = state.stop_reason
        result["verify_status"] = str(state.verification.get("status", ""))
        result["verify_rounds"] = int(state.verification.get("rounds", 0))
        result["verify_repairs"] = int(state.verification.get("repairs", 0))
        result["fix_proof"] = fix_proof_label(state.verification.get("fix_proof"))
        result["fix_proof_repairs"] = int(state.verification.get("fix_proof_repairs", 0))
        result.update(trace_usage(agent.run_store.trace_path(state)))
        result.update(trace_prompt_reuse(agent.run_store.trace_path(state)))
        turn = agent.cost_meter.turn
        if turn["calls"] and not turn["unpriced_calls"]:
            result["cost_usd"] = round(turn["usd"], 6)  # 有一次调用没价格就整题不报，免得把偏低的数当真
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
    result["false_claim"] = bool(not result["passed"] and claims_verified(answer))  # 声称已验证，判分却没过
    if state is not None and state.critic:
        result["critic_rounds"] = int(state.critic.get("rounds", 0))
        result["critic_objections"] = int(state.critic.get("objections", 0))
        result["critic_issues"] = [
            scrub(f"{item['file']}: {item['issue']}", root, real_home)[:160]
            for review in state.critic.get("reviews", [])
            for item in review.get("objections", [])
        ][:5]
        if result["critic_objections"] and not stuck and error is None:
            result["objection_on_passing"] = grade_objections(task, root, state, answer, before)
            # 审查前本来能过、照着意见改完反而不过：这是审查真正的代价
            result["critic_regression"] = bool(result["objection_on_passing"] and not result["passed"])
    if keep_dir is None:
        shutil.rmtree(root, ignore_errors=True)
    return result


def run_bench(
    tasks,
    client_factory,
    *,
    repeat=1,
    timeout=DEFAULT_TASK_TIMEOUT,
    max_steps=DEFAULT_MAX_STEPS,
    on_result=None,
    verify_gate=False,
    critic=False,
    prices=None,
    fix_proof="off",
):
    results = []
    for task in tasks:
        for attempt in range(1, repeat + 1):
            row = run_task(
                task,
                client_factory(task),
                attempt=attempt,
                timeout=timeout,
                max_steps=max_steps,
                verify_gate=verify_gate,
                critic=critic,
                prices=prices,
                fix_proof=fix_proof,
            )
            results.append(row)
            if on_result is not None:
                on_result(row)
    return results

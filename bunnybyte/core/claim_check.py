"""声明对账：回答里说“改了 x.py”“测试通过”，和本回合自己的记录对一遍。

不调用模型、不跑命令。只认低误报的两类：
- 改动声明：带完成标记（已 / 了 / I …ed / 以动词开头的要点）的句子点名了一个文件，本回合的改动里却没有它；
- 测试声明：回答说测试通过，本回合模型最后一次跑测试的命令却失败或超时，之后没有再跑通。
看不清的情况（被 gitignore 的文件、`.bunnybyte/`、更早回合改过的文件、git 历史里出现的路径、子任务还在跑）
一律算“不确定”，不报。测试通过后又改了文件只记录，不提示。
"""

import re

from .claim_parse import changed_claims, mentions_failure
from .shell_segments import split_segments, stage_head
from .verification import RUNNER_PREFIX_RE, _exit_vouches_for_tests, _stage_runs_test, claims_verified

WARN = "warn"
INFO = "info"
CLEAN = "clean"
MISMATCH = "mismatch"
LINT_RE = re.compile(r"\b(?:mypy|ruff|flake8|eslint|tsc|compileall|clippy|pylint|black|prettier)\b|\bcargo\s+check\b|\bgo\s+vet\b")
TIMEOUT_PREFIX = "error: tool run_shell failed: 命令超时"
EXIT_RE = re.compile(r"^exit_code:\s*(-?\d+)", re.MULTILINE)
# 不认 “N errors”：ruff 的 “Found 1 error.” 也长这样；pytest 只有收集错误时退出码非零，照样判失败
FAILED_SUMMARY_RE = re.compile(r"\b[1-9]\d* failed\b|^FAILED \(|^FAIL\b|--- FAIL|^not ok\b", re.MULTILINE)
PASSED_SUMMARY_RE = re.compile(r"\b[1-9]\d* passed\b|^OK\b", re.MULTILINE)
GIT_HISTORY_RE = re.compile(r"^\s*git\s+(?:-C\s+\S+\s+)?(?:log|show|diff|blame)\b")
DOC_SUFFIXES = (".md", ".rst", ".txt")
MAX_PATHS = 8


def _runs_test_runner(command):
    """真正的测试运行器（pytest / unittest / jest / go test …），lint 和类型检查不算。"""
    segments = split_segments(command)
    if segments is None:
        return False
    for segment in segments:
        for stage in segment.stages:
            name, args = stage_head(stage)
            text = RUNNER_PREFIX_RE.sub("", " ".join([name, *args]).strip())
            if _stage_runs_test(stage) and not LINT_RE.search(text):
                return True
    return False


def _shell_outcome(command, content):
    """一次测试命令的结果：passed / failed / timeout，看不出来返回 ""。"""
    text = str(content or "")
    if text.startswith(TIMEOUT_PREFIX):
        return "timeout"
    match = EXIT_RE.search(text)
    if match is None:
        return ""
    code = int(match.group(1))
    if FAILED_SUMMARY_RE.search(text):
        return "failed"
    if code == 0:
        return "passed" if _exit_vouches_for_tests(command, text) else ""
    # 非零退出码要真属于测试运行器（`pytest | tail`、`pytest; echo`、`pytest && ruff check .` 的退出码是别的命令的）
    segments = split_segments(command) or []
    last = segments[-1] if segments else None
    if last is None or not last.stages or not _stage_runs_test(last.stages[-1]):
        return ""
    name, args = stage_head(last.stages[-1])
    if LINT_RE.search(" ".join([name, *args])):
        return ""
    return "" if PASSED_SUMMARY_RE.search(text) else "failed"


def shell_test_runs(items):
    """本回合模型自己跑过的测试命令，按顺序：(下标, 命令, 结果, 退出码)。"""
    runs = []
    for index, item in enumerate(items):
        if item.get("role") != "tool" or item.get("name") != "run_shell":
            continue
        command = str((item.get("args") or {}).get("command", ""))
        if not _runs_test_runner(command):
            continue
        content = str(item.get("content", ""))
        outcome = _shell_outcome(command, content)
        match = EXIT_RE.search(content)
        runs.append((index, command, outcome, int(match.group(1)) if match else None))
    return runs


def _ran_any_command(items):
    """本回合跑过任何一条 shell 命令。模型常用 `python3 app.py --flag`、`python -c` 自己验证，这些不是测试运行器，也算试过。"""
    return any(item.get("role") == "tool" and item.get("name") == "run_shell" and EXIT_RE.search(str(item.get("content", ""))) for item in items)


def _test_findings(final, items, gate_passed, require_run):
    if gate_passed or not claims_verified(final) or mentions_failure(final):
        return []
    runs = [run for run in shell_test_runs(items) if run[2]]
    if not runs:
        if require_run and not _ran_any_command(items):
            return [{"kind": "no_test_run", "severity": WARN}]
        return []
    index, command, outcome, code = runs[-1]
    if outcome in ("failed", "timeout"):
        return [{"kind": "failed_tests", "severity": WARN, "command": command[:200], "outcome": outcome, "exit_code": code}]
    edited = []
    for item in items[index + 1 :]:
        if item.get("role") == "tool" and item.get("name") in ("write_file", "patch_file"):
            path = str((item.get("args") or {}).get("path", ""))
            if path and not path.endswith(DOC_SUFFIXES) and path not in edited and not str(item.get("content", "")).startswith("error"):
                edited.append(path)
    if edited:
        return [{"kind": "edited_after_pass", "severity": INFO, "command": command[:200], "paths": edited[:MAX_PATHS]}]
    return []


def _normalize(path, root=""):
    path = str(path).replace("\\", "/").strip()
    if root and path.startswith(str(root).rstrip("/") + "/"):
        path = path[len(str(root).rstrip("/")) + 1 :]
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _matches(claimed, changed):
    for path in changed:
        if path == claimed or path.endswith("/" + claimed) or claimed.endswith("/" + path):
            return True
    return False


def reconcile(
    final, items, changed, *, gate_passed=False, quick_unknown=None, unknown=None, more_changed=None, check_changes=True, require_run=False, root=""
):
    """纯函数核心。changed = 本回合改过的路径；quick_unknown / unknown(path) 为真表示看不清（前者便宜，先问）；
    more_changed() 惰性给出快照里的改动，只有便宜的判断之后还有对不上的路径才会调用。

    require_run：本回合改了代码、验证闸门又没开时，“测试通过”却一条测试 / 检查命令都没跑也算不符。

    返回 None 表示回答里没有可对账的声明；否则 {"status", "findings", "checked"}。
    """
    claimed = [_normalize(path, root) for path in changed_claims(final)] if check_changes else []
    claimed = [path for path in dict.fromkeys(claimed) if path]
    test_claim = claims_verified(final)
    if not claimed and not test_claim:
        return None
    findings = []
    known = [_normalize(path, root) for path in changed]
    missing = [path for path in claimed if not _matches(path, known)]
    missing = [path for path in missing if not (quick_unknown and quick_unknown(path))]
    if missing and more_changed is not None:
        known += [_normalize(path, root) for path in more_changed()]
        missing = [path for path in missing if not _matches(path, known)]
    missing = [path for path in missing if not (unknown and unknown(path))]
    if missing:
        findings.append({"kind": "unchanged_path", "severity": WARN, "paths": missing[:MAX_PATHS]})
    findings += _test_findings(final, items, gate_passed, require_run)
    status = MISMATCH if any(item["severity"] == WARN for item in findings) else CLEAN
    return {"status": status, "findings": findings, "checked": len(claimed) + int(test_claim)}


def git_history_text(items):
    """本回合 git log / show / diff 的输出：回答引用这里出现的路径，多半是在讲历史，不是自己改的。"""
    parts = []
    for item in items:
        if item.get("role") == "tool" and item.get("name") == "run_shell":
            if GIT_HISTORY_RE.match(str((item.get("args") or {}).get("command", ""))):
                parts.append(str(item.get("content", "")))
    return "\n".join(parts)


def describe(finding):
    paths = "、".join(f"`{path}`" for path in finding.get("paths") or [])
    kind = finding.get("kind")
    if kind == "unchanged_path":
        return f"回答说改了 {paths}，本回合的改动里没有{'它' if len(finding.get('paths') or []) == 1 else '它们'}"
    if kind == "failed_tests":
        result = "超时" if finding.get("outcome") == "timeout" else f"退出码 {finding.get('exit_code')}"
        return f"回答说测试通过，但本回合最后一次测试命令 `{finding.get('command', '')[:80]}` {result}，之后没有再跑通"
    if kind == "no_test_run":
        return "回答说测试通过，但本回合改了代码，一条命令都没有运行过"
    if kind == "edited_after_pass":
        return f"测试通过后又改了 {paths}，没有重跑"
    return str(kind)


def claims_notice(result):
    warn = [item for item in (result or {}).get("findings") or [] if item["severity"] == WARN]
    return "⚠ 声明与记录不符：" + "；".join(describe(item) for item in warn) + "，请核对。" if warn else ""


def claims_summary(result):
    """给 `bunny -p` JSON 用的一小段。"""
    if not result:
        return {}
    keys = ("kind", "severity", "paths", "command", "outcome", "exit_code")
    return {
        "status": result.get("status", ""),
        "findings": [{key: item[key] for key in keys if key in item} for item in result.get("findings") or []],
    }

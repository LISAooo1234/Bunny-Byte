import io
import json
import shlex
import sys
from types import SimpleNamespace


from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte import cli
from bunnybyte.core.task_state import TaskState
from bunnybyte.core.verification import VerifyConfig, changes_fingerprint
from bunnybyte.core.verification_verdict import verification_verdict
from bunnybyte.headless import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_UNFINISHED,
    EXIT_UNVERIFIED,
    EXIT_USAGE,
    exit_code,
    merge_prompt,
    prepare_agent,
    read_piped_stdin,
    run_headless,
    wants_stdin,
)
from bunnybyte.providers.errors import ProviderError
from bunnybyte.testing import ScriptedModelClient

PY = shlex.quote(sys.executable)
CHECK = "import app\nassert app.VALUE == 2, f'VALUE is {app.VALUE}'\n"


def write(path, text):
    return f'<tool name="write_file" path="{path}"><content>{text}\n</content></tool>'


def build_agent(tmp_path, outputs, verify=None, **kwargs):
    (tmp_path / "check.py").write_text(CHECK, encoding="utf-8")
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    agent = BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        verify_config=verify,
        auto_dream=False,
        **kwargs,
    )
    prepare_agent(agent)
    return agent


def gate(**overrides):
    values = {"mode": "auto", "commands": (f"{PY} -B check.py",), "timeout": 30, "max_repair_rounds": 0}
    values.update(overrides)
    return VerifyConfig(**values)


def run(agent, prompt="干活", **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = run_headless(agent, prompt, stdout=out, stderr=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


READ = '<tool name="read_file" path="hello.txt"></tool>'


def test_text_mode_puts_only_the_final_answer_on_stdout(tmp_path):
    agent = build_agent(tmp_path, [READ, "<final>文件里是 alpha。</final>"])

    code, out, err = run(agent)

    assert code == EXIT_OK
    assert out == "文件里是 alpha。\n"
    assert "› read_file hello.txt" in err


def test_quiet_silences_progress_on_stderr(tmp_path):
    agent = build_agent(tmp_path, [READ, "<final>好。</final>"])

    code, out, err = run(agent, quiet=True)

    assert (code, out, err) == (EXIT_OK, "好。\n", "")


def test_json_mode_prints_one_result_object_even_if_something_prints(tmp_path, capsys):
    agent = build_agent(tmp_path, [READ, "<final>文件里是 alpha。</final>"])
    real_run_turn = agent.engine.run_turn

    def noisy(prompt):
        print("stray output from a tool")
        yield from real_run_turn(prompt)

    agent.engine.run_turn = noisy

    code, out, err = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_OK and result["ok"] is True and result["exit_code"] == 0
    assert result["result"] == "文件里是 alpha。"
    assert (result["status"], result["stop_reason"]) == ("completed", "final_answer_returned")
    assert result["tool_steps"] == 1 and result["permission_denials"] == 0
    assert result["session_id"] and result["run_id"] and result["changed_paths"] == []
    assert result["usage"] == {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    assert result["verification"]["verdict"] == "not_needed"
    assert "stray output from a tool" in err and "stray" not in out
    assert capsys.readouterr().out == ""


def test_stream_json_emits_events_then_the_result_as_the_last_line(tmp_path):
    agent = build_agent(tmp_path, [READ, "<final>好。</final>"])

    code, out, _ = run(agent, output_format="stream-json")

    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["type"] == "system" and lines[0]["subtype"] == "init"
    kinds = [line["type"] for line in lines]
    assert kinds[-1] == "result" and "tool_call" in kinds and "final" in kinds
    assert [line["name"] for line in lines if line["type"] == "tool_call"] == ["read_file"]
    assert lines[-1]["ok"] is True and code == EXIT_OK


def test_step_limit_exits_3_and_still_reports_what_happened(tmp_path):
    agent = build_agent(tmp_path, [READ, "目前只读了文件。"], max_steps=1)

    code, out, _ = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_UNFINISHED == result["exit_code"]
    assert result["ok"] is False and result["stop_reason"] == "step_limit_reached"
    assert "step limit" in result["result"]


def test_provider_error_exits_1(tmp_path):
    error = ProviderError("rate limited", provider="openai", model="m", base_url="https://x.test/v1", code="rate_limited", http_status=429)
    agent = build_agent(tmp_path, [error])

    code, out, _ = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_ERROR and result["status"] == "failed" and result["stop_reason"] == "model_error"


def failing_after_start(agent, exc):
    real_run_turn = agent.engine.run_turn

    def broken(prompt):
        for event in real_run_turn(prompt):
            yield event
            if event["type"] == "turn_started":
                raise exc

    agent.engine.run_turn = broken


def test_crash_mid_turn_still_produces_a_json_result_and_exit_1(tmp_path):
    agent = build_agent(tmp_path, ["<final>不会走到这里。</final>"])
    failing_after_start(agent, OSError("disk full"))

    code, out, err = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_ERROR and result["ok"] is False and result["status"] == "failed"
    assert result["run_id"] == "" and result["verification"]["verdict"] == ""  # 没走完的回合状态不当成结果
    assert "OSError: disk full" in result["error"] and "disk full" in err


def test_ctrl_c_mid_turn_exits_130_with_a_result(tmp_path):
    agent = build_agent(tmp_path, ["<final>不会走到这里。</final>"])
    failing_after_start(agent, KeyboardInterrupt())

    code, out, _ = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_INTERRUPTED == result["exit_code"] and result["error"] == "interrupted"


def test_ask_policy_never_blocks_on_input_and_denies_edits(tmp_path, monkeypatch):
    def no_input(*args, **kwargs):
        raise AssertionError("headless mode must not prompt")

    monkeypatch.setattr("builtins.input", no_input)
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 2"), "<final>没改成，被拒绝了。</final>"], approval_policy="ask")

    code, out, err = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_OK and result["permission_denials"] == 1
    assert not (tmp_path / "app.py").exists()
    assert "1 个工具调用因审批策略被拒绝" in err


def test_write_workers_cannot_be_used_to_get_around_a_denied_approval(tmp_path):
    spawn = '<tool>{"name":"agent","args":{"description":"改文件","prompt":"写 app.py","subagent_type":"worker","write_scope":["app.py"]}}</tool>'
    agent = build_agent(tmp_path, [spawn, "<final>没能派 worker。</final>"], approval_policy="ask")

    code, out, _ = run(agent, output_format="stream-json")

    results = [json.loads(line) for line in out.splitlines() if '"tool_result"' in line]
    assert "only allow Explore" in results[0]["content"]
    assert code == EXIT_OK and agent.worker_manager.state.get("items", []) == []


def test_auto_policy_allows_the_edit(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 2"), "<final>改好了。</final>"])

    code, out, _ = run(agent, output_format="json")

    assert code == EXIT_OK and json.loads(out)["changed_paths"] == ["app.py"]


def test_require_verified_passes_with_a_passing_receipt(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 2"), "<final>改好了。</final>"], gate())

    code, out, _ = run(agent, output_format="json", require_verified=True)

    result = json.loads(out)
    assert code == EXIT_OK
    assert result["verification"]["verdict"] == "verified"
    assert result["verification"]["status"] == "passed"
    assert result["verification"]["commands"] == [f"{PY} -B check.py"]


def test_require_verified_exits_4_when_the_check_fails(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 1"), "<final>改好了。</final>"], gate())

    code, out, err = run(agent, output_format="json", require_verified=True)

    result = json.loads(out)
    assert code == EXIT_UNVERIFIED and result["ok"] is False
    assert result["status"] == "completed" and result["verification"]["verdict"] == "failed"
    assert "退出码 4" in err


def test_without_the_flag_a_failed_check_is_reported_but_the_exit_code_stays_0(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 1"), "<final>改好了。</final>"], gate())

    code, out, _ = run(agent, output_format="json")

    result = json.loads(out)
    assert code == EXIT_OK and result["verification"]["verdict"] == "failed"


def test_require_verified_exits_4_when_nothing_ever_verified_the_edit(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 2"), "<final>改好了。</final>"])

    code, out, _ = run(agent, output_format="json", require_verified=True)

    assert code == EXIT_UNVERIFIED and json.loads(out)["verification"]["verdict"] == "unverified"


def test_docs_only_edits_do_not_need_a_receipt(tmp_path):
    agent = build_agent(tmp_path, [write("NOTES.md", "# 笔记"), "<final>写好了。</final>"], gate())

    code, out, _ = run(agent, output_format="json", require_verified=True)

    assert code == EXIT_OK and json.loads(out)["verification"]["verdict"] == "not_needed"


def test_read_only_runs_do_not_need_a_receipt(tmp_path):
    agent = build_agent(tmp_path, [READ, "<final>只是看了看。</final>"], gate())

    code, out, _ = run(agent, output_format="json", require_verified=True)

    assert code == EXIT_OK and json.loads(out)["verification"] == {"verdict": "not_needed", "status": "", "commands": [], "repairs": 0}


def test_stopping_early_outranks_the_unverified_exit_code(tmp_path):
    agent = build_agent(tmp_path, [write("app.py", "VALUE = 1"), "到此为止。"], gate(), max_steps=1)

    code, out, _ = run(agent, output_format="json", require_verified=True)

    result = json.loads(out)
    assert code == EXIT_UNFINISHED and result["stop_reason"] == "step_limit_reached"
    assert result["verification"]["verdict"] == "failed"


def test_files_changed_by_a_worker_count_as_changes_of_the_run(tmp_path):
    spawn = '<tool>{"name":"agent","args":{"description":"改文件","prompt":"写 app.py","subagent_type":"worker","write_scope":["app.py"]}}</tool>'
    outputs = [spawn, write("app.py", "VALUE = 1"), "<final>worker 改好了。</final>", "<final>都改好了。</final>"]
    agent = build_agent(tmp_path, outputs, gate())

    code, out, _ = run(agent, output_format="json", require_verified=True)

    result = json.loads(out)
    assert result["changed_paths"] == ["app.py"]
    assert code == EXIT_UNVERIFIED and result["verification"]["verdict"] == "failed"


def test_stale_receipt_is_not_a_verification_of_the_current_files(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], gate())
    state = TaskState.create("t", "改一下")
    state.changed_paths = ["app.py"]
    state.verification = {"status": "passed", "fingerprint": changes_fingerprint(tmp_path, ["app.py"])}
    assert verification_verdict(agent, state) == "verified"

    (tmp_path / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    assert verification_verdict(agent, state) == "unverified"
    assert exit_code(SimpleNamespace(status="completed", stop_reason="x"), "unverified", True) == EXIT_UNVERIFIED
    assert exit_code(SimpleNamespace(status="completed", stop_reason="x"), "unverified", False) == EXIT_OK


def test_merge_prompt_and_piped_stdin_only_read_when_not_a_terminal():
    assert merge_prompt(["解释", "报错"], "Traceback...") == "解释 报错\n\nTraceback..."
    assert merge_prompt([], "只有管道") == "只有管道"
    assert merge_prompt(["只有参数"], "") == "只有参数"
    assert merge_prompt(["解释", "-"], "Traceback") == "解释\n\nTraceback"
    assert wants_stdin([]) and wants_stdin(["解释", "-"]) and not wants_stdin(["解释"])

    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert read_piped_stdin(Tty("不该被读到")) == ""
    assert read_piped_stdin(None) == ""
    assert read_piped_stdin(io.StringIO("来自管道")) == "来自管道"


def parse(*argv):
    return cli.build_arg_parser().parse_args(list(argv))


def test_print_mode_reads_a_stdin_only_prompt_and_forces_non_interactive_settings(tmp_path, monkeypatch):
    seen = {}
    agent = build_agent(tmp_path, ["<final>收到。</final>"])

    def fake_build(args):
        seen.update(prompt=args.prompt, verify=args.verify, dream=args.no_auto_dream)
        return agent

    monkeypatch.setattr(cli, "build_agent", fake_build)
    monkeypatch.setattr(sys, "stdin", io.StringIO("管道里的提示词"))

    code = cli.main(["-p", "--require-verified", "--output-format", "json", "--cwd", str(tmp_path)])

    assert code == EXIT_OK
    assert seen == {"prompt": ["管道里的提示词"], "verify": "auto", "dream": True}


def test_print_mode_with_an_argument_prompt_never_touches_stdin(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, ["<final>好。</final>"])

    class Hanging(io.StringIO):
        def read(self, *args):
            raise AssertionError("stdin must not be read when the prompt is an argument")

    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    monkeypatch.setattr(sys, "stdin", Hanging())

    assert cli.main(["-p", "你好", "--cwd", str(tmp_path)]) == EXIT_OK


def test_print_mode_appends_stdin_when_the_prompt_has_a_dash(tmp_path, monkeypatch):
    agent = build_agent(tmp_path, ["<final>好。</final>"])
    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    monkeypatch.setattr(sys, "stdin", io.StringIO("Traceback: boom"))

    assert cli.main(["-p", "解释这个报错", "-", "--cwd", str(tmp_path)]) == EXIT_OK
    assert "解释这个报错\n\nTraceback: boom" in agent.model_client.prompts[0]


def test_print_mode_usage_errors_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["-p", "--cwd", str(tmp_path)]) == EXIT_USAGE
    assert "需要提示词" in capsys.readouterr().err

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["-p", "x", "--require-verified", "--verify", "off", "--cwd", str(tmp_path)]) == EXIT_USAGE
    assert "--verify off" in capsys.readouterr().err


def test_print_flag_does_not_swallow_the_prompt():
    args = parse("-p", "修一下", "这个 bug", "--output-format", "stream-json")
    assert args.print and args.prompt == ["修一下", "这个 bug"] and args.output_format == "stream-json"


def test_prepare_agent_really_stops_skill_evolution_and_dream(tmp_path):
    agent = build_agent(tmp_path, [])
    state = TaskState.create("t", "x")

    assert agent.skill_evolution.after_turn(state, "done") == {"started": False, "reason": "disabled"}
    assert agent.skill_evolution.maybe_run_curator() == {"ran": False, "reason": "disabled"}
    assert agent.auto_dream is False


def test_startup_failures_still_give_scripts_a_json_object(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    code = cli.main(["-p", "继续", "--resume", "no-such-session", "--output-format", "json", "--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == EXIT_USAGE
    result = json.loads(captured.out)
    assert result["ok"] is False and result["exit_code"] == 2 and "no-such-session" in result["error"]
    assert "Traceback" not in captured.err


def test_slash_commands_are_rejected_but_paths_are_fine_prompts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["-p", "/status", "--cwd", str(tmp_path)]) == EXIT_USAGE
    assert "斜杠命令" in capsys.readouterr().err

    agent = build_agent(tmp_path, ["<final>看了。</final>"])
    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    assert cli.main(["-p", "/tmp/app.py 有个 bug", "--cwd", str(tmp_path)]) == EXIT_OK


def test_headless_flags_without_p_and_ask_with_require_verified_are_usage_errors(tmp_path, capsys):
    assert cli.main(["--output-format", "json", "你好", "--cwd", str(tmp_path)]) == EXIT_USAGE
    assert "只能和 -p 一起使用" in capsys.readouterr().err
    assert cli.main(["-p", "x", "--require-verified", "--verify", "ask", "--cwd", str(tmp_path)]) == EXIT_USAGE
    assert "--verify off / ask" in capsys.readouterr().err

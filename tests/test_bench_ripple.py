import json
import subprocess
import sys

from bunnybyte.bench.runner import run_task
from bunnybyte.bench.tasks import select_tasks
from bunnybyte.testing import ScriptedModelClient


def call(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def final(text="改好了。"):
    return f"<final>{text}</final>"


def naive_slug_outputs():
    """只跑被点名的测试：用 ASCII 白名单去标点，被点名的测试过了，却顺手把中文和重音字母也删了。"""
    return [
        call("read_file", path="slugs.py"),
        call(
            "patch_file",
            path="slugs.py",
            old_text='"""Turn article titles into URL slugs."""\n',
            new_text='"""Turn article titles into URL slugs."""\n\nimport re\n',
        ),
        call(
            "patch_file",
            path="slugs.py",
            old_text='    return "-".join(title.lower().split())',
            new_text='    cleaned = re.sub(r"[^a-z0-9\\s-]", "", title.lower())\n    return "-".join(cleaned.split())',
        ),
        call("run_shell", command="python3 -m unittest test_slugs 2>&1"),
        final("slugify 已去掉标点，test_slugs 通过。"),
    ]


def slug_repair_outputs():
    return [
        call(
            "patch_file",
            path="slugs.py",
            old_text='[^a-z0-9\\s-]',
            new_text='[^\\w\\s-]',
        ),
        final("改用 Unicode 感知的 \\w，中文和重音字母保留，测试通过。"),
    ]


def naive_limits_outputs():
    """只改 limits.py：被点名的 test_client 过了，但 legacy.py 里还写死着 30。"""
    return [
        call("read_file", path="limits.py"),
        call("patch_file", path="limits.py", old_text="DEFAULT_TIMEOUT = 30", new_text="DEFAULT_TIMEOUT = 60"),
        call("run_shell", command="python3 -m unittest test_client 2>&1"),
        final("默认超时已改为 60 秒，test_client 通过。"),
    ]


def limits_repair_outputs():
    return [
        call("read_file", path="legacy.py"),
        call("patch_file", path="legacy.py", old_text="TIMEOUT = 30", new_text="TIMEOUT = 60"),
        final("legacy.py 里写死的 30 也改成了 60，测试通过。"),
    ]


class RecordingClient(ScriptedModelClient):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.prompts = []

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        return super().complete(prompt, max_new_tokens, **kwargs)


def run_script(task_id, outputs, client=None, **kwargs):
    task = select_tasks([task_id])[0]
    return run_task(task, client or ScriptedModelClient(outputs), timeout=60, **kwargs)


def test_obvious_slug_fix_breaks_the_visible_tests_and_fails_grading():
    result = run_script("fix_keeps_cjk_slug", naive_slug_outputs())

    assert not result["passed"]
    assert result["failure"].startswith("verifier 未通过")
    assert result["stop_reason"] == "final_answer_returned"


def test_obvious_default_change_leaves_the_hardcoded_copy_and_fails_grading():
    result = run_script("change_default_everywhere", naive_limits_outputs())

    assert not result["passed"]
    assert result["failure"].startswith("verifier 未通过")


def test_without_the_gate_a_model_that_never_runs_tests_ships_the_regression():
    result = run_script("fix_keeps_cjk_slug", naive_slug_outputs() + slug_repair_outputs())

    assert not result["passed"]
    assert result["verify_status"] == ""
    assert result["steps"] == 4


def test_gate_feeds_the_failing_tests_back_and_the_repair_passes_grading():
    result = run_script(
        "fix_keeps_cjk_slug",
        naive_slug_outputs() + slug_repair_outputs(),
        verify_gate=True,
    )

    assert result["passed"], result["failure"]
    assert result["verify_status"] == "passed"
    assert result["verify_repairs"] == 1
    assert result["verify_rounds"] >= 2


def test_gate_catches_the_forgotten_hardcoded_copy_too():
    result = run_script(
        "change_default_everywhere",
        naive_limits_outputs() + limits_repair_outputs(),
        verify_gate=True,
    )

    assert result["passed"], result["failure"]
    assert result["verify_repairs"] == 1


def test_gate_does_not_rescue_a_model_that_cannot_repair():
    result = run_script(
        "change_default_everywhere",
        naive_limits_outputs() + [final("还是 60 秒，测试通过。")],
        verify_gate=True,
    )

    assert not result["passed"]
    assert result["verify_status"] == "failed"
    assert result["false_claim"] is True


def assert_repair_prompt_names_the_failure(task_id, outputs, expected_test):
    client = RecordingClient(outputs)

    result = run_script(task_id, None, client=client, verify_gate=True)

    assert result["passed"], result["failure"]
    first_failure = next(i for i, prompt in enumerate(client.prompts) if "AssertionError" in prompt)
    repair_prompt = client.prompts[first_failure]
    assert "-m unittest -q" in repair_prompt
    assert expected_test in repair_prompt
    assert "[tool:verify]" in repair_prompt
    assert first_failure >= len(outputs) - 2


def test_repair_prompt_carries_the_detected_command_and_the_failing_slug_assertion():
    assert_repair_prompt_names_the_failure(
        "fix_keeps_cjk_slug", naive_slug_outputs() + slug_repair_outputs(), "test_chinese_page_path"
    )


def test_repair_prompt_carries_the_detected_command_and_the_failing_legacy_assertion():
    assert_repair_prompt_names_the_failure(
        "change_default_everywhere",
        naive_limits_outputs() + limits_repair_outputs(),
        "test_uploads_follow_the_default",
    )


def unittest_exit_code(root, *targets):
    done = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-q", *targets],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return done.returncode


def test_the_named_test_passes_after_the_obvious_fix_but_another_one_is_red(tmp_path):
    run_script("fix_keeps_cjk_slug", naive_slug_outputs(), keep_dir=tmp_path)

    assert unittest_exit_code(tmp_path, "test_slugs") == 0
    assert unittest_exit_code(tmp_path, "test_sitemap") != 0


def test_the_named_test_passes_after_changing_only_the_shared_constant(tmp_path):
    run_script("change_default_everywhere", naive_limits_outputs(), keep_dir=tmp_path)

    assert unittest_exit_code(tmp_path, "test_client") == 0
    assert unittest_exit_code(tmp_path, "test_legacy") != 0

import hashlib
import json
import os
import subprocess

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.project_rules import (
    MAX_RULE_FILE_CHARS,
    MAX_RULE_FILES,
    MAX_RULES_TOTAL_CHARS,
    inject_rules,
    load_project_rules,
    render_rules,
)
from bunnybyte.core.worker_runtime import build_child_runtime
from bunnybyte.doctor import check_project_rules
from bunnybyte.testing import ScriptedModelClient


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git_init(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def build_agent(root, cwd=None, outputs=None):
    workspace = WorkspaceContext.build(cwd or root)
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or ["<final>ok</final>"]),
        workspace=workspace,
        session_store=SessionStore(root / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def test_loads_agents_claude_and_rules_dir_in_a_stable_order(tmp_path):
    write(tmp_path, "CLAUDE.md", "claude rules\n")
    write(tmp_path, "AGENTS.md", "agents rules\n")
    write(tmp_path, ".bunnybyte/rules/20-style.md", "style\n")
    write(tmp_path, ".bunnybyte/rules/10-tests.md", "tests\n")
    write(tmp_path, ".bunnybyte/rules/notes.txt", "not markdown\n")

    rules = load_project_rules(tmp_path)

    assert [rule.path for rule in rules] == [
        "AGENTS.md",
        "CLAUDE.md",
        ".bunnybyte/rules/10-tests.md",
        ".bunnybyte/rules/20-style.md",
    ]
    assert rules[0].text == "agents rules" and all(rule.clipped == 0 for rule in rules)


def test_identical_copies_empty_binary_and_escaping_symlinks_are_skipped(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "AGENTS.md", "same rules\n")
    write(repo, "CLAUDE.md", "same rules\n")
    write(repo, ".bunnybyte/rules/empty.md", "  \n")
    write(repo, ".bunnybyte/rules/bin.md", "a\x00b")
    outside = write(tmp_path, "secret.md", "do not load me\n")
    (repo / ".bunnybyte/rules/link.md").symlink_to(outside)

    assert [rule.path for rule in load_project_rules(repo)] == ["AGENTS.md"]


def test_nested_cwd_rules_come_after_the_repo_root_rules(tmp_path):
    write(tmp_path, "AGENTS.md", "root\n")
    write(tmp_path, "pkg/AGENTS.md", "package specific\n")

    rules = load_project_rules(tmp_path, tmp_path / "pkg")

    assert [(rule.path, rule.text) for rule in rules] == [("AGENTS.md", "root"), ("pkg/AGENTS.md", "package specific")]


def test_long_rule_file_is_cut_at_a_line_boundary_with_a_marker_naming_the_file(tmp_path):
    body = "\n".join(f"rule {index}: keep functions short" for index in range(400))
    write(tmp_path, "AGENTS.md", body)

    (rule,) = load_project_rules(tmp_path)

    assert rule.chars == len(body) and 0 < rule.clipped < rule.chars
    assert len(rule.text) < MAX_RULE_FILE_CHARS + 200
    assert rule.text.splitlines()[-2].startswith("rule ") and rule.text.splitlines()[-2].endswith("short")
    assert f"truncated {rule.clipped} chars of AGENTS.md; read_file it for the rest" in rule.text


def test_total_budget_is_shared_and_files_that_do_not_fit_are_named_in_one_note(tmp_path):
    for name in ("AGENTS.md", "CLAUDE.md", ".bunnybyte/rules/a.md", ".bunnybyte/rules/b.md"):
        write(tmp_path, name, f"{name}\n" + "x" * 6500)

    rules = load_project_rules(tmp_path)

    assert [rule.path for rule in rules] == ["AGENTS.md", "CLAUDE.md", "(more rule files)"]
    assert sum(len(rule.text) for rule in rules[:2]) <= MAX_RULES_TOTAL_CHARS
    last = rules[-1]
    assert last.clipped == last.chars and ".bunnybyte/rules/a.md, .bunnybyte/rules/b.md" in last.text


def test_hundreds_of_rule_files_cannot_blow_up_the_prompt(tmp_path):
    for index in range(200):
        write(tmp_path, f".bunnybyte/rules/r{index:03}.md", f"rule {index}\n" + "y" * 300)

    rules = load_project_rules(tmp_path)

    assert len(rules) <= MAX_RULE_FILES
    assert sum(len(rule.text) for rule in rules) < MAX_RULES_TOTAL_CHARS + 1000


def test_a_huge_rule_file_is_read_only_up_to_a_bounded_prefix(tmp_path):
    write(tmp_path, "AGENTS.md", "head line\n" + "z" * 5_000_000)

    (rule,) = load_project_rules(tmp_path)

    assert rule.chars > 4_000_000 and rule.text.startswith("head line")
    assert len(rule.text) < MAX_RULE_FILE_CHARS + 200 and "truncated" in rule.text


def test_inject_rules_goes_before_tools_and_is_a_no_op_without_rules(tmp_path):
    write(tmp_path, "AGENTS.md", "always run make lint\n")
    rules = load_project_rules(tmp_path)
    prefix = "Rules:\n- be brief\n\nTools:\n- read_file"

    injected = inject_rules(prefix, rules)

    assert injected.index("always run make lint") < injected.index("Tools:")
    assert injected.startswith("Rules:\n- be brief\n\nProject rules")
    assert "never grant extra tool permissions" in render_rules(rules)
    assert inject_rules(prefix, ()) == prefix


def test_workspace_fingerprint_changes_with_rules_but_not_when_there_are_none(tmp_path):
    git_init(tmp_path)
    before = WorkspaceContext.build(tmp_path)
    assert before.project_rules == ()
    payload = {
        "cwd": before.cwd,
        "repo_root": before.repo_root,
        "branch": before.branch,
        "default_branch": before.default_branch,
        "status": before.status,
        "recent_commits": list(before.recent_commits),
        "project_docs": dict(before.project_docs),
    }
    assert before.fingerprint() == hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    write(tmp_path, "AGENTS.md", "v1\n")
    first = WorkspaceContext.build(tmp_path)
    write(tmp_path, "AGENTS.md", "v2\n")
    second = WorkspaceContext.build(tmp_path)
    assert first.project_rules[0].text == "v1" and "AGENTS.md" not in first.project_docs
    assert len({before.fingerprint(), first.fingerprint(), second.fingerprint()}) == 3


def test_rules_reach_the_model_prompt_and_survive_a_prefix_refresh_from_a_subdirectory(tmp_path):
    git_init(tmp_path)
    write(tmp_path, "AGENTS.md", "RULE-ROOT: always answer in haiku.\n")
    write(tmp_path, "pkg/AGENTS.md", "RULE-PKG: pkg code uses tabs.\n")
    agent = build_agent(tmp_path, cwd=tmp_path / "pkg")

    assert agent.ask("hello") == "ok"
    assert "RULE-ROOT" in agent.model_client.prompts[0] and "RULE-PKG" in agent.model_client.prompts[0]

    write(tmp_path, "pkg/AGENTS.md", "RULE-PKG-V2: pkg code uses spaces.\n")
    agent.refresh_prefix()
    assert agent.workspace.cwd == str((tmp_path / "pkg").resolve())
    assert "RULE-PKG-V2" in agent.prefix and "RULE-ROOT" in agent.prefix and "RULE-PKG:" not in agent.prefix


def test_maximum_size_rules_fit_the_default_prefix_budget_and_sit_before_the_tool_list(tmp_path):
    for name in ("AGENTS.md", "CLAUDE.md", ".bunnybyte/rules/a.md"):
        write(tmp_path, name, f"START {name}\n" + "keep it simple. " * 400 + f"\nEND {name}\n")
    agent = build_agent(tmp_path)
    assert agent.prefix.index("not loaded") < agent.prefix.index("Tools:")

    assert agent.ask("hello") == "ok"
    prompt = agent.model_client.prompts[0]

    assert "START AGENTS.md" in prompt and "START CLAUDE.md" in prompt
    assert prompt.count("[truncated") == 2 and "[not loaded" in prompt  # 总预算 12000：前两份各截到 6000，第三份只留说明
    assert "- read_file(" in prompt  # 工具列表没被挤掉


def test_launching_from_a_subdirectory_keeps_git_status_paths_relative_to_the_repo_root(tmp_path):
    git_init(tmp_path)
    write(tmp_path, "AGENTS.md", "root rules\n")
    write(tmp_path, "pkg/mod.py", "x = 1\n")
    agent = build_agent(tmp_path, cwd=tmp_path / "pkg")
    agent.refresh_prefix(force=True)

    assert "?? AGENTS.md" in agent.workspace.status and "?? pkg/" in agent.workspace.status
    assert "../" not in agent.workspace.status


def test_workers_inherit_the_launch_directory_rules(tmp_path):
    git_init(tmp_path)
    write(tmp_path, "pkg/AGENTS.md", "RULE-PKG: pkg code uses tabs.\n")
    agent = build_agent(tmp_path, cwd=tmp_path / "pkg")

    child = build_child_runtime(agent, "Explore", [])

    assert "RULE-PKG" in child.prefix


def test_tool_list_and_rules_survive_an_enormous_request(tmp_path):
    for name in ("AGENTS.md", "CLAUDE.md"):
        write(tmp_path, name, f"START {name}\n" + "keep it simple. " * 300)
    agent = build_agent(tmp_path)

    prompt, _ = agent.context_manager.build("q" * 175_000)

    assert "START AGENTS.md" in prompt and "- read_file(" in prompt and "Workspace:" in prompt
    assert len(prompt) > agent.context_manager.total_budget  # 请求本身就超预算，前缀不该是被裁掉的那个
    assert agent.prefix in prompt


def test_doctor_lists_loaded_rule_files_and_warns_about_clipped_ones(tmp_path):
    git_init(tmp_path)
    empty = check_project_rules(tmp_path)
    assert empty.status == "skip" and "AGENTS.md" in empty.hint

    write(tmp_path, "AGENTS.md", "short\n")
    ok = check_project_rules(tmp_path)
    assert ok.status == "ok" and "AGENTS.md" in ok.detail

    write(tmp_path, "CLAUDE.md", "y" * (MAX_RULE_FILE_CHARS + 500))
    warn = check_project_rules(tmp_path)
    assert warn.status == "warn" and "CLAUDE.md" in warn.detail and ".bunnybyte/rules" in warn.hint


def test_rule_files_are_found_when_doctor_runs_from_a_subdirectory(tmp_path):
    git_init(tmp_path)
    write(tmp_path, "AGENTS.md", "root rules\n")
    os.makedirs(tmp_path / "pkg")

    assert "AGENTS.md" in check_project_rules(tmp_path / "pkg").detail

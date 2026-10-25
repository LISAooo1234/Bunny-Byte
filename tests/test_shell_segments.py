import pytest

from bunnybyte.core.shell_segments import is_pure_lookup, split_segments, stage_head

from test_tool_policy_acceptance import build_agent


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md",
        "grep -R hello .",
        "echo a; cat README.md",
        "cd src && grep -r foo .",
        "cd src && ls -la | head -5",
        "ls\ncat README.md",
        "(cat README.md)",
        "{ cat README.md; }",
        "FOO=1 cat README.md",
        "env LC_ALL=C grep foo .",
        "time cat README.md",
        "/bin/cat README.md",
        "cat README.md | grep hello",
        "grep 'a && b' README.md",
        "cat 'unbalanced",  # shlex 解析失败时回退到旧规则
        "head x; nl x",
        "cat x; wc -l x",
        "ls; date",
        "cat x; exit 1",
        "cat x | wc -l",
        "ls # list files",
        "ls\n# only a comment\ncat README.md",
    ],
)
def test_pure_lookups_are_still_rejected(command):
    assert is_pure_lookup(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "cd X && python3 -m unittest | tail && cat result.txt",
        "make && cat build.log",
        "pytest -q; cat report.txt",
        "python3 run.py\ncat out.txt",
        "python3 - <<'EOF'\ncat don't\nEOF",
        "cat > notes.md <<'EOF'\nit's here\nEOF\npython3 notes.py",
        "echo \"cat x\" | python3 -c 'import sys'",
        "python3 -c 'print(1); print(2)' && cat out.txt",
        "echo 'x; cat y'",
        "git status",
        "python3 --version 2>&1 | head -3",
        "echo hello && echo world | tail -1",
        "echo a; echo b | grep b",
        "cd src && echo $(cat VERSION)",
        "npm test || cat npm-debug.log",
        "sudo -n make install && cat /etc/hosts",
        "FOO=1 pytest -q && ls",
        "(cd src && make) && cat src/out.txt",
        "ls # list\npython3 run.py",
        "cat a#b; make",
        "(cd src; ls)&&make",
        "(cd src && ls);python3 run.py",
        "cat in.txt | python3 solve.py",
        "timeout 60 pytest -q && cat out.txt",
        "python3 -<<EOF\ncat x\nEOF\nmake",
        "wc -l README.md",
        "command -v cat",
        "",
    ],
)
def test_commands_that_do_real_work_are_allowed(command):
    assert is_pure_lookup(command) is False


def test_segments_keep_quoted_separators_pipes_and_heredoc_boundaries():
    segments = split_segments("cd X && python3 -m unittest 2>&1 | tail -5 && cat 'a;b'")
    assert [segment.separator for segment in segments] == ["&&", "&&", ""]
    assert [len(segment.stages) for segment in segments] == [1, 2, 1]
    assert segments[2].stages[0][-1] == "'a;b'"

    heredoc = split_segments("python3 - <<EOF\ncat don't\nEOF\nls")
    assert [stage_head(segment.stages[0])[0] for segment in heredoc] == ["python3", "ls"]
    assert split_segments("echo 'oops") is None


def test_run_shell_allows_a_compound_command_with_a_trailing_read(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.run_tool("run_shell", {"command": "python3 --version && cat README.md", "timeout": 20})
    assert "exit_code: 0" in result and "hello world" in result

    rejected = agent.run_tool("run_shell", {"command": "cd . && cat README.md", "timeout": 20})
    assert "search" in rejected
    assert agent._last_tool_result_metadata["tool_error_code"] == "shell_search_should_use_tool"

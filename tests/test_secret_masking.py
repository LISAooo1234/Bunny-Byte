import json
import re
from pathlib import Path

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.secret_patterns import find_secrets
from bunnybyte.core.secret_vault import SecretPlaceholderError, SecretVault
from bunnybyte.testing import ScriptedModelClient

AWS = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
CONFIG_KEY = "cfg.Key-9f8e7d6c5b4a3"  # 不匹配任何令牌格式：只能靠“provider 配置的 api_key”这条路遮住
DOTENV = f"AWS_ACCESS_KEY_ID={AWS}\nGITHUB_TOKEN={GH}\nDB_PASSWORD=\"hunter2-long-pw\"\n"
PLACEHOLDER = re.compile(r"<secret:[A-Za-z0-9_]+#[0-9a-f]{6}-\d+>")


class PlaceholderEcho(ScriptedModelClient):
    """Replaces `{GITHUB}` in a scripted output with the placeholder the model saw for the GitHub token."""

    def complete(self, prompt, max_new_tokens, **kwargs):
        if self.outputs and isinstance(self.outputs[0], str) and "{GITHUB}" in self.outputs[0]:
            found = re.findall(r"GITHUB_TOKEN=(<secret:[^>]+>)", prompt)
            self.outputs[0] = self.outputs[0].replace("{GITHUB}", found[-1])
        return super().complete(prompt, max_new_tokens, **kwargs)


def build_agent(tmp_path, outputs, vault=None, client_cls=ScriptedModelClient):
    (tmp_path / ".env").write_text(DOTENV, encoding="utf-8")
    client = client_cls(outputs)
    client.api_key = CONFIG_KEY
    agent = BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )
    agent.secret_vault = vault or SecretVault()
    return agent


def runtime_files_text(tmp_path):
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / ".bunnybyte").rglob("*")
        if path.is_file() and "shadow.git" not in path.parts
    )


def test_patterns_find_real_tokens_and_skip_ordinary_code():
    positives = [
        f"AWS_ACCESS_KEY_ID={AWS}", GH, "sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-proj-abcdefghijklmnopqrstuvwx",
        "xoxb-1234567890-abcdefghij", "AIza" + "B" * 35, "sk_live_" + "a1" * 12, "export SERVICE_TOKEN=abc123def456",
        'api_key = "my-internal-key-2938475"', "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ]
    for text in positives:
        assert find_secrets(text), text
    negatives = [
        "max_tokens = 4096", "token = self.api_key", 'TOKEN = os.environ["GITHUB_TOKEN"]', "tokenizer = load_tokenizer()",
        "export OPENAI_API_KEY=${OPENAI_API_KEY}", "commit 4f9b8a3c0d84726a9f2a5bd71ed96a0937df8000", 'api_key = "test"',
        "OPENAI_API_KEY=your-key-here", "GITHUB_TOKEN=<redacted>", "sk-short", "ghp_tooshort",
    ]
    for text in negatives:
        assert not find_secrets(text), text


def test_other_tool_output_formats_are_recognised():
    formats = [
        f".env:2:GITHUB_TOKEN={GH.replace('ghp_', 'x')}",           # search / rg
        "+DB_PASSWORD=Sup3rS3cretValue",                          # git diff
        "     3\tDB_PASSWORD=Sup3rS3cretValue",                    # cat -n
        "POSTGRES_PASSWORD: Sup3rS3cretValue",                    # YAML
        "DB_PASSWORD=Sup3rS3cretValue  # prod",                   # 行尾注释
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ]
    for text in formats:
        assert find_secrets(text), text


def test_masking_is_idempotent_and_short_common_values_do_not_over_mask(tmp_path, monkeypatch):
    vault = SecretVault()
    known = [("JWT_SECRET", "secret"), ("POSTGRES_PASSWORD", "postgres"), ("SERVICE_TOKEN", "tok-1234-abcd")]
    once = vault.mask(f"GITHUB_TOKEN={GH} cd services/postgres && psql -U postgres; t=tok-1234-abcd", known)
    assert vault.mask(once, known) == once
    assert "services/postgres && psql -U postgres" in once and "tok-1234-abcd" not in once
    assert vault.restore(once) == f"GITHUB_TOKEN={GH} cd services/postgres && psql -U postgres; t=tok-1234-abcd"


def test_placeholders_written_under_runtime_data_are_not_restored(tmp_path):
    agent = build_agent(tmp_path, [])
    placeholder = agent.secret_vault.placeholder(GH, "github")
    (tmp_path / ".bunnybyte" / "plans").mkdir(parents=True, exist_ok=True)

    agent.run_tool("write_file", {"path": ".bunnybyte/plans/p.md", "content": f"token {placeholder}\n"})

    assert (tmp_path / ".bunnybyte" / "plans" / "p.md").read_text(encoding="utf-8") == f"token {placeholder}\n"


def test_repo_sources_and_docs_produce_no_false_positives():
    root = Path(__file__).resolve().parents[1]
    files = list((root / "bunnybyte").rglob("*.py")) + list(root.glob("*.md")) + list((root / "docs").rglob("*.md"))
    hits = [(str(path), m.kind) for path in files for m in find_secrets(path.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []


def test_same_value_gets_the_same_placeholder_and_restores():
    vault = SecretVault()
    first = vault.mask(f"a {GH} b")
    assert GH not in first and first == vault.mask(f"a {GH} b")
    assert vault.restore(first) == f"a {GH} b"


def test_foreign_placeholders_pass_through_and_stale_ones_are_refused():
    vault = SecretVault()
    literal = "docs say <secret:github#abcdef-1> looks like this"
    assert vault.restore(literal) == literal
    with pytest.raises(SecretPlaceholderError):
        vault.restore("<secret:github#abcdef-1>", stale_nonces=("abcdef",))
    with pytest.raises(SecretPlaceholderError):
        vault.restore(f"<secret:github#{vault.nonce}-99>")


def test_reading_dotenv_keeps_secrets_out_of_prompts_and_runtime_files(tmp_path):
    agent = build_agent(tmp_path, ['<tool name="read_file" path=".env"></tool>', "<final>读完了</final>"])

    assert agent.ask(f"看看 .env，另外我的 key 是 {CONFIG_KEY}") == "读完了"

    prompts = "\n".join(agent.model_client.prompts)
    for secret in (AWS, GH, CONFIG_KEY, "hunter2-long-pw"):
        assert secret not in prompts
        assert secret not in runtime_files_text(tmp_path)
    assert "GITHUB_TOKEN=<secret:GITHUB_TOKEN#" in agent.model_client.prompts[1] or "GITHUB_TOKEN=<secret:github#" in agent.model_client.prompts[1]
    session = json.loads(Path(agent.session_path).read_text(encoding="utf-8"))
    assert session["secret_nonces"] == [agent.secret_vault.nonce]


def test_native_tool_mode_prompt_is_masked_too(tmp_path):
    from bunnybyte.providers.base import ModelToolCall

    (tmp_path / ".env").write_text(DOTENV, encoding="utf-8")
    client = ScriptedModelClient(
        [ModelToolCall(name="read_file", args={"path": ".env"}, id="c1"), "读完了"], supports_native_tools=True
    )
    agent = BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )

    agent.ask("看看 .env")

    assert len(client.prompts) == 2 and AWS not in client.prompts[1] and PLACEHOLDER.search(client.prompts[1])


def test_secret_in_long_shell_output_is_masked_before_clipping_and_in_the_artifact(tmp_path):
    (tmp_path / "dump.py").write_text("print('x' * 990)\nprint(open('.env').read())\n", encoding="utf-8")
    agent = build_agent(tmp_path, ['<tool name="run_shell" command="python3 dump.py"></tool>', "<final>ok</final>"])

    agent.ask("跑一下")

    assert GH not in "\n".join(agent.model_client.prompts)
    assert GH not in runtime_files_text(tmp_path)
    assert "full output saved" in agent.model_client.prompts[1]
    artifacts = list((tmp_path / ".bunnybyte" / "runs").rglob("run_shell-output*"))
    assert artifacts and PLACEHOLDER.search(artifacts[0].read_text(encoding="utf-8"))


def test_write_and_patch_with_placeholders_put_the_real_value_on_disk(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="read_file" path=".env"></tool>',
            '<tool name="write_file" path="settings.env"><content>GITHUB_TOKEN={GITHUB}\nDEBUG=1\n</content></tool>',
            '<tool name="read_file" path="settings.env"></tool>',
            '<tool name="patch_file" path="settings.env"><old_text>GITHUB_TOKEN={GITHUB}\nDEBUG=1</old_text><new_text>GITHUB_TOKEN={GITHUB}\nDEBUG=0</new_text></tool>',
            "<final>好了</final>",
        ],
        client_cls=PlaceholderEcho,
    )

    agent.ask("把 GitHub token 抄到 settings.env，再关掉 DEBUG")

    assert (tmp_path / "settings.env").read_text(encoding="utf-8") == f"GITHUB_TOKEN={GH}\nDEBUG=0\n"
    assert GH not in "\n".join(agent.model_client.prompts)
    assert GH not in runtime_files_text(tmp_path)


def test_literal_placeholder_in_a_file_survives_read_and_write_unchanged(tmp_path):
    literal = "示例：<secret:github#abcdef-1>\n"
    (tmp_path / "doc.md").write_text(literal, encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool name="read_file" path="doc.md"></tool>',
            '<tool name="write_file" path="doc.md"><content>示例：<secret:github#abcdef-1>\n</content></tool>',
            "<final>好了</final>",
        ],
    )

    agent.ask("原样重写 doc.md")

    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == literal


def test_placeholder_from_an_earlier_process_is_refused_after_resume(tmp_path):
    first = build_agent(tmp_path, ['<tool name="read_file" path=".env"></tool>', "<final>读完了</final>"])
    first.ask("看看 .env")
    stale = PLACEHOLDER.findall(first.model_client.prompts[1])[0]
    (tmp_path / "out.env").write_text("old\n", encoding="utf-8")

    resumed = BunnyByte.from_session(
        model_client=ScriptedModelClient(
            [
                '<tool name="read_file" path="out.env"></tool>',
                f'<tool name="write_file" path="out.env"><content>GITHUB_TOKEN={stale}\n</content></tool>',
                "<final>写不了</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=first.session_store,
        session_id=first.session["id"],
        approval_policy="auto",
    )
    resumed.secret_vault = SecretVault()
    resumed.ask("把 token 写进 out.env")

    assert (tmp_path / "out.env").read_text(encoding="utf-8") == "old\n"
    tool_results = [item["content"] for item in resumed.session["history"] if item.get("role") == "tool"]
    assert "已不在内存里" in tool_results[-1]


def test_run_shell_with_a_placeholder_is_refused(tmp_path):
    agent = build_agent(tmp_path, [])
    placeholder = agent.secret_vault.placeholder(GH, "github")

    result = agent.run_tool("run_shell", {"command": f"echo {placeholder} > leak.txt"})

    assert "run_shell 不会替换" in result
    assert not (tmp_path / "leak.txt").exists()


def test_worker_child_shares_the_parent_placeholders(tmp_path):
    from bunnybyte.core.worker_runtime import build_child_runtime

    agent = build_agent(tmp_path, [])
    parent_token = agent.redact_text(GH)
    child = build_child_runtime(agent, "Explore", None)

    assert child.redact_text(GH) == parent_token


def test_session_json_survives_secrets_with_quotes_and_backslashes(tmp_path, monkeypatch):
    tricky = 'p"a\\ss-wörd-123456'
    monkeypatch.setenv("SERVICE_PASSWORD", tricky)
    agent = build_agent(tmp_path, ["<final>好</final>"])

    agent.ask(f"密码是 {tricky}")

    text = Path(agent.session_path).read_text(encoding="utf-8")
    assert tricky not in json.loads(text)["history"][0]["content"]
    assert json.dumps(tricky)[1:-1] not in text

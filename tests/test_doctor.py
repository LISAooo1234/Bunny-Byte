import json
import subprocess
from types import SimpleNamespace

from bunnybyte import cli
from bunnybyte.doctor import (
    check_connectivity,
    check_provider,
    check_sandbox,
    check_workspace,
    doctor_exit_code,
    mask_secret,
    render_doctor_json,
    render_doctor_text,
    run_doctor,
)
from bunnybyte.config import ProviderConfig
from bunnybyte.providers import ProviderError
from bunnybyte.testing import ScriptedModelClient

SECRET = "sk-test-supersecret-1234"


def _write_project_config(root, api_key=SECRET, sandbox_mode="off"):
    (root / ".bunnybyte.toml").write_text(
        "\n".join(
            [
                'provider = "deepseek"',
                "[providers.deepseek]",
                'protocol = "anthropic"',
                f'api_key = "{api_key}"',
                'base_url = "https://api.deepseek.com/anthropic"',
                'model = "deepseek-v4-pro"',
                "[sandbox]",
                f'mode = "{sandbox_mode}"',
            ]
        ),
        encoding="utf-8",
    )


def _isolate_env(monkeypatch, tmp_path):
    for name in ("BUNNYBYTE_PROVIDER", "BUNNYBYTE_API_KEY", "BUNNYBYTE_BASE_URL", "BUNNYBYTE_MODEL",
                 "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "missing-global.toml")
    monkeypatch.setattr("bunnybyte.doctor.DEFAULT_CONFIG_PATH", tmp_path / "missing-global.toml")


def _git_init(root):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_mask_secret_never_reveals_prefix():
    assert mask_secret("") == ""
    assert mask_secret("short") == "*****"
    masked = mask_secret(SECRET)
    assert masked.endswith("1234")
    assert "sk-test" not in masked


def test_check_provider_reports_missing_key_with_fix(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, tmp_path)
    _write_project_config(tmp_path, api_key="")
    checks, config = check_provider(tmp_path)
    by_name = {check.name: check for check in checks}
    assert config.name == "deepseek"
    assert by_name["API key"].status == "fail"
    assert "bunny setup" in by_name["API key"].hint


def test_doctor_output_never_contains_api_key(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, tmp_path)
    _write_project_config(tmp_path)
    checks = run_doctor(tmp_path, offline=True)
    text = render_doctor_text(checks)
    payload = render_doctor_json(checks)
    assert SECRET not in text
    assert SECRET not in payload
    assert "1234" in text


def test_connectivity_ok_uses_client_factory(tmp_path):
    config = ProviderConfig("deepseek", "anthropic", SECRET, "https://example.test", "m")
    seen = []

    def factory(cfg):
        seen.append(cfg)
        return ScriptedModelClient(["pong"])

    check = check_connectivity(config, factory)
    assert check.status == "ok"
    assert "pong" in check.detail
    assert seen == [config]


def test_connectivity_maps_auth_error_to_actionable_hint():
    config = ProviderConfig("deepseek", "anthropic", SECRET, "https://example.test", "m")
    error = ProviderError("unauthorized", http_status=401, code="auth_error")
    check = check_connectivity(config, lambda cfg: ScriptedModelClient([error]))
    assert check.status == "fail"
    assert "401" in check.detail
    assert "API key" in check.hint


def test_connectivity_skips_when_config_incomplete():
    config = ProviderConfig("deepseek", "anthropic", "", "https://example.test", "m")
    check = check_connectivity(config, lambda cfg: (_ for _ in ()).throw(AssertionError("no call")))
    assert check.status == "skip"


def test_workspace_flags_tracked_secret_file(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".bunnybyte.toml").write_text('api_key = "x"\n', encoding="utf-8")
    subprocess.run(["git", "add", ".bunnybyte.toml"], cwd=tmp_path, check=True)
    checks = {check.name: check for check in check_workspace(tmp_path)}
    risk = checks[".bunnybyte.toml 泄露风险"]
    assert risk.status == "fail"
    assert "git rm --cached" in risk.hint


def test_workspace_warns_on_unignored_secret_file_and_non_git_dir(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".env").write_text("KEY=x\n", encoding="utf-8")
    checks = {check.name: check for check in check_workspace(tmp_path)}
    assert checks[".env 泄露风险"].status == "warn"

    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    checks = {check.name: check for check in check_workspace(tmp_path)}
    assert ".env 泄露风险" not in checks


def test_sandbox_required_without_backend_fails(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, tmp_path)
    _write_project_config(tmp_path, sandbox_mode="required")
    check = check_sandbox(tmp_path, which=lambda name: None)
    assert check.status == "fail"
    assert "bubblewrap" in check.hint
    _write_project_config(tmp_path, sandbox_mode="best_effort")
    assert check_sandbox(tmp_path, which=lambda name: None).status == "warn"
    assert check_sandbox(tmp_path, which=lambda name: "/usr/bin/bwrap").status == "ok"


def test_doctor_exit_code_reflects_failures(tmp_path, monkeypatch):
    _isolate_env(monkeypatch, tmp_path)
    _git_init(tmp_path)
    _write_project_config(tmp_path, api_key="")
    checks = run_doctor(tmp_path, offline=True)
    assert doctor_exit_code(checks) == 1
    _write_project_config(tmp_path)
    checks = run_doctor(tmp_path, offline=True, which=lambda name: f"/usr/bin/{name}")
    assert doctor_exit_code(checks) == 0
    assert json.loads(render_doctor_json(checks))["ok"] is True


def test_cli_doctor_subcommand_json(tmp_path, monkeypatch, capsys):
    _isolate_env(monkeypatch, tmp_path)
    _git_init(tmp_path)
    _write_project_config(tmp_path)
    code = cli.main(["doctor", "--cwd", str(tmp_path), "--offline", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    names = [check["name"] for check in payload["checks"]]
    assert "Provider" in names and "连通性" in names
    assert SECRET not in out
    assert code == (0 if payload["ok"] else 1)


def test_cli_doctor_online_builds_real_client(tmp_path, monkeypatch, capsys):
    _isolate_env(monkeypatch, tmp_path)
    _write_project_config(tmp_path)
    built = []

    def fake_client(args, config):
        built.append(SimpleNamespace(timeout=args.openai_timeout, provider=config.name))
        return ScriptedModelClient(["pong"])

    monkeypatch.setattr(cli, "_model_client_from_config", fake_client)
    cli.main(["doctor", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert "pong" in out
    assert built and built[0].provider == "deepseek"

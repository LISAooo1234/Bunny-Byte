"""Configuration helpers."""

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..features.sandbox import resolve_sandbox_config as resolve_sandbox_values

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - covered on Python 3.10 by dependency resolution
    import tomli as tomllib  # type: ignore[no-redef]


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PROVIDER = "openai"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "bunnybyte" / "config.toml"
PROJECT_CONFIG_NAME = ".bunnybyte.toml"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    protocol: str
    api_key: str
    base_url: str
    model: str


PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4",
    },
    "anthropic": {
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-6",
    },
    "deepseek": {
        "protocol": "anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-pro",
    },
}

PROVIDER_ALIASES = {
    "gpt": "openai",
    "claude": "anthropic",
}

PROTOCOLS = {"openai", "anthropic"}

PROVIDER_MAX_TOKENS: dict[str, int | None] = {
    "openai": None,
    "anthropic": None,
    "deepseek": None,
}
DEFAULT_MAX_TOKENS_FALLBACK = None


def default_max_tokens_for_provider(provider: str | None) -> int | None:
    if not provider:
        return DEFAULT_MAX_TOKENS_FALLBACK
    key = PROVIDER_ALIASES.get(provider, provider)
    return PROVIDER_MAX_TOKENS.get(key, DEFAULT_MAX_TOKENS_FALLBACK)


def default_provider_values(provider: str | None) -> dict[str, str]:
    provider_name = normalize_provider_name(provider)
    return dict(PROVIDER_DEFAULTS.get(provider_name, {}))

ENV_PROVIDER = "BUNNYBYTE_PROVIDER"
ENV_API_KEY = "BUNNYBYTE_API_KEY"
ENV_BASE_URL = "BUNNYBYTE_BASE_URL"
ENV_MODEL = "BUNNYBYTE_MODEL"

PROVIDER_ENV_NAMES = {
    "openai": {
        "api_key": ("OPENAI_API_KEY",),
        "base_url": ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
        "model": ("OPENAI_MODEL",),
    },
    "anthropic": {
        "api_key": (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY",
        ),
        "base_url": ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"),
        "model": ("ANTHROPIC_MODEL",),
    },
    "deepseek": {
        "api_key": ("DEEPSEEK_API_KEY",),
        "base_url": ("DEEPSEEK_API_BASE", "DEEPSEEK_BASE_URL"),
        "model": ("DEEPSEEK_MODEL",),
    },
}

LEGACY_ENV_NAMES = {
    "openai": {
        "api_key": ("BUNNYBYTE_OPENAI_API_KEY", "OPENAI_API_KEY"),
        "base_url": ("BUNNYBYTE_OPENAI_API_BASE", "OPENAI_API_BASE", "OPENAI_BASE_URL"),
        "model": ("BUNNYBYTE_OPENAI_MODEL", "OPENAI_MODEL"),
    },
    "anthropic": {
        "api_key": (
            "BUNNYBYTE_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
            "BUNNYBYTE_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        ),
        "base_url": (
            "BUNNYBYTE_ANTHROPIC_API_BASE",
            "ANTHROPIC_API_BASE",
            "ANTHROPIC_BASE_URL",
        ),
        "model": ("BUNNYBYTE_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
    },
    "deepseek": {
        "api_key": ("BUNNYBYTE_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        "base_url": (
            "BUNNYBYTE_DEEPSEEK_API_BASE",
            "DEEPSEEK_API_BASE",
            "DEEPSEEK_BASE_URL",
        ),
        "model": ("BUNNYBYTE_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
    },
}


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        raise ValueError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def find_project_config(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        config_path = path / PROJECT_CONFIG_NAME
        if config_path.exists():
            return config_path
    return None


def load_project_env(start, override=True):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default


def resolve_provider_config(
    provider: str | None = None,
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ProviderConfig:
    file_values = _load_config_values(start=start, explicit_path=config_path)
    legacy_env = _load_legacy_env_values(start)

    requested_provider = (
        provider
        or os.environ.get(ENV_PROVIDER)
        or file_values["top"].get("provider")
        or legacy_env.get(ENV_PROVIDER)
        or DEFAULT_PROVIDER
    )
    provider_name = normalize_provider_name(requested_provider)
    profile_values = _profile_values(file_values["providers"], provider_name)
    default_values = dict(PROVIDER_DEFAULTS.get(provider_name, {}))

    protocol = _first_value(
        None,
        os.environ.get("BUNNYBYTE_PROTOCOL"),
        profile_values.get("protocol"),
        legacy_env.get("BUNNYBYTE_PROTOCOL"),
        default_values.get("protocol"),
    )
    protocol = _validate_protocol(protocol, provider_name)

    env_values = _env_values(provider_name, protocol)
    legacy_values = _legacy_values(provider_name, protocol, legacy_env)

    resolved_model = _first_value(
        model,
        os.environ.get(ENV_MODEL),
        env_values.get("model"),
        profile_values.get("model"),
        legacy_env.get(ENV_MODEL),
        legacy_values.get("model"),
        default_values.get("model"),
    )
    resolved_base_url = _first_value(
        base_url,
        os.environ.get(ENV_BASE_URL),
        env_values.get("base_url"),
        profile_values.get("base_url"),
        legacy_env.get(ENV_BASE_URL),
        legacy_values.get("base_url"),
        default_values.get("base_url"),
    )
    resolved_api_key = _first_value(
        api_key,
        os.environ.get(ENV_API_KEY),
        env_values.get("api_key"),
        profile_values.get("api_key"),
        legacy_env.get(ENV_API_KEY),
        legacy_values.get("api_key"),
        "",
    )

    return ProviderConfig(
        name=provider_name,
        protocol=protocol,
        api_key=str(resolved_api_key or ""),
        base_url=str(resolved_base_url or ""),
        model=str(resolved_model or ""),
    )


def list_provider_profiles(
    *, start: str | Path = ".", config_path: str | None = None
) -> list[ProviderConfig]:
    """Return configured provider profiles without exposing API keys."""
    file_values = _load_config_values(start=start, explicit_path=config_path)
    names = set(PROVIDER_DEFAULTS)
    names.update(file_values["providers"])
    profiles = []
    for name in sorted(names):
        profile_values = _profile_values(file_values["providers"], name)
        protocol = _validate_protocol(profile_values.get("protocol"), name)
        profiles.append(
            ProviderConfig(
                name=name,
                protocol=protocol,
                api_key="",
                base_url=str(profile_values.get("base_url", "") or ""),
                model=str(profile_values.get("model", "") or ""),
            )
        )
    return profiles


def write_global_provider_config(
    provider: str,
    *,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    protocol: str | None = None,
    config_path: str | Path | None = None,
) -> ProviderConfig:
    provider_name = normalize_provider_name(provider)
    defaults = default_provider_values(provider_name)
    if not defaults and not protocol:
        raise ValueError(f"custom provider {provider_name!r} requires protocol")
    resolved_protocol = _validate_protocol(
        protocol or defaults.get("protocol"), provider_name
    )
    resolved_base_url = str(base_url or defaults.get("base_url") or "").strip()
    resolved_model = str(model or defaults.get("model") or "").strip()
    resolved_api_key = str(api_key or "").strip()
    if not resolved_api_key:
        raise ValueError("api key is required")
    if not resolved_base_url:
        raise ValueError("base URL is required")
    if not resolved_model:
        raise ValueError("model is required")

    path = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
    values: dict[str, Any] = {"top": {}, "providers": {}, "sandbox": {}}
    if path.exists():
        _merge_config_values(values, _read_config_file(path))

    values["top"]["provider"] = provider_name
    values["providers"].setdefault(provider_name, {}).update(
        {
            "protocol": resolved_protocol,
            "api_key": resolved_api_key,
            "base_url": resolved_base_url,
            "model": resolved_model,
        }
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(_render_config_values(values), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return ProviderConfig(
        name=provider_name,
        protocol=resolved_protocol,
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        model=resolved_model,
    )


def resolve_project_sandbox_config(
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
):
    file_values = _load_config_values(start=start, explicit_path=config_path)
    values = {"sandbox": dict(file_values.get("sandbox", {}) or {})}
    if mode:
        values["sandbox"]["mode"] = mode
    if backend:
        values["sandbox"]["backend"] = backend
    return resolve_sandbox_values(values)


def normalize_provider_name(provider: str | None) -> str:
    normalized = (provider or DEFAULT_PROVIDER).strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


def _load_config_values(start: str | Path, explicit_path: str | None) -> dict[str, Any]:
    values: dict[str, Any] = {"top": {}, "providers": {}, "sandbox": {}}
    if explicit_path:
        _merge_config_values(
            values, _read_config_file(Path(explicit_path).expanduser())
        )
        return values

    for path in (DEFAULT_CONFIG_PATH, find_project_config(start)):
        if path and path.exists():
            _merge_config_values(values, _read_config_file(path))
    return values


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid BunnyByte config file {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"could not read BunnyByte config file {path}: {exc}") from exc

    values: dict[str, Any] = {"top": {}, "providers": {}, "sandbox": {}}
    if "provider" in data:
        values["top"]["provider"] = data["provider"]

    providers = data.get("providers", {})
    if isinstance(providers, dict):
        for name, section in providers.items():
            if isinstance(section, dict):
                values["providers"][normalize_provider_name(str(name))] = dict(section)

    sandbox = data.get("sandbox", {})
    if isinstance(sandbox, dict):
        values["sandbox"] = dict(sandbox)

    for name in ("openai", "anthropic", "deepseek"):
        section = data.get(name, {})
        if isinstance(section, dict):
            values["providers"].setdefault(name, {}).update(section)
    return values


def _render_config_values(values: dict[str, Any]) -> str:
    lines: list[str] = []
    provider = values.get("top", {}).get("provider")
    if provider:
        lines.append(f"provider = {_toml_value(provider)}")
        lines.append("")

    providers = dict(values.get("providers", {}) or {})
    provider_names = list(providers)
    if provider in provider_names:
        provider_names.remove(provider)
        provider_names.insert(0, provider)
    for name in provider_names:
        section = dict(providers.get(name, {}) or {})
        if not section:
            continue
        lines.append(f"[providers.{_toml_key(name)}]")
        for key in ("protocol", "api_key", "base_url", "model"):
            value = section.get(key)
            if value:
                lines.append(f"{key} = {_toml_value(value)}")
        extra_keys = sorted(set(section) - {"protocol", "api_key", "base_url", "model"})
        for key in extra_keys:
            value = section.get(key)
            if value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")

    sandbox = dict(values.get("sandbox", {}) or {})
    if sandbox:
        lines.append("[sandbox]")
        for key in sorted(sandbox):
            value = sandbox.get(key)
            if value is not None:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _toml_key(value: Any) -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    return json.dumps(text)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _merge_config_values(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    target["top"].update(incoming.get("top", {}))
    target["sandbox"].update(incoming.get("sandbox", {}))
    for name, section in incoming.get("providers", {}).items():
        target["providers"].setdefault(name, {}).update(section)


def _profile_values(
    providers: dict[str, dict[str, Any]], provider_name: str
) -> dict[str, Any]:
    values = dict(PROVIDER_DEFAULTS.get(provider_name, {}))
    values.update(providers.get(provider_name, {}))
    return values


def _load_legacy_env_values(start: str | Path) -> dict[str, str]:
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is not None:
            loaded[parsed[0]] = parsed[1]
    return loaded


def _env_values(provider_name: str, protocol: str) -> dict[str, str]:
    values: dict[str, str] = {}
    sources = [PROVIDER_ENV_NAMES.get(provider_name, {})]
    if provider_name == protocol:
        sources.append(PROVIDER_ENV_NAMES.get(protocol, {}))
    for source in sources:
        for key, names in source.items():
            value = _first_env(names)
            if value and key not in values:
                values[key] = value
    return values


def _legacy_values(
    provider_name: str, protocol: str, env_values: dict[str, str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    sources = [LEGACY_ENV_NAMES.get(provider_name, {})]
    if provider_name == protocol:
        sources.append(LEGACY_ENV_NAMES.get(protocol, {}))
    for source in sources:
        for key, names in source.items():
            value = _first_mapping_value(env_values, names)
            if value and key not in values:
                values[key] = value
    return values


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _first_mapping_value(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = values.get(name)
        if value:
            return value
    return ""


def _first_value(*values):
    for value in values:
        if value:
            return value
    return ""


def _validate_protocol(protocol: Any, provider_name: str) -> str:
    normalized = str(protocol or "").strip().lower()
    if normalized not in PROTOCOLS:
        raise ValueError(
            f"provider {provider_name!r} uses unsupported protocol: {protocol!r}"
        )
    return normalized

"""备用 provider 链的解析。

配置来源优先级：CLI `--fallback`、环境变量 `BUNNYBYTE_FALLBACK`、
`[providers.<name>]` 下的 `fallback`、顶层 `fallback`。
"""

import os
import re
from pathlib import Path

from . import (
    ProviderConfig,
    _load_config_values,
    normalize_provider_name,
    resolve_provider_config,
)

ENV_FALLBACK = "BUNNYBYTE_FALLBACK"
_DISABLED = {"none", "off", "false", "no"}


def parse_fallback_names(value) -> list[str]:
    """把逗号分隔字符串或列表整理成去重后的 provider 名称列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    names: list[str] = []
    for item in items:
        text = item.strip()
        if not text:
            continue
        if text.lower() in _DISABLED:
            return []
        name = normalize_provider_name(text)
        if name not in names:
            names.append(name)
    return names


def resolve_fallback_names(
    provider_name: str,
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    cli_value=None,
) -> list[str]:
    if cli_value is not None:
        names = parse_fallback_names(cli_value)
    elif os.environ.get(ENV_FALLBACK):
        names = parse_fallback_names(os.environ[ENV_FALLBACK])
    else:
        file_values = _load_config_values(start=start, explicit_path=config_path)
        profile = file_values["providers"].get(provider_name, {})
        if "fallback" in profile:
            names = parse_fallback_names(profile["fallback"])
        else:
            names = parse_fallback_names(file_values["top"].get("fallback"))
    return [name for name in names if name != provider_name]


def resolve_fallback_configs(
    provider_name: str,
    *,
    start: str | Path = ".",
    config_path: str | None = None,
    cli_value=None,
) -> list[ProviderConfig]:
    """返回配置完整（有 key / base_url / model）的备用 provider。"""
    configs = []
    names = resolve_fallback_names(
        provider_name, start=start, config_path=config_path, cli_value=cli_value
    )
    for name in names:
        try:
            config = resolve_provider_config(
                name,
                start=start,
                config_path=config_path,
                include_generic_env=False,
            )
        except ValueError:
            continue
        if config.api_key and config.base_url and config.model:
            configs.append(config)
    return configs

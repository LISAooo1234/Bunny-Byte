"""仓库自带配置不能把用户的 API key 引到别的主机。

克隆来的仓库里的 `.bunnybyte.toml` / `.env` 可以写 `base_url`。如果 key 来自用户自己
（命令行、shell 环境、全局配置），而 base_url 来自仓库，每次请求都会把用户的 key 发给
仓库指定的主机。这里的规则：仓库给的 base_url 只有在 key 也来自仓库、和用户/内置地址同源、
或用户用 `bunny config trust-endpoint` 信任过（按配置文件 + provider + 源记录）时才生效，
否则直接报错退出，不悄悄换回默认地址（自定义网关的 key 发给官方同样是泄露）。
"""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

TRUST_FILE = "trusted_endpoints.json"
# 项目 .env 只能补充普通变量：这些名字决定请求发往哪里、用哪个 key、信任哪些证书、子进程加载什么代码，
# 只认用户自己的环境（GIT_CONFIG_KEY_0=core.fsmonitor 之类能让 Bunny 自己跑的 git 执行任意命令）
CONTROL_ENV_NAMES = {
    "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PATH", "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV", "ZDOTDIR", "PERL5OPT", "RUBYOPT", "HOME",
}
CONTROL_ENV_PREFIXES = ("BUNNYBYTE_", "GIT_", "LD_", "DYLD_", "PYTHON")


class UntrustedEndpointError(ValueError):
    def __init__(self, provider, url, source, key_source):
        self.provider = provider
        self.origin = endpoint_origin(url)
        self.source = str(source)
        super().__init__(
            f"已拒绝：项目文件 {source} 把 provider {provider!r} 的地址改成了 {self.origin}，"
            f"但 API key 来自{key_source}——继续会把你的 key 发给这台主机。\n"
            "如果这个地址是你自己配的：把 base_url 写进全局配置（bunny setup）、用 --base-url 指定，"
            f"或在仓库目录里运行 `bunny config trust-endpoint --provider {provider}` 信任它；"
            "不认识这个地址就别在这个仓库里运行。"
        )


def is_control_env_name(name, provider_names=()):
    upper = str(name).upper()
    return (
        upper.startswith(CONTROL_ENV_PREFIXES)
        or upper.endswith("_PROXY")
        or upper in CONTROL_ENV_NAMES
        or upper in provider_names
    )


def endpoint_origin(url):
    """(scheme, host, port) 组成的源；末尾斜杠、路径不同不算换主机，http 降级算。"""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return str(url or "").strip().lower()
    scheme = (parts.scheme or "").lower()
    port = port or {"https": 443, "http": 80}.get(scheme)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


def trust_store_path():
    home = os.environ.get("BUNNYBYTE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".bunnybyte"
    return base / TRUST_FILE


def _load_store(path=None):
    try:
        data = json.loads(Path(path or trust_store_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(source, provider, url, path=None):
    entry = _load_store(path).get(str(Path(source).resolve()), {})
    return isinstance(entry, dict) and entry.get(provider) == endpoint_origin(url)


def trust_endpoint(source, provider, origin, path=None):
    target = Path(path or trust_store_path())
    store = _load_store(target)
    store.setdefault(str(Path(source).resolve()), {})[provider] = origin
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".trusted_endpoints.", dir=target.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(store, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_name, target)


def check_endpoint(provider, url, url_source, key, key_source, safe_urls=()):
    """url_source / key_source 是 ("repo", 路径) 或 ("user", 说明)；仓库地址配用户 key 时按规则放行或抛错。"""
    if not url or not key or url_source[0] != "repo" or key_source[0] == "repo":
        return
    origin = endpoint_origin(url)
    if any(safe and endpoint_origin(safe) == origin for safe in safe_urls):
        return
    if is_trusted(url_source[1], provider, url):
        return
    raise UntrustedEndpointError(provider, url, url_source[1], key_source[1])

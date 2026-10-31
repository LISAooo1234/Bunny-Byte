"""查包在公共仓库里存不存在、第一次发布是什么时候（PyPI simple JSON API / npm registry）。

只查内置热门名单之外的名字；每个请求 2 秒超时、最多 MAX_WORKERS 个并行，结果缓存在
`~/.bunnybyte/dependency_cache.json`（仓库里的缓存文件能被克隆来的仓库预先写好，所以放在用户目录）。配置了私有索引的生态不查（不把内部包名发到公网）。
"""

import configparser
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .dependency_extract import NPM, PYPI, normalize

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

TIMEOUT = 2.0
MAX_WORKERS = 8
CACHE_TTL = 24 * 3600
NEW_PACKAGE_DAYS = 30
PYPI_HOSTS = {"pypi.org", "files.pythonhosted.org", "pypi.python.org"}
# 国内常用的 PyPI / npm 镜像：内容和公共仓库一样，配了它们照样可以去 pypi.org / npmjs 查
PUBLIC_MIRRORS = (
    "pypi.tuna.tsinghua.edu.cn", "mirrors.tuna.tsinghua.edu.cn", "mirrors.aliyun.com", "mirrors.ustc.edu.cn",
    "pypi.mirrors.ustc.edu.cn", "mirrors.cloud.tencent.com", "repo.huaweicloud.com", "mirrors.huaweicloud.com",
    "pypi.douban.com", "mirrors.bfsu.edu.cn", "mirrors.sjtug.sjtu.edu.cn", "mirror.nju.edu.cn", "mirrors.163.com",
    "registry.npmjs.org", "registry.yarnpkg.com", "registry.npmmirror.com", "registry.npm.taobao.org",
)
NPM_REGISTRY = "https://registry.npmjs.org"
NPM_MIRRORS = {"registry.npmmirror.com": "https://registry.npmmirror.com", "registry.npm.taobao.org": "https://registry.npmmirror.com"}
PY_INDEX_ENV = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX", "UV_DEFAULT_INDEX")
NPM_INDEX_ENV = ("NPM_CONFIG_REGISTRY", "npm_config_registry", "YARN_REGISTRY")


def is_public_index(url):
    host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    return not host or host in PYPI_HOSTS or any(host == mirror or host.endswith("." + mirror) for mirror in PUBLIC_MIRRORS)


def private_index(ecosystem, root, extra_urls=(), env=None):
    """这个生态配置了非公共的索引 / registry 时返回它的地址（只用来说明原因），否则返回空串。"""
    env = os.environ if env is None else env
    urls = list(extra_urls)
    if ecosystem == PYPI:
        urls += [value for name in PY_INDEX_ENV for value in str(env.get(name, "")).split()]
        urls += _pip_conf_urls(env)
    else:
        urls += [env.get(name, "") for name in NPM_INDEX_ENV]
        urls += _npmrc_urls([Path(root) / ".npmrc", Path(env.get("HOME", "~")).expanduser() / ".npmrc"])
    for url in urls:
        if url and not is_public_index(url):
            return url
    return ""


def _pip_conf_urls(env):
    home = Path(env.get("HOME", "~")).expanduser()
    paths = [env.get("PIP_CONFIG_FILE", ""), home / ".pip" / "pip.conf", home / ".config" / "pip" / "pip.conf",
             home / "Library" / "Application Support" / "pip" / "pip.conf", "/etc/pip.conf"]
    urls = []
    for path in paths:
        if not path or not Path(path).is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in parser.sections():
            for key in ("index-url", "extra-index-url", "index_url", "extra_index_url"):
                urls += parser.get(section, key, fallback="").split()
    return urls


def _npmrc_urls(paths):
    urls = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip().endswith("registry"):  # `registry=` 和 `@scope:registry=` 都算
                urls.append(value.strip())
    return urls


def project_index_urls(root):
    """项目文件里已经写着的索引：pyproject 的 [[tool.uv.index]] / poetry source、uv.toml、requirements 的 --index-url、.yarnrc.yml。"""
    root = Path(root)
    urls = []
    for name in ("pyproject.toml", "uv.toml"):
        try:
            data = tomllib.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        uv = (data.get("tool", {}) or {}).get("uv", {}) if name == "pyproject.toml" else data
        sources = list((uv or {}).get("index", []) or []) + list(((data.get("tool", {}) or {}).get("poetry", {}) or {}).get("source", []) or [])
        urls += [str(item.get("url", "")) for item in sources if isinstance(item, dict)]
        urls += [str((uv or {}).get(key, "")) for key in ("index-url", "extra-index-url") if isinstance((uv or {}).get(key), str)]
    for path in [*sorted(root.glob("requirements*.txt")), *sorted(root.glob("requirements/*.txt"))]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            parts = line.strip().replace("=", " ", 1).split()
            if len(parts) >= 2 and parts[0] in {"-i", "--index-url", "--extra-index-url"}:
                urls.append(parts[1])
    try:
        for line in (root / ".yarnrc.yml").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "npmRegistryServer":
                urls.append(value.strip().strip("\"'"))
    except (OSError, UnicodeDecodeError):
        pass
    return [url for url in urls if url]


def npm_lookup_base(root, env=None):
    """配了 npmmirror（淘宝源）时去镜像查：国内访问 registry.npmjs.org 经常超时。"""
    env = os.environ if env is None else env
    urls = [env.get(name, "") for name in NPM_INDEX_ENV]
    urls += _npmrc_urls([Path(root) / ".npmrc", Path(env.get("HOME", "~")).expanduser() / ".npmrc"])
    for url in urls:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
        if host in NPM_MIRRORS:
            return NPM_MIRRORS[host]
    return NPM_REGISTRY


class RegistryLookup:
    """`check(ecosystem, names)` → {规范化名字: verdict}。verdict 是 dict：
    `status` = ok / new / missing / unchecked，`created`（ISO 日期，可能为空），`detail`。"""

    def __init__(self, cache_path=None, fetch=None, now=None, npm_base=NPM_REGISTRY):
        self.npm_base = npm_base
        self.cache_path = Path(cache_path) if cache_path else None
        self.fetch = fetch or fetch_json
        self.now = now or time.time
        self._cache = None

    def check(self, ecosystem, names):
        names = list(dict.fromkeys(normalize(ecosystem, name) for name in names))
        cache = self._load()
        results, todo = {}, []
        for name in names:
            cached = cache.get(f"{ecosystem}:{name}")  # 镜像和官方源内容一样，缓存不区分
            if cached and self.now() - float(cached.get("checked_at", 0)) < CACHE_TTL:
                results[name] = _with_age(cached, self.now())
            else:
                todo.append(name)  # 全部都查：只查前几个的话，把可疑包排在第六个就能绕过
        if todo:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as pool:
                fetched = dict(zip(todo, pool.map(lambda name: self._lookup(ecosystem, name), todo)))
            for name, verdict in fetched.items():
                if verdict["status"] in {"ok", "new"}:  # 查不到的不缓存：这个名字随时可能被注册
                    cache[f"{ecosystem}:{name}"] = dict(verdict, checked_at=self.now())
                results[name] = _with_age(verdict, self.now())
            self._save(cache)
        return results

    def _lookup(self, ecosystem, name):
        # PyPI 用 PEP 691 的 simple JSON（带 PEP 700 upload-time）；npm 用 registry 文档里的 time.created
        if ecosystem == PYPI:
            url = f"https://pypi.org/simple/{urllib.parse.quote(name)}/"
        else:
            url = f"{self.npm_base.rstrip('/')}/{urllib.parse.quote(name, safe='@')}"
        try:
            status, data = self.fetch(url, TIMEOUT)
        except Exception as exc:  # 超时、断网、DNS：不能拖住工具调用
            return {"status": "unchecked", "created": "", "detail": f"查询失败：{type(exc).__name__}"}
        if status == 404:
            return {"status": "missing", "created": "", "detail": "公共仓库里没有这个包"}
        if status != 200 or not isinstance(data, dict):
            return {"status": "unchecked", "created": "", "detail": f"查询失败：HTTP {status}"}
        if ecosystem == NPM:
            created = str((data.get("time") or {}).get("created", ""))
            if not data.get("versions"):
                return {"status": "missing", "created": created[:10], "detail": "包已被撤下（没有可安装的版本）"}
            return {"status": "ok", "created": created[:10], "detail": ""}
        if str((data.get("project-status") or {}).get("status", "")) == "quarantined":
            return {"status": "missing", "created": "", "detail": "PyPI 已隔离这个包"}
        times = sorted(str(item.get("upload-time") or "") for item in data.get("files") or [] if item.get("upload-time"))
        if not data.get("files"):
            return {"status": "missing", "created": "", "detail": "包没有任何可安装的文件"}
        return {"status": "ok", "created": times[0][:10] if times else "", "detail": ""}

    def _load(self):
        if self._cache is None:
            self._cache = {}
            if self.cache_path and self.cache_path.is_file():
                try:
                    self._cache = dict(json.loads(self.cache_path.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    self._cache = {}
        return self._cache

    def _save(self, cache):
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        except OSError:
            pass


def _with_age(verdict, now):
    """首次发布不到 NEW_PACKAGE_DAYS 天的包标成 new（缓存里存的是 ok，年龄每次现算）。"""
    verdict = {key: verdict.get(key, "") for key in ("status", "created", "detail")}
    if verdict["status"] == "ok" and verdict["created"]:
        try:
            created = datetime.fromisoformat(verdict["created"]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return verdict
        days = int((now - created) // 86400)
        if days < NEW_PACKAGE_DAYS:
            verdict["status"] = "new"
            verdict["detail"] = f"第一次发布在 {max(days, 0)} 天前"
    return verdict


def fetch_json(url, timeout):
    accept = "application/vnd.pypi.simple.v1+json" if "/simple/" in url else "application/json"
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "bunnybyte-dependency-guard"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None

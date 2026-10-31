"""从一条 shell 命令或一次依赖文件改动里找出“这次要新装的包名”。

只做纯文本解析，不联网：给依赖守卫（dependency_guard）用。认得的命令有 pip / python -m pip /
uv / pipx / poetry / pdm 和 npm / pnpm / yarn / bun（`bash -c` 里的也算），以及会下载并直接运行包的
npx / bunx / `npm exec` / `pnpm dlx` / `yarn dlx` / `npm create` / uvx / `uv tool run` / `pipx run`
（标成 runner，解析在 dependency_runners）；`-r 文件` 记下文件名，由守卫去读。认得的文件有
requirements*.txt、pyproject.toml、package.json。路径、URL、git 地址、wheel 包都不算包名。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import dependency_runners as runners
from .shell_segments import split_segments, stage_head

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

PYPI, NPM = "pypi", "npm"
PY_NAME_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
NPM_NAME_RE = re.compile(r"^((?:@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*)", re.I)
PYTHON_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")
PIP_RE = re.compile(r"^pip(?:\d+(?:\.\d+)?)?$")
SHELLS = {"sh", "bash", "zsh", "dash"}
ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".zip", ".tar.bz2", ".egg")
INDEX_FLAGS = {"-i", "--index-url", "--extra-index-url", "--index", "--default-index", "-f", "--find-links", "--registry"}
# 带值的选项：值不是包名。没列出的 `--x` 当作开关，`--x=y` 本身就带着值。
PY_VALUE_FLAGS = INDEX_FLAGS | {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-t", "--target", "--prefix", "--root",
    "--python", "-p", "--platform", "--python-version", "--implementation", "--abi", "--src", "--upgrade-strategy",
    "--progress-bar", "--log", "--cache-dir", "--trusted-host", "--proxy", "--timeout", "--retries",
    "--exists-action", "--no-binary", "--only-binary", "--config-settings", "-C", "--global-option", "--report",
    "--group", "-G", "--optional", "--extra", "--package", "--directory", "--project", "--tag", "--branch", "--rev",
    "--script", "--bounds", "--source", "--from", "--with", "--spec", "--pip-args", "--suffix", "--python-preference",
}
NPM_VALUE_FLAGS = INDEX_FLAGS | {"--prefix", "-w", "--workspace", "--tag", "--save-prefix", "--omit", "--include",
                                 "--cache", "--userconfig", "-C", "--dir", "--filter", "-p", "--package", "--cwd",
                                 "--loglevel", "--registry-scope", "--scope", "-F"}
NPM_INSTALL_VERBS = {"install", "i", "add", "isntall", "in", "ins", "inst", "insta", "instal"}
REQUIREMENTS_RE = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|requirements/[^/]+\.(txt|in))$")


@dataclass(frozen=True)
class Requested:
    ecosystem: str  # "pypi" | "npm"
    name: str  # 规范化前的写法（去掉版本、extras）
    origin: str  # 原文：命令里的那个参数，或文件名
    runner: bool = False  # npx / uvx 这类：下载后立刻运行
    bin_name: bool = False  # `npx 名字` 的裸位置参数：本地 node_modules/.bin 里有同名命令时不下载


@dataclass
class Extraction:
    packages: list
    index_urls: list  # 命令或文件里显式写的索引 / registry 地址
    requirement_files: list = field(default_factory=list)  # `pip install -r 文件` 引用的文件（守卫去读）


def normalize(ecosystem, name):
    """PEP 503：PyPI 名字不分大小写，`-_.` 等价；npm 名字只转小写。"""
    if ecosystem == PYPI:
        return re.sub(r"[-_.]+", "-", name).lower()
    return name.lower()


def from_command(command):
    """一条 run_shell 命令里所有安装类调用要装的包。解析不了（引号没闭合）时返回空。"""
    result = Extraction([], [])
    segments = split_segments(command)
    for segment in segments or []:
        for stage in segment.stages:
            head, args = stage_head(stage)
            _from_stage(head, [_unquote(item) for item in _drop_redirections(args)], result)
    return result


def _drop_redirections(args):
    """`2>&1`、`> install.log`、`<in` 不是包名：去掉重定向符号、它前面的文件描述符数字和后面的目标。"""
    kept, skip = [], False
    for index, item in enumerate(args):
        if skip:
            skip = False
            continue
        if item and set(item) <= set("<>&"):
            if kept and kept[-1].isdigit():
                kept.pop()
            skip = True  # 重定向目标：文件名或 `&1` 里的 1
            continue
        kept.append(item)
    return kept


def _from_stage(head, args, result):
    if head in SHELLS and len(args) >= 2 and args[0].startswith("-") and "c" in args[0] and not args[0].startswith("--"):
        inner = from_command(args[1])  # `bash -c "pip install x"`、`bash -lc …`
        result.packages += inner.packages
        result.index_urls += inner.index_urls
        result.requirement_files += inner.requirement_files
        return
    if PYTHON_RE.match(head) and args[:2] == ["-m", "pip"]:
        head, args = "pip", args[2:]
    elif PYTHON_RE.match(head) and len(args) >= 2 and args[0] == "-m" and args[1] in {"uv", "pipx", "poetry", "pdm"}:
        head, args = args[1], args[2:]
    if PIP_RE.match(head) and args[:1] == ["install"]:
        _collect(PYPI, args[1:], result)
    elif head == "uv" and args[:2] == ["pip", "install"]:
        _collect(PYPI, args[2:], result)
    elif head == "uv" and args[:2] == ["pip", "sync"]:
        result.requirement_files += [item for item in args[2:] if not item.startswith("-")]
    elif head == "uv" and args[:1] == ["add"] or head in {"poetry", "pdm"} and args[:1] == ["add"]:
        _collect(PYPI, args[1:], result)
    elif head == "uvx" or head == "uv" and args[:2] == ["tool", "run"]:
        _uv(args if head == "uvx" else args[2:], "run_tool", result, runner=True)
    elif head == "uv" and args[:2] == ["tool", "install"]:
        _uv(args[2:], "install", result)
    elif head == "uv" and args[:1] == ["run"]:
        _uv(args[1:], "run", result)
    elif head == "pipx" and args[:1] == ["install"]:
        _collect(PYPI, args[1:], result)
    elif head == "pipx" and args[:1] in (["run"], ["inject"]):
        specs, files = runners.pipx_specs(args[0], args[1:])
        result.requirement_files += files
        for spec in specs:
            _add(PYPI, spec, result, runner=args[0] == "run")
    elif head in {"npx", "pnpx", "bunx"} or head == "bun" and args[:1] == ["x"]:
        _npm_runner(args[1:] if head == "bun" else args, result)
    elif head in {"npm", "pnpm", "bun", "yarn"}:
        leading = _leading_packages(args)  # `pnpm --package=x dlx cmd`
        args = _skip_leading_options(head, args)  # `npm --prefix web install x`、`pnpm --filter app add x`
        if head == "yarn" and args[:1] == ["workspace"] and len(args) >= 2:
            args = args[2:]  # `yarn workspace web add x`
        if head == "yarn" and args[:1] == ["global"]:
            args = args[1:]
        verb = args[0] if args else ""
        if verb == "add" or head != "yarn" and verb in NPM_INSTALL_VERBS:
            _collect(NPM, args[1:], result)
        elif head == "npm" and verb in {"exec", "x"} or head in {"pnpm", "yarn"} and verb == "dlx":
            _npm_runner(args[1:], result, leading, shell_flags={"-c", "--shell-mode"} if head == "pnpm" else ())
        elif verb == "create" or head == "npm" and verb in {"init", "innit"}:
            spec = runners.create_spec(args[1:], lambda item: package_name(NPM, item))
            if spec:
                result.packages.append(Requested(NPM, spec, f"{head} {verb} {args[1] if len(args) > 1 else ''}".strip(), True))


def _npm_runner(args, result, leading=(), shell_flags=()):
    specs = [(item, False) for item in leading] or runners.npx_specs(args, shell_flags)
    for spec, bin_name in specs:
        name = package_name(NPM, spec)
        if name:
            result.packages.append(Requested(NPM, name, spec, True, bin_name))


def _leading_packages(args):
    found, index = [], 0
    while index < len(args) and args[index].startswith("-"):
        flag, eq, value = args[index].partition("=")
        if flag in {"--package", "-p"}:
            value = value if eq else (args[index + 1] if index + 1 < len(args) else "")
            found.append(value)
            index += 0 if eq else 1
        index += 1
    return [item for item in found if item]


def _uv(args, mode, result, runner=False):
    specs, urls, files = runners.uv_specs(args, mode)
    result.index_urls += urls
    result.requirement_files += files
    for spec in specs:
        _add(PYPI, spec, result, runner=runner)


def _skip_leading_options(head, args):
    index = 0
    while index < len(args) and args[index].startswith("-"):
        flag, _, value = args[index].partition("=")
        takes_value = flag in NPM_VALUE_FLAGS and not value and not (head == "pnpm" and flag == "-w")  # pnpm -w = 工作区根目录
        index += 2 if takes_value else 1
    return args[index:]


def _collect(ecosystem, args, result):
    value_flags = PY_VALUE_FLAGS if ecosystem == PYPI else NPM_VALUE_FLAGS
    skip_next = False
    for index, item in enumerate(args):
        if skip_next:
            skip_next = False
            if args[index - 1] in INDEX_FLAGS:
                result.index_urls.append(item)
            elif args[index - 1] in {"-r", "--requirement", "--requirements"}:
                result.requirement_files.append(item)
            continue
        if item.startswith("-r") and len(item) > 2 and not item.startswith("--") and ecosystem == PYPI:
            result.requirement_files.append(item[2:])  # `-rextra.txt`
            continue
        if item.startswith("-"):
            flag, _, value = item.partition("=")
            if flag in INDEX_FLAGS and value:
                result.index_urls.append(value)
            elif flag in {"-r", "--requirement", "--requirements"} and value:
                result.requirement_files.append(value)
            skip_next = flag in value_flags and not value
            continue
        if item in {"--", "@"}:
            continue
        _add(ecosystem, item, result)


def _add(ecosystem, item, result, runner=False):
    name = package_name(ecosystem, item)
    if name:
        result.packages.append(Requested(ecosystem, name, item, runner))


def package_name(ecosystem, spec):
    """`requests[socks]>=2`、`lodash@4`、`@types/node@20` 里的包名；路径、URL、本地文件返回空串。"""
    spec = spec.strip()
    if not spec or "://" in spec or spec.startswith(("git+", ".", "/", "~", "file:", "link:", "workspace:", "github:")):
        return ""
    if spec.lower().endswith(ARCHIVE_SUFFIXES):
        return ""
    if ecosystem == NPM:
        if "@npm:" in spec:  # 别名 `foo@npm:bar@1` 装的是 bar
            spec = spec.split("@npm:", 1)[1]
        if "/" in spec and not spec.startswith("@"):
            return ""  # `user/repo` 是 GitHub 简写
        match = NPM_NAME_RE.match(spec)
        return match.group(1) if match else ""
    if "/" in spec or "\\" in spec or spec.isdigit():
        return ""
    match = PY_NAME_RE.match(spec)
    return match.group(1) if match else ""


def from_file_change(relative_path, before_text, after_text):
    """依赖文件改动后新出现的包（改动前已有的不算）；不是依赖文件或解析失败时返回空。"""
    kind = dependency_file_kind(relative_path)
    if not kind:
        return Extraction([], [])
    after = _file_names(kind, after_text)
    if after is None:
        return Extraction([], [])
    before = _file_names(kind, before_text or "") or Extraction([], [])
    known = {normalize(item.ecosystem, item.name) for item in before.packages}
    fresh = [item for item in after.packages if normalize(item.ecosystem, item.name) not in known]
    urls = [url for url in after.index_urls if url not in before.index_urls]
    return Extraction(_dedupe(fresh), urls)


def names_in_file(relative_path, text):
    """`pip install -r 文件` 引用的文件里的包（文件名不叫 requirements 也按 requirements 解析）。"""
    kind = dependency_file_kind(relative_path) or "requirements"
    return _file_names(kind, text) or Extraction([], [])


def dependency_file_kind(relative_path):
    path = str(relative_path or "").replace("\\", "/")
    name = PurePosixPath(path).name
    if name == "pyproject.toml":
        return "pyproject"
    if name == "package.json":
        return "package_json"
    if REQUIREMENTS_RE.search(path):
        return "requirements"
    return ""


def _file_names(kind, text):
    try:
        if kind == "requirements":
            return _requirements(text)
        if kind == "pyproject":
            return _pyproject(tomllib.loads(text) if text.strip() else {})
        return _package_json(json.loads(text) if text.strip() else {})
    except (ValueError, TypeError, AttributeError):
        return None  # 改到一半的 TOML / JSON：等它写完整了再说


def _requirements(text):
    result = Extraction([], [])
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            flag, _, rest = line.replace("=", " ", 1).partition(" ")
            if flag in INDEX_FLAGS and rest.strip():
                result.index_urls.append(rest.strip())
            continue
        _add(PYPI, line, result)
    return result


def _pyproject(data):
    result = Extraction([], [])
    project = data.get("project", {}) or {}
    specs = list(project.get("dependencies", []) or [])
    for group in (project.get("optional-dependencies", {}) or {}).values():
        specs += list(group or [])
    for group in (data.get("dependency-groups", {}) or {}).values():
        specs += [item for item in group or [] if isinstance(item, str)]
    tool = data.get("tool", {}) or {}
    specs += list((tool.get("uv", {}) or {}).get("dev-dependencies", []) or [])
    for spec in specs:
        if isinstance(spec, str):
            _add(PYPI, spec, result)
    for index in (tool.get("uv", {}) or {}).get("index", []) or []:
        if isinstance(index, dict) and index.get("url"):
            result.index_urls.append(str(index["url"]))
    return result


def _package_json(data):
    result = Extraction([], [])
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, version in (data.get(section, {}) or {}).items():
            version = str(version)
            if version.startswith("npm:"):
                _add(NPM, version[4:], result)
            elif not version.startswith(("file:", "link:", "workspace:", "git", "http", "github:", "portal:")) and "/" not in version:
                _add(NPM, name, result)
    return result


def _dedupe(packages):
    seen, kept = set(), []
    for item in packages:
        key = (item.ecosystem, normalize(item.ecosystem, item.name))
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def _unquote(token):
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token

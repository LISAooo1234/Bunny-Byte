"""会下载并直接运行包的命令，以及安装命令里藏着的包。

- npx / pnpx / bunx / `bun x` / `npm exec` / `pnpm dlx` / `yarn dlx`：包不在本地时先下载再运行，
  拼错的名字（`npx expres`）装上的那一刻代码就执行了，比 `npm i` 更危险；
- `npm create vite` / `npm init vite` / `yarn|pnpm|bun create x` 下载的是 `create-vite`（`@s` → `@s/create`）；
- uvx / `uv tool run` / `pipx run`，以及 `--from`、`--with`（uvx、`uv run`、`uv tool install`）、`pipx inject`；
- `pip install -r 文件`、`uv pip sync 文件`、`--with-requirements 文件` 引用的文件里的包。

这里只解析命令，返回包的写法；包名规范化、判断和放行规则在 dependency_extract / dependency_guard。
"""

import subprocess
from pathlib import Path, PurePosixPath

NPX_VALUE_FLAGS = {"-p", "--package", "-c", "--call", "--prefix", "-w", "--workspace", "--registry", "--cache",
                   "--userconfig", "--node-options", "--loglevel"}
NPX_LOCAL_ONLY = {"--no-install", "--no", "--offline"}  # 只用本地已有的，不会下载
# 带值的选项来自 `uvx --help` / `uv help run`（uv 0.9）：漏一个，它的值就会被当成要运行的包，真正的包被跳过
UV_VALUE_FLAGS = {
    "--from", "--with", "-w", "--with-editable", "--with-requirements", "--with-executables-from",
    "-p", "--python", "--python-platform", "--python-preference", "--torch-backend",
    "--index", "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--index-strategy",
    "--keyring-provider", "--allow-insecure-host",
    "-c", "--constraint", "--constraints", "-b", "--build-constraint", "--build-constraints", "--override", "--overrides",
    "-P", "--upgrade-package", "--upgrade-group", "--reinstall-package", "--refresh-package", "--no-sources-package",
    "--no-binary-package", "--no-build-package", "--no-build-isolation-package", "-C", "--config-setting",
    "--config-settings-package", "--resolution", "--prerelease", "--fork-strategy", "--exclude-newer",
    "--exclude-newer-package", "--link-mode", "--cache-dir", "--color", "--config-file", "--directory", "--project",
    "--env-file", "--group", "--extra", "--no-extra", "--package", "--only-group", "--no-group",
}
UV_INDEX_FLAGS = {"--index", "--index-url", "--extra-index-url", "--default-index", "-i", "--find-links", "-f"}
PIPX_VALUE_FLAGS = {"--spec", "--python", "--pip-args", "--index-url", "-i", "--suffix", "--preinstall", "--backend",
                    "-r", "--requirement"}
CREATE_VALUE_FLAGS = {"-w", "--workspace", "--prefix"}
MAX_REQUIREMENT_BYTES = 256 * 1024


def _options(args, value_flags):
    """拆出 (选项列表 [(flag, value)], 第一个位置参数的下标)；`--` 之后的第一个参数就是位置参数。"""
    options, index = [], 0
    while index < len(args):
        item = args[index]
        if item == "--":
            return options, index + 1
        if not item.startswith("-") or item == "-":
            return options, index
        flag, eq, value = item.partition("=")
        if flag in value_flags and not eq:
            index += 1
            value = args[index] if index < len(args) else ""
        options.append((flag, value))
        index += 1
    return options, index


def npx_specs(args, shell_flags=()):
    """npx / bunx / npm exec / pnpm dlx / yarn dlx 要下载的包：[(写法, 是否只是位置参数)]。

    `-p/--package X` 指定了包时，后面的命令只是包里的可执行文件名，不是包名；只用本地包（`--no-install`）时返回空。
    位置参数可能在 node_modules/.bin 里找到同名命令（这时不下载），`-p` 的包 npm 按包名找。
    """
    options, first = _options(args, NPX_VALUE_FLAGS - set(shell_flags))
    flags = {flag for flag, _ in options}
    if flags & NPX_LOCAL_ONLY:
        return []
    packages = [(value, False) for flag, value in options if flag in {"-p", "--package"} and value]
    if packages:
        return packages
    if flags & set(shell_flags):
        # pnpm dlx -c 'expres --help'：没给 --package 时 pnpm 用命令的第一个词当包名
        words = args[first].split() if first < len(args) else []
        return [(words[0], False)] if words else []
    if flags & {"-c", "--call"}:
        return []  # `npx -c 'cmd'`：没有 -p 时只在本地找命令
    return [(args[first], True)] if first < len(args) else []


def create_spec(args, package_name):
    """`npm create vite@latest app` → `create-vite`；`@vue` → `@vue/create`；`@s/x` → `@s/create-x`。"""
    _, first = _options(args, CREATE_VALUE_FLAGS)
    if first >= len(args):
        return ""
    spec = args[first]
    if spec.startswith("@") and "/" not in spec:
        scope = "@" + spec[1:].split("@", 1)[0]
        return f"{scope}/create" if len(scope) > 1 else ""
    name = package_name(spec)
    if not name:
        return ""
    if name.startswith("@"):
        scope, _, rest = name.partition("/")
        return f"{scope}/create-{rest}"
    return f"create-{name}"


def uv_specs(args, mode):
    """uvx / `uv tool run`（mode="run_tool"）、`uv tool install`（"install"）、`uv run`（"run"）。

    返回 (包, 索引地址, requirements 文件)。`--from X` 时位置参数是命令名；`uv run` 的位置参数是脚本。
    """
    options, first = _options(args, UV_VALUE_FLAGS)
    froms = [value for flag, value in options if flag == "--from" and value]
    withs = [part for flag, value in options if flag in {"--with", "-w", "--with-editable"} for part in value.split(",") if part]
    files = [value for flag, value in options if flag == "--with-requirements" and value]
    urls = [value for flag, value in options if flag in UV_INDEX_FLAGS and value]
    command = [] if froms or mode == "run" or first >= len(args) else [args[first]]
    return froms + command + withs, urls, files


def pipx_specs(verb, args):
    """`pipx run [--spec X] app`、`pipx inject PKG DEP… [-r 文件]`（只算 DEP）。返回 (包, requirements 文件)。"""
    if verb == "run":
        options, first = _options(args, PIPX_VALUE_FLAGS)
        specs = [value for flag, value in options if flag == "--spec" and value]
        return (specs or args[first : first + 1]), []
    positional, files, index = [], [], 0
    while index < len(args):
        flag, eq, value = args[index].partition("=")
        if flag.startswith("-") and flag in PIPX_VALUE_FLAGS:
            if not eq:
                index += 1
                value = args[index] if index < len(args) else ""
            if flag in {"-r", "--requirement"} and value:
                files.append(value)
        elif not flag.startswith("-"):
            positional.append(args[index])
        index += 1
    return positional[1:], files


def requirement_files(read, files, base="."):
    """读 `-r` 引用的文件（再展开一层文件里的 `-r`）：返回 [(文件路径, 文本或 None)]。

    文本为 None = 读不到（不存在、在工作区外、超过 256KB、URL）：守卫没法知道里面装什么。
    """
    seen, result = set(), []
    queue = [(item if "://" in item else str(PurePosixPath(base) / item), 0) for item in files]
    while queue:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        text = None if "://" in path else read(path)
        result.append((path, text))
        if text is None:
            continue
        if depth >= 1 or PurePosixPath(path).name == "pyproject.toml":
            continue
        for line in text.splitlines():
            flag, _, rest = line.strip().replace("=", " ", 1).partition(" ")
            if flag in {"-r", "--requirement"} and rest.strip():
                queue.append((str(PurePosixPath(path).parent / rest.strip()), depth + 1))
    return result


def local_node_package(root, name, bin_name=False):
    """npx 会先用仓库（和上级目录，依赖可能被提升）里已经装好的包：这时不下载，不必检查。

    `node_modules/.bin/<名字>` 只对 `npx 名字` 这种裸位置参数算数；`-p 包`、带 scope 的名字、`npm create`
    npm 都按包名找 `node_modules/<包>/package.json`。只看到用户目录为止，不看 `~/node_modules`。
    """
    home = Path.home().resolve()
    for directory in [Path(root).resolve(), *Path(root).resolve().parents][:12]:
        if directory == home or directory == Path(directory.anchor):
            break
        modules = directory / "node_modules"
        if (modules / name / "package.json").is_file():
            return True
        if bin_name and "/" not in name and (modules / ".bin" / name).exists():
            return True
    return False


def committed_text(root, relative_path):
    """依赖文件在 HEAD 里的内容（不在 git 里或没提交时为 None）：已经提交的依赖算项目自己的决定。"""
    try:
        done = subprocess.run(
            ["git", "show", f"HEAD:{relative_path}"], cwd=root, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


MISSING = ""  # 工作区里没有这个文件：pip 会直接报错，什么也装不上


def read_workspace_file(agent):
    """返回 read(path)：工作区里、不超过 256KB 的文本文件的内容；不存在时返回 MISSING，读不了（工作区外、太大）返回 None。"""

    def read(raw):
        try:
            path = agent.path(raw)
            if not path.exists():
                return MISSING
            if not path.is_file() or path.stat().st_size > MAX_REQUIREMENT_BYTES:
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    return read


WRITERS_THAT_ONLY_READ = frozenset({"cat", "less", "head", "tail", "grep", "rg", "ls", "wc", "diff", "file", "stat", "echo", "printf"})


def files_written_by(command, installer_heads):
    """同一条命令里会写到的文件（重定向目标、tee / cp / sed -i 这类命令的参数）：守卫在命令运行前读不到它们的新内容。

    安装命令自己的参数不算（`pip install -r a.txt` 不写 a.txt）；cat / grep 这类只读命令的参数也不算。
    """
    from .shell_segments import split_segments, stage_head

    written = set()
    for segment in split_segments(command) or []:
        for stage in segment.stages:
            tokens = list(stage)
            for index, token in enumerate(tokens[:-1]):
                if token and set(token) <= set("<>&|") and ">" in token:
                    written.add(_normal(tokens[index + 1]))
            head, args = stage_head(stage)
            if head in installer_heads or head in WRITERS_THAT_ONLY_READ:
                continue
            written |= {_normal(item) for item in args if item and not item.startswith("-")}
    return written


def _normal(path):
    path = str(path).strip("'\"")
    return str(PurePosixPath(path)) if path else ""

"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import html
import importlib
import inspect
import pkgutil
import shutil
import subprocess
import textwrap
from functools import partial

from ..core.workspace import IGNORED_PATH_NAMES
from .base import RegisteredTool

CORE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "parallel_safe": True,
        "description": "列出工作区内的文件和目录。",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=2000"},
        "risky": False,
        "parallel_safe": True,
        "description": "按行号范围读取 UTF-8 文本文件。",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "parallel_safe": True,
        "description": "在工作区中搜索文本，优先使用 rg。",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "risky": True,
        "description": "在仓库根目录运行 shell 命令。",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "写入一个文本文件。",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "把文件中唯一匹配的文本块替换为新内容。",
    },
}

CORE_TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":2000}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
}

_DYNAMIC_TOOL_CACHE: tuple[dict, dict, dict, list] | None = None


def discover_tool_definitions():
    """动态发现 bunnybyte.tools 包下的工具模块。"""
    global _DYNAMIC_TOOL_CACHE
    if _DYNAMIC_TOOL_CACHE is not None:
        return _DYNAMIC_TOOL_CACHE

    specs = dict(CORE_TOOL_SPECS)
    examples = dict(CORE_TOOL_EXAMPLES)
    runners = {name: globals()[f"tool_{name}"] for name in CORE_TOOL_SPECS}
    validators = [(set(CORE_TOOL_SPECS), validate_core_tool)]

    package_name = __package__ or "bunnybyte.tools"
    package = importlib.import_module(package_name)
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name in {"base", "registry"} or name.startswith("_"):
            continue
        module = importlib.import_module(f"{package_name}.{name}")
        module_specs = {}
        module_validators = []
        for attr_name, value in vars(module).items():
            if attr_name.endswith("_TOOL_SPECS") and isinstance(value, dict):
                module_specs.update(value)
            elif attr_name.endswith("_TOOL_EXAMPLES") and isinstance(value, dict):
                examples.update(value)
            elif attr_name.startswith("tool_") and callable(value):
                runners[attr_name.removeprefix("tool_")] = value
            elif attr_name.startswith("validate_") and attr_name.endswith("_tool") and callable(value):
                module_validators.append(value)
        if module_specs:
            specs.update(module_specs)
            for validator in module_validators:
                validators.append((set(module_specs), validator))

    missing = sorted(set(specs) - set(runners))
    if missing:
        raise RuntimeError(f"missing tool runners: {', '.join(missing)}")
    _DYNAMIC_TOOL_CACHE = specs, examples, runners, validators
    return _DYNAMIC_TOOL_CACHE


def build_tool_registry(agent):
    specs, _, runners, _ = discover_tool_definitions()
    return {
        name: RegisteredTool(
            name=name,
            schema=spec["schema"],
            description=spec["description"],
            risky=bool(spec["risky"]),
            runner=partial(runners[name], agent),
            parallel_safe=bool(spec.get("parallel_safe", False)),
        )
        for name, spec in specs.items()
    }


def tool_example(name):
    _, examples, _, _ = discover_tool_definitions()
    return examples.get(name, "")


def tool_specs():
    specs, _, _, _ = discover_tool_definitions()
    return dict(specs)


def validate_tool(agent, name, args):
    args = args or {}
    _, _, _, validators = discover_tool_definitions()
    for names, validator in validators:
        if name not in names:
            continue
        _call_validator(validator, agent, name, args)
        return


def _call_validator(validator, agent, name, args):
    signature = inspect.signature(validator)
    parameter_count = len(signature.parameters)
    if parameter_count == 3:
        return validator(agent, name, args)
    if parameter_count == 2:
        return validator(name, args)
    return validator(args)


def validate_core_tool(agent, name, args):

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 2000))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        agent.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = agent.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item
        for item in sorted(
            path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
        )
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(agent.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 2000))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[start - 1 : end]
    actual_end = start + len(selected) - 1 if selected else start - 1
    body = "\n".join(
        f"{number:>4}: {line}"
        for number, line in enumerate(selected, start=start)
    )
    relative = path.relative_to(agent.root).as_posix()
    eof = len(lines) == 0 or actual_end >= len(lines)
    escaped_relative = html.escape(relative, quote=True)
    meta = (
        f'<read_file_meta path="{escaped_relative}" start="{start}" end="{actual_end}" '
        f'returned_lines="{len(selected)}" total_lines="{len(lines)}" '
        f'eof="{str(eof).lower()}" />'
    )
    if not body:
        if len(lines) == 0:
            body = "(empty file)"
        else:
            body = f"(no lines returned; file has {len(lines)} lines)"
    return f"# {relative}\n{body}\n{meta}"


def tool_search(agent, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = agent.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=agent.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = (
        [path]
        if path.is_file()
        else [
            item
            for item in path.rglob("*")
            if item.is_file()
            and not any(
                part in IGNORED_PATH_NAMES
                for part in item.relative_to(agent.root).parts
            )
        ]
    )
    for file_path in files:
        for number, line in enumerate(
            file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(agent, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    runner = getattr(agent, "sandbox_runner", None)
    if runner is None:
        result = subprocess.run(
            command,
            cwd=agent.root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
            # 目的是减少敏感信息被意外带进命令执行环境的风险。
            env=agent.shell_env(),
        )
    else:
        result = runner.run(
            command,
            cwd=agent.root,
            env=agent.shell_env(),
            timeout=timeout,
        )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"


def tool_patch_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(agent.root)}"


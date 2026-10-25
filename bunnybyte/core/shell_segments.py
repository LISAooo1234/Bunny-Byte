"""把一条 shell 命令按 ; && || & 换行拆成段，每段再按管道拆成阶段。

给 tool_policy（判断是不是纯查看命令）和 verification（判断退出码属于哪条命令）共用。
用 shlex（非 posix 模式，token 保留引号）而不是正则，这样引号里的 `&&`、`;` 不会被误拆成分隔符。
"""

import os
import re
import shlex
from dataclasses import dataclass

READ_COMMANDS = frozenset({"cat", "less", "head", "tail", "grep", "rg", "find", "ls"})
# 既不算"查看"也不算"干活"的命令：单独出现照常放行，和查看命令串在一起时也不能让整条命令变成"真在做事"。
NEUTRAL_COMMANDS = frozenset(
    {"cd", "echo", "printf", "true", "pwd", "export", "set", ":", "date", "exit", "sleep", "which", "type"}
    | {"wc", "sort", "uniq", "nl", "cut", "tr", "file", "stat", "tree", "basename", "dirname", "realpath", "whoami"}
)
WRAPPER_COMMANDS = frozenset({"env", "time", "timeout", "sudo", "nice", "nohup", "exec"})
DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
PUNCTUATION_CHARS = set(";&|<>()\n")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")
SEPARATOR_CHARS = set(";&|\n")
PIPE_TOKENS = {"|", "|&"}
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
# shlex 解析不了时（引号没闭合等）回退到的旧规则：只认"主命令位置"的查看命令。
LEGACY_READ_RE = re.compile(r"(?:^|;|&&|\|\|)\s*(?:cat|less|head|tail|grep|rg|find|ls)(?:\s|$)")


@dataclass(frozen=True)
class Segment:
    stages: tuple  # 管道的各阶段，每阶段是一个 token 列表
    separator: str  # 紧跟在这一段后面的分隔符；最后一段为 ""


def drop_heredoc_bodies(command):
    """去掉 heredoc 正文：正文是数据不是命令，里面的 `cat x` 或单引号不该影响判断。"""
    kept, delimiter = [], None
    for line in str(command or "").split("\n"):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        kept.append(line)
        match = HEREDOC_RE.search(line)
        if match:
            delimiter = match.group(2)
    return "\n".join(kept)


def split_segments(command):
    """返回 Segment 列表；引号没闭合等解析失败时返回 None，调用方自己回退。"""
    lexer = shlex.shlex(drop_heredoc_bodies(command), posix=False, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""  # shlex 默认把 # 当注释会吞掉换行；这里自己处理，只有词首的 # 才是注释
    segments, stages, stage = [], [], []
    comment = False

    def close(separator):
        if stage:
            stages.append(list(stage))
            stage.clear()
        if stages:
            segments.append(Segment(tuple(tuple(item) for item in stages), separator))
            stages.clear()
        elif segments and separator:
            segments[-1] = Segment(segments[-1].stages, separator)

    try:
        while True:
            token = lexer.get_token()
            if not token:
                break
            for piece in _split_punctuation(token):
                if comment and "\n" not in piece:
                    continue
                comment = False
                if piece.startswith("#"):
                    comment = True
                elif piece in PIPE_TOKENS:
                    if stage:
                        stages.append(list(stage))
                        stage.clear()
                elif set(piece) <= SEPARATOR_CHARS:
                    close("\n" if "\n" in piece else piece)
                else:
                    stage.append(piece)
    except ValueError:
        return None
    close("")
    return segments


def _split_punctuation(token):
    """shlex 会把紧挨着的标点合成一个 token（如 `);`、`)&&(`）；括号单独拆出来，分隔符才认得出。"""
    if set(token) <= PUNCTUATION_CHARS:
        return re.findall(r"[()]|[^()]+", token)
    return [token]


def stage_head(tokens):
    """一个阶段真正执行的命令名和它的参数，去掉括号、变量赋值和 env/time/sudo 这类包装。"""
    items = [item for item in tokens if item not in ("(", ")", "{", "}")]
    index = 0
    while index < len(items):
        item = items[index]
        if ASSIGNMENT_RE.match(item):
            index += 1
        elif os.path.basename(item) in WRAPPER_COMMANDS:
            index += 1
            while index < len(items) and (items[index].startswith("-") or DURATION_RE.match(items[index])):
                index += 1
        else:
            break
    if index >= len(items):
        return "", []
    return os.path.basename(items[index]), items[index + 1 :]


def is_pure_lookup(command):
    """整条命令只是在查看/搜索工作区（没有任何真正干活的命令）。管道里任何一个阶段在干活，整段就算干活。"""
    segments = split_segments(command)
    if segments is None:
        return bool(LEGACY_READ_RE.search(str(command or "")))
    reads = work = False
    for segment in segments:
        for index, stage in enumerate(segment.stages):
            name = stage_head(stage)[0]
            if not name or name in NEUTRAL_COMMANDS:
                continue
            if name in READ_COMMANDS:
                reads = reads or index == 0
            else:
                work = True
    return reads and not work

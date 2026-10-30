"""Detect credential-looking values in free text (tool output, pastes, diffs).

只认格式很明确的令牌（AKIA…、ghp_…、sk-ant-…、PEM 私钥块），以及 `.env` 风格的
`GITHUB_TOKEN=…` 行。宁可漏掉，也不把普通代码、测试夹具里的 `api_key = "test"`、
哈希、`max_tokens = 4096` 当成密钥：误伤会让模型看到一堆占位符。
"""

import re
from dataclasses import dataclass

MIN_DOTENV_SECRET_LEN = 8

TOKEN_PATTERNS = (
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----"
    )),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("stripe", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
)

# `.env` / YAML 行：大写变量名以 KEY/TOKEN/SECRET/PASSWORD 等结尾，`=` 或 `: ` 后面是值，值后只能是行尾或 `# 注释`。
# 名字前允许任意非标识符字符，这样 read_file 行号、`cat -n`、rg 的 `.env:3:`、git diff 的 `+` 都能认出来。
DOTENV_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:export[ \t]+)?(?P<name>[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS))"
    r"(?:=|:[ \t]+)(?P<quote>[\"']?)(?P<value>[^\s\"'#]+)(?P=quote)[ \t]*(?:#.*)?$",
    re.MULTILINE,
)
# ~/.aws/credentials 这类 ini：小写键名、不带引号
INI_SECRET_PATTERN = re.compile(
    r"^[^\S\n]*(?:\S*[\t:+ ])?(?:aws_secret_access_key|aws_session_token)[ \t]*=[ \t]*(?P<value>[A-Za-z0-9/+=]{16,})[ \t]*$",
    re.MULTILINE,
)
# 配置文件里整行的 `api_key = "…"`（.bunnybyte.toml、YAML、JSON）；只认这几个键名、带引号、≥16 字符且字母数字混合
CONFIG_KEY_PATTERN = re.compile(
    r"""^(?:[ \t]*\d+: )?[ \t]*["']?(?:api_?key|apikey|auth_token|access_token|secret_key|client_secret)["']?"""
    r"""[ \t]*[=:][ \t]*(?P<quote>["'])(?P<value>[^\s"']{16,})(?P=quote)[ \t]*,?[ \t]*$""",
    re.MULTILINE | re.IGNORECASE,
)
_NOT_A_VALUE = re.compile(r"^(?:\$|%|<|\{|\.\.\.|\*+$|x+$|changeme$|your[_-])", re.IGNORECASE)


@dataclass(frozen=True)
class SecretMatch:
    start: int
    end: int
    kind: str


def find_secrets(text):
    """Return non-overlapping spans of secret values, sorted by position."""
    text = str(text or "")
    found = []
    for kind, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            found.append(SecretMatch(match.start(), match.end(), kind))
    for match in DOTENV_PATTERN.finditer(text):
        value = match.group("value")
        if len(value) >= MIN_DOTENV_SECRET_LEN and not _NOT_A_VALUE.match(value):
            found.append(SecretMatch(match.start("value"), match.end("value"), match.group("name")))
    for match in INI_SECRET_PATTERN.finditer(text):
        found.append(SecretMatch(match.start("value"), match.end("value"), "aws_secret"))
    for match in CONFIG_KEY_PATTERN.finditer(text):
        value = match.group("value")
        if any(ch.isalpha() for ch in value) and any(ch.isdigit() for ch in value) and not _NOT_A_VALUE.match(value):
            found.append(SecretMatch(match.start("value"), match.end("value"), "api_key"))
    return _non_overlapping(found)


def _non_overlapping(matches):
    matches.sort(key=lambda item: (item.start, -(item.end - item.start)))
    kept = []
    for item in matches:
        if kept and item.start < kept[-1].end:
            continue
        kept.append(item)
    return kept

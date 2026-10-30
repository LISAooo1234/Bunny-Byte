"""Mask secrets with stable placeholders and put the real values back for file edits.

模型、session JSON、trace 里只出现 `<secret:GITHUB_TOKEN#3f9a0c-1>`；同一个值在同一个进程里
永远是同一个占位符（前缀缓存不受影响）。模型把占位符原样写进 write_file / patch_file 时，
落盘前换回真实值，所以“看不到密钥”不会把文件改坏。占位符带本进程的随机 nonce：仓库里
字面写着的 `<secret:...>`（测试、文档）不会被误换；重启 / resume 之后旧 nonce 的占位符
一律拒绝写入，而不是写进一个错的值。run_shell 里出现占位符直接拒绝：换回去等于把密钥
交给 `git commit -m`、`curl -d`、`echo`。
"""

import re
import secrets
import threading

from .secret_patterns import SecretMatch, find_secrets

PLACEHOLDER_PATTERN = re.compile(r"<secret:(?P<label>[A-Za-z0-9_]+)#(?P<nonce>[0-9a-f]{6})-(?P<index>\d+)>")
RESTORED_ARGS = {"write_file": ("content",), "patch_file": ("old_text", "new_text")}
MIN_KNOWN_SECRET_LEN = 8
STRUCTURE_CACHE_LIMIT = 20_000  # session JSON 每次 save 都整份重写；历史里的字符串大多没变，别每次重扫


class SecretPlaceholderError(ValueError):
    pass


class SecretVault:
    """One per process by default, so workers, skill forks and dream runs share placeholders."""

    def __init__(self, nonce=None):
        self.nonce = nonce or secrets.token_hex(3)
        self._by_value = {}
        self._by_placeholder = {}
        self._lock = threading.Lock()
        self._known_cache = ((), None)
        self._structure_cache = (None, {})

    @property
    def issued(self):
        return bool(self._by_value)

    def placeholder(self, value, label):
        with self._lock:
            existing = self._by_value.get(value)
            if existing:
                return existing
            label = re.sub(r"[^A-Za-z0-9_]", "_", str(label or "secret")) or "secret"
            token = f"<secret:{label}#{self.nonce}-{len(self._by_value) + 1}>"
            self._by_value[value] = token
            self._by_placeholder[token] = value
            return token

    def mask(self, text, known=()):
        """Replace known values (`known` = [(label, value)]) and detected tokens with placeholders."""
        text = str(text)
        if not text:
            return text
        labels = {value: label for label, value in known if _maskable_known_value(value)}
        protected = [(m.start(), m.end()) for m in PLACEHOLDER_PATTERN.finditer(text)]  # 已是占位符的不再遮第二遍
        spans = []
        pattern = self._known_pattern(labels)
        if pattern is not None:
            spans = [SecretMatch(m.start(), m.end(), labels.get(m.group(), "")) for m in pattern.finditer(text)]
        spans.extend(find_secrets(text))
        if not spans:
            return text
        spans.sort(key=lambda item: (item.start, -(item.end - item.start)))  # 稳定排序：同一位置已知值优先
        parts, cursor = [], 0
        for span in spans:
            if span.start < cursor or any(span.start < end and start < span.end for start, end in protected):
                continue
            value = text[span.start:span.end]
            parts.append(text[cursor:span.start])
            parts.append(self.placeholder(value, span.kind))
            cursor = span.end
        parts.append(text[cursor:])
        return "".join(parts)

    def mask_structure(self, value, known=()):
        """Mask every string inside a JSON-like structure (never the serialized text: escaping would break it)."""
        cache_key = (tuple(sorted(value for _, value in known if value)), len(self._by_value))
        with self._lock:
            if self._structure_cache[0] != cache_key:
                self._structure_cache = (cache_key, {})
            cache = self._structure_cache[1]
        return self._mask_cached(value, known, cache)

    def _mask_cached(self, value, known, cache):
        if isinstance(value, dict):
            return {key: self._mask_cached(item, known, cache) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._mask_cached(item, known, cache) for item in value]
        if not isinstance(value, str):
            return value
        masked = cache.get(value)
        if masked is None:
            masked = self.mask(value, known)
            if len(cache) < STRUCTURE_CACHE_LIMIT:
                cache[value] = masked
        return masked

    def restore(self, text, stale_nonces=()):
        """Put real values back; refuse placeholders this process can no longer resolve."""
        def replace(match):
            token = match.group(0)
            nonce = match.group("nonce")
            if nonce == self.nonce and token in self._by_placeholder:
                return self._by_placeholder[token]
            if nonce == self.nonce or nonce in stale_nonces:
                raise SecretPlaceholderError(
                    f"{token} 对应的密钥已不在内存里（会话恢复或重启之后旧占位符失效），"
                    "请重新 read_file 拿到新的占位符再写"
                )
            return token  # 别的会话 / 仓库里字面写着的占位符，原样保留
        return PLACEHOLDER_PATTERN.sub(replace, str(text))

    def mentions(self, text, stale_nonces=()):
        return any(
            match.group("nonce") == self.nonce or match.group("nonce") in stale_nonces
            for match in PLACEHOLDER_PATTERN.finditer(str(text))
        )

    def _known_pattern(self, labels):
        with self._lock:
            values = set(self._by_value) | set(labels)
        if not values:
            return None
        key = tuple(sorted(values, key=lambda item: (-len(item), item)))
        with self._lock:
            cached_key, cached = self._known_cache
        if cached_key == key:
            return cached
        pattern = re.compile("|".join(re.escape(item) for item in key))
        with self._lock:
            self._known_cache = (key, pattern)
        return pattern


def _maskable_known_value(value):
    """环境变量里的值也可能是 `postgres`、`secret` 这种普通单词；按子串替换会把路径、命令、代码都遮掉。"""
    value = str(value or "")
    if len(value) < MIN_KNOWN_SECRET_LEN:
        return False
    return not value.isalpha() or len(value) >= 20


def unmask_args(agent, name, args):
    """Arguments the tool really runs with: placeholders restored for file edits, refused in shell."""
    vault = getattr(agent, "secret_vault", None)
    if vault is None or not isinstance(args, dict):
        return args
    stale = tuple(agent.session.get("secret_nonces", ()) or ())
    if name == "run_shell" and vault.mentions(args.get("command", ""), stale):
        raise SecretPlaceholderError(
            "命令里有密钥占位符 <secret:…>；为了不把真实值交给 shell，run_shell 不会替换它。"
            "需要把密钥写进文件请用 write_file / patch_file（会自动换回真实值），命令里请改用环境变量"
        )
    fields = RESTORED_ARGS.get(name, ())
    if fields and _is_runtime_data_path(agent, args.get("path", "")):
        return args  # 记忆、计划等写进 .bunnybyte/ 的内容保留占位符，真实值不落到运行数据里
    if not any(isinstance(args.get(field), str) and "<secret:" in args[field] for field in fields):
        return args
    restored = dict(args)
    for field in fields:
        if isinstance(restored.get(field), str):
            restored[field] = vault.restore(restored[field], stale)
    return restored


def _is_runtime_data_path(agent, path):
    try:
        resolved = agent.path(path)
        return (agent.root / ".bunnybyte") in (resolved, *resolved.parents)
    except Exception:
        return False


def execute_masked(agent, tool, args):
    """Run a tool with real values and hand back output with secrets already masked."""
    return agent.redact_text(tool.execute(unmask_args(agent, tool.name, args)).content)


DEFAULT_VAULT = SecretVault()

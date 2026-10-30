"""模型价目表：把一次模型调用上报的 token 换算成美元估算。

价格来自各家官网的公开价目（见 PRICES_AS_OF），单位是每百万 token 的美元。经网关（OneAPI 之类）转发时
实际账单以网关为准，这里只是估算。用户可以在配置里用 `[pricing."模型名"]` 覆盖或补充。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

PRICES_AS_OF = "2026-10-31"
PRICE_FIELDS = ("input", "output", "cached_input", "cache_write")


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cached_input: float | None = None  # 缓存命中的输入；None 表示按普通输入算
    cache_write: float | None = None  # 写入缓存的输入（Anthropic 风格单独上报）
    long_context: "Price | None" = None  # 提示超过 long_threshold token 时整次调用改用这档价格
    long_threshold: int = 0
    peak_hours: bool = False  # DeepSeek：北京白天的高峰时段价格翻倍，表里存的是低谷价


def _claude(input_price, output, cached, write, **extra):
    return Price(input_price, output, cached, write, **extra)


_OPUS_4 = _claude(5, 25, 0.5, 6.25)
_SONNET_4 = _claude(3, 15, 0.3, 3.75)
_FABLE_5_1 = _claude(10, 50, 0.25, 12.5)
_FABLE_5 = _claude(10, 50, 1, 12.5)
_DEEPSEEK_FLASH = Price(0.15, 0.60, 0.003, peak_hours=True)

BUILTIN_PRICES = {
    # Anthropic（platform.claude.com/docs/en/about-claude/pricing）
    "claude-fable-5-1": _FABLE_5_1,
    "claude-mythos-5-1": _FABLE_5_1,
    "claude-fable-5": _FABLE_5,
    "claude-mythos-5": _FABLE_5,
    "claude-opus-5-5": _claude(4, 20, 0.2, 5),
    "claude-opus-5": _OPUS_4,
    "claude-opus-4-8": _OPUS_4,
    "claude-opus-4-7": _OPUS_4,
    "claude-opus-4-6": _OPUS_4,
    "claude-opus-4-5": _OPUS_4,
    "claude-opus-4-1": _claude(15, 75, 1.5, 18.75),
    "claude-opus-4": _claude(15, 75, 1.5, 18.75),
    "claude-sonnet-5-5": _claude(2, 10, 0.1, 2.5),
    "claude-sonnet-5": _claude(2, 10, 0.2, 2.5),
    "claude-sonnet-4-6": _SONNET_4,
    "claude-sonnet-4-5": _SONNET_4,
    "claude-sonnet-4": _SONNET_4,
    "claude-haiku-5-5": _claude(
        0.1, 0.5, 0.01, 0.125, long_context=_claude(0.5, 2.5, 0.05, 0.625), long_threshold=100_000
    ),
    "claude-haiku-4-5": _claude(1, 5, 0.1, 1.25),
    # DeepSeek（api-docs.deepseek.com/quick_start/pricing）；旧名 deepseek-v4-flash 按 Flash 计费
    "deepseek-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-flash-vision-exp": _DEEPSEEK_FLASH,
    "deepseek-v4-1-flash": _DEEPSEEK_FLASH,
    "deepseek-v4-pro": Price(0.66, 1.98, 0.022, peak_hours=True),
    # OpenAI（developers.openai.com/api/docs/pricing，标准档）；超过 272K 的长上下文价和 gpt-5.6 的缓存写入价没有计入，会偏低
    "gpt-5-6-sol": Price(4, 20, 0.4),
    "gpt-5-6-terra": Price(2, 12, 0.2),
    "gpt-5-6-luna": Price(0.2, 1.2, 0.02),
    "gpt-5-5": Price(5, 30, 0.5),
    "gpt-5-4": Price(2.5, 15, 0.25),
    "gpt-5-4-mini": Price(0.75, 4.5, 0.075),
    "gpt-5-4-nano": Price(0.2, 1.25, 0.02),
}

_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]+\.)?anthropic\.")  # us. / eu. / apac. / global.
_VERSION_SUFFIX = re.compile(r"-v\d+(?::\d+)?$")
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|latest)$")


def normalize_model(name):
    """"Claude Haiku 4.5"、"claude-haiku-4-5-20251001"、"us.anthropic.claude-haiku-4-5-20251001-v1:0" 都归一成 claude-haiku-4-5。

    只做精确匹配，不按前缀猜：claude-sonnet-5 和 claude-sonnet-5-5 是两种价格。
    """
    text = str(name or "").strip().lower()
    text = text.rsplit("/", 1)[-1]
    text = _BEDROCK_PREFIX.sub("", text)
    text = _VERSION_SUFFIX.sub("", text)
    text = re.sub(r"[\s_.@]+", "-", text).strip("-")
    return _DATE_SUFFIX.sub("", text)


def parse_price_overrides(raw):
    """`[pricing."模型名"]` 表 -> ({归一化模型名: Price}, [问题描述])。"""
    prices, problems = {}, []
    if not isinstance(raw, dict):
        return prices, ([f"[pricing] 应该是表，实际是 {type(raw).__name__}"] if raw else [])
    for name, section in raw.items():
        if not isinstance(section, dict):
            problems.append(f'[pricing."{name}"] 应该是表')
            continue
        values = {}
        for field in PRICE_FIELDS:
            value = section.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                problems.append(f'[pricing."{name}"].{field} 应该是非负数字（美元 / 百万 token）')
                values = None
                break
            values[field] = float(value)
        if values is None:
            continue
        unknown = sorted(set(section) - set(PRICE_FIELDS))
        if unknown:
            problems.append(f'[pricing."{name}"] 有未知字段：{", ".join(unknown)}（可用 {", ".join(PRICE_FIELDS)}）')
        if "input" not in values or "output" not in values:
            problems.append(f'[pricing."{name}"] 至少要写 input 和 output')
            continue
        prices[normalize_model(name)] = Price(**values)
    return prices, problems


def lookup_price(model, overrides=None):
    """(Price, 来源) ；没有价格时 (None, "")。配置里的价格优先于内置表。"""
    key = normalize_model(model)
    if not key:
        return None, ""
    if overrides and key in overrides:
        return overrides[key], "[pricing] 配置"
    if key in BUILTIN_PRICES:
        return BUILTIN_PRICES[key], f"内置价目表 {PRICES_AS_OF}"
    return None, ""


def is_deepseek_peak(when):
    """DeepSeek 高峰：周一到周五 UTC 01:00–04:00 与 06:00–10:00。中国法定节假日也是低谷价，这里无法知道，会高估。"""
    when = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10)


def _count(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def call_cost(price, usage, protocol, when=None):
    """一次调用的美元估算。

    OpenAI 风格：cached_tokens 是 input_tokens 的一部分。Anthropic 风格（含 DeepSeek 的 /anthropic 端点）：
    input_tokens 只是没命中缓存的部分，cache_read / cache_creation 另外上报。
    """
    uncached = _count(usage, "input_tokens")
    cached = _count(usage, "cached_tokens")
    created = _count(usage, "cache_creation_tokens")
    output = _count(usage, "output_tokens")
    if protocol == "openai":
        uncached = max(0, uncached - cached)
    tier = price
    if price.long_context is not None and uncached + cached + created > price.long_threshold:
        tier = price.long_context  # 提示长度按全部输入算，含缓存命中与写入
    multiplier = 2 if price.peak_hours and is_deepseek_peak(when or datetime.now(timezone.utc)) else 1
    total = (
        uncached * tier.input
        + cached * (tier.cached_input if tier.cached_input is not None else tier.input)
        + created * (tier.cache_write if tier.cache_write is not None else tier.input)
        + output * tier.output
    )
    return total * multiplier / 1_000_000


def format_usd(value):
    if value is None:
        return "-"
    if value == 0:
        return "$0"
    if value < 0.1:
        return f"${value:.4f}"  # 便宜模型一轮常常只有几分钱，三位小数会把 $0.0104 和上限 $0.0100 都显示成 $0.010
    if value < 1:
        return f"${value:.3f}"
    return f"${value:.2f}"

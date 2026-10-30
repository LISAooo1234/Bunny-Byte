"""第一次运行的守门：没配 API key 时在发请求之前就讲清楚怎么配，而不是甩一个 401 的 JSON。"""

from urllib.parse import urlsplit

# 这些官方地址没有 key 一定会被拒绝；其余地址（自建网关、本地模型）可能由代理注入鉴权，只提示不拦截
OFFICIAL_HOSTS = {"api.openai.com", "api.anthropic.com", "api.deepseek.com"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
KEY_ENV_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


def missing_key_check(client):
    """返回 (level, message)：level 为 "error"（应停下）、"warn"（打一行提示继续）或 ""（没问题）。"""
    from ..providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
    from ..providers.fallback import FallbackModelClient

    if isinstance(client, FallbackModelClient):
        client = client._primary  # 鉴权错误不会切到备用，按主 client 判断
    if not isinstance(client, (OpenAICompatibleModelClient, AnthropicCompatibleModelClient)):
        return "", ""  # 测试里的脚本化模型、自定义 client 不归这里管
    if str(getattr(client, "api_key", "") or "").strip():
        return "", ""
    provider = str(getattr(client, "provider", "") or "openai")
    model = str(getattr(client, "model", "") or "")
    base_url = str(getattr(client, "base_url", "") or "")
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    if host in LOCAL_HOSTS:
        return "", ""
    if host not in OFFICIAL_HOSTS:
        return "warn", f"⚠ 没有配置 {provider} 的 API key，直接请求 {host or base_url}（网关自己处理鉴权时可忽略）。"
    env_name = KEY_ENV_NAMES.get(provider, "BUNNYBYTE_API_KEY")
    return "error", "\n".join(
        [
            f"还没有配置 {provider} 的 API key（模型 {model}，{host}），请求会被直接拒绝。",
            "  → 运行 `bunny setup` 选 provider 并保存 key（只需一次，之后任意目录可用）",
            f"  → 或设置环境变量 {env_name}（或 BUNNYBYTE_API_KEY）后重试",
            "  → 换 provider：bunny --provider deepseek；检查配置：bunny doctor",
        ]
    )


def model_error_hint(code):
    """模型错误后面追加的一行修复建议；没有对应建议时返回空串。"""
    if code == "auth_error":
        return "\n→ API key 被拒绝（401/403）：运行 `bunny doctor` 查看当前 provider 和 key，或 `bunny setup --force` 换一个 key。"
    return ""

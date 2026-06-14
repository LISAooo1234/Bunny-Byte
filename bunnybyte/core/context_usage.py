"""Context usage estimation for prompt transparency."""


DEFAULT_CONTEXT_WINDOW = 200_000
TOKEN_ESTIMATION_METHOD = "weighted_chars_cjk_aware"


def estimate_tokens(value):
    if isinstance(value, str):
        return estimate_text_tokens(value)
    return max(0, (int(value) + 3) // 4)


def estimate_text_tokens(text):
    # Provider tokenizers differ, so this is still an estimate. It is more
    # conservative than chars/4 for Chinese-heavy prompts and code/JSON.
    total = 0.0
    for char in str(text or ""):
        codepoint = ord(char)
        if _is_cjk(codepoint):
            total += 1.0
        elif char.isascii() and (char.isalnum() or char.isspace()):
            total += 0.25
        elif char.isascii():
            total += 0.5
        else:
            total += 1.0
    return max(0, int(total + 0.999999))


def _is_cjk(codepoint):
    return (
        0x4E00 <= codepoint <= 0x9FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0xF900 <= codepoint <= 0xFAFF
    )


class ContextUsageAnalyzer:
    def __init__(self, agent):
        self.agent = agent

    def analyze(self, rendered):
        tools_chars = self._tools_chars()
        sections = {}
        for name, section in rendered.items():
            key = "current_request" if name == "current_request" else name
            chars = int(section.rendered_chars)
            text = section.rendered
            if key == "prefix":
                chars = max(0, chars - tools_chars)
                text = text[:chars]
            sections[key] = {
                "chars": chars,
                "tokens": estimate_tokens(text),
            }
        sections["tools"] = {
            "chars": tools_chars,
            "tokens": estimate_tokens(self._tools_text()),
        }
        total = sum(section["tokens"] for section in sections.values())
        window = self._context_window()
        reserved = int(getattr(self.agent, "max_new_tokens", 0) or 0)
        return {
            "estimation_method": TOKEN_ESTIMATION_METHOD,
            "model": str(getattr(getattr(self.agent, "model_client", None), "model", "")),
            "context_window": window,
            "reserved_output_tokens": reserved,
            "total_estimated_tokens": total,
            "sections": sections,
            "free_tokens": window - total - reserved,
            "auto_compact_threshold": int(window * 0.8),
        }

    def _context_window(self):
        model = str(getattr(getattr(self.agent, "model_client", None), "model", "")).lower()
        if "1m" in model or "1000000" in model:
            return 1_000_000
        return DEFAULT_CONTEXT_WINDOW

    def _tools_text(self):
        lines = []
        for name, tool in self.agent.available_tools().items():
            fields = ", ".join(f"{key}: {value}" for key, value in tool.schema.items())
            risk = "approval required" if tool.risky else "safe"
            lines.append(f"- {name}({fields}) [{risk}] {tool.description}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _tools_chars(self):
        return len(self._tools_text())

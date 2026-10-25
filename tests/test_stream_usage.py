from unittest.mock import patch

from bunnybyte.providers import AnthropicCompatibleModelClient


class FakeStream:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self.lines)


def complete(lines):
    client = AnthropicCompatibleModelClient(
        model="deepseek-test",
        base_url="https://api.example.invalid/anthropic",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )
    with patch("urllib.request.urlopen", return_value=FakeStream(lines)):
        return client.complete_result("hello", 64, on_delta=lambda _delta: None)


def test_streamed_anthropic_input_tokens_come_from_message_start():
    result = complete(
        [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":120,"output_tokens":1,"cache_read_input_tokens":80}}}\n\n',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<final>OK</final>"}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":9}}\n\n',
            b'data: {"type":"message_stop"}\n\n',
        ]
    )

    assert result.text == "<final>OK</final>"
    assert result.metadata["input_tokens"] == 120
    assert result.metadata["output_tokens"] == 9
    assert result.metadata["cached_tokens"] == 80
    assert result.metadata["cache_hit"] is True


def test_streamed_anthropic_usage_without_message_start_still_works():
    result = complete(
        [
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<final>OK</final>"}}\n\n',
            b'data: {"type":"message_delta","usage":{"input_tokens":11,"output_tokens":3}}\n\n',
            b'data: {"type":"message_stop"}\n\n',
        ]
    )

    assert result.metadata["input_tokens"] == 11
    assert result.metadata["output_tokens"] == 3
    assert "cached_tokens" not in result.metadata

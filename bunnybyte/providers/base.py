"""Provider-facing result types."""

from dataclasses import dataclass, field
import inspect


@dataclass(frozen=True)
class ModelResult:
    text: str
    metadata: dict = field(default_factory=dict)


def complete_model(model_client, prompt, max_new_tokens, **kwargs):
    if hasattr(model_client, "complete_result"):
        method = model_client.complete_result
        return method(
            prompt, max_new_tokens, **_supported_kwargs(method, kwargs)
        )
    method = model_client.complete
    text = method(prompt, max_new_tokens, **_supported_kwargs(method, kwargs))
    metadata = dict(getattr(model_client, "last_completion_metadata", {}) or {})
    return ModelResult(text=str(text), metadata=metadata)


def _supported_kwargs(method, kwargs):
    if not kwargs:
        return {}
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return dict(kwargs)
    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }

"""Streaming model completion bridge for Engine."""

import queue
import threading

from ..providers.base import complete_model


def complete_model_with_deltas(
    engine,
    task_state,
    prompt,
    prompt_cache_key=None,
    prompt_cache_retention=None,
    tools=None,
):
    agent = engine.runtime
    events = queue.Queue()
    done = object()
    state = {"result": None, "error": None}

    def on_delta(delta):
        text = str(delta or "")
        if text:
            events.put(text)

    def worker():
        try:
            state["result"] = complete_model(
                agent.model_client,
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                tools=tools,
                on_delta=on_delta,
            )
        except Exception as exc:
            state["error"] = exc
        finally:
            events.put(done)

    thread = threading.Thread(
        target=worker,
        name=f"bunnybyte-model-{task_state.run_id}",
        daemon=True,
    )
    thread.start()
    total_chars = 0
    while True:
        item = events.get()
        if item is done:
            break
        total_chars += len(item)
        yield {
            "type": "model_delta",
            "run_id": task_state.run_id,
            "content": item,
            "total_chars": total_chars,
        }
    thread.join()
    if state["error"] is not None:
        raise state["error"]
    return state["result"]

import json

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.cli import handle_repl_command
from bunnybyte.providers.base import ModelResult
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_successful_turn_writes_structured_turn_index_and_event(tmp_path):
    agent = build_agent(tmp_path, ["<final>Use transcript recall with compact summaries.</final>"])

    assert agent.ask("Index compact transcript recall keywords") == "Use transcript recall with compact summaries."

    turn_index = agent.session["turn_index"][agent.current_task_state.task_id]
    assert 1 <= len(turn_index["keywords"]) <= 5
    assert "summary" in turn_index
    assert turn_index["event_ids"]

    events = read_jsonl(agent.session_event_bus.path)
    indexed = [event for event in events if event["event"] == "turn_indexed"]
    assert indexed
    assert indexed[-1]["turn_id"] == agent.current_task_state.task_id
    assert indexed[-1]["keywords"] == turn_index["keywords"]


def test_compacted_hidden_turn_is_recalled_into_prompt(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Stored the ledger checkpoint decision.</final>",
            "<final>Recent unrelated answer.</final>",
            "<summary>Old work was compacted.</summary>",
        ],
    )

    assert agent.ask("Remember the ledger checkpoint decision for transcript recall") == "Stored the ledger checkpoint decision."
    hidden_turn = agent.current_task_state.task_id
    assert agent.ask("Discuss a recent unrelated topic") == "Recent unrelated answer."

    agent.compact_history(trigger="manual", keep_recent_turns=1)
    prompt, metadata = agent._build_prompt_and_metadata("What did we decide about ledger checkpoint?")

    assert "Recovered transcript snippets:" in prompt
    assert "ledger checkpoint decision" in prompt
    assert hidden_turn in metadata["transcript_recall"]["selected_turns"]
    assert metadata["transcript_recall"]["selected_count"] == 1


def test_visible_turn_is_not_duplicated_by_automatic_recall(tmp_path):
    agent = build_agent(tmp_path, ["<final>Visible ledger answer.</final>"])

    assert agent.ask("Visible ledger checkpoint context") == "Visible ledger answer."
    prompt, metadata = agent._build_prompt_and_metadata("ledger checkpoint")

    assert "Visible ledger checkpoint context" in prompt
    assert metadata["transcript_recall"]["selected_count"] == 0


def test_recall_slash_command_searches_current_session_transcript(tmp_path):
    agent = build_agent(tmp_path, ["<final>Manual recall can find ledger checkpoint.</final>"])
    assert agent.ask("Store manual ledger checkpoint transcript") == "Manual recall can find ledger checkpoint."

    handled, should_exit, output = handle_repl_command(agent, "/recall ledger checkpoint")

    assert handled is True
    assert should_exit is False
    assert "## Transcript Recall" in output
    assert "ledger checkpoint" in output
    assert "```text" in output


def test_chinese_query_recalls_compacted_transcript_turn(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>已经记录压缩召回关键词方案。</final>",
            "<final>最近话题。</final>",
            "<summary>旧中文上下文已压缩。</summary>",
        ],
    )

    assert agent.ask("讨论压缩召回关键词方案") == "已经记录压缩召回关键词方案。"
    hidden_turn = agent.current_task_state.task_id
    assert agent.ask("聊一个最近话题") == "最近话题。"

    agent.compact_history(trigger="manual", keep_recent_turns=1)
    prompt, metadata = agent._build_prompt_and_metadata("之前压缩召回关键词怎么说的？")

    assert "Recovered transcript snippets:" in prompt
    assert "压缩召回关键词方案" in prompt
    assert hidden_turn in metadata["transcript_recall"]["selected_turns"]


def test_real_model_client_can_refine_turn_index_keywords(tmp_path):
    class IndexingModelClient:
        supports_prompt_cache = False
        supports_native_tools = False
        model = "indexing-model"

        def __init__(self):
            self.calls = []
            self.last_completion_metadata = {}

        def complete_result(self, prompt, max_new_tokens, **kwargs):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                return ModelResult("<final>Main final.</final>")
            return ModelResult(
                '{"keywords":["model-keyword","turn-index","transcript-recall","compact"],'
                '"summary":"Model generated turn index summary.",'
                '"entities":["bunnybyte/core/transcript_recall.py"]}'
            )

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    agent = BunnyByte(
        model_client=IndexingModelClient(),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    assert agent.ask("Please index transcript recall") == "Main final."

    turn_index = agent.session["turn_index"][agent.current_task_state.task_id]
    assert len(agent.model_client.calls) == 2
    assert turn_index["indexer"] == "model_refined_v1"
    assert turn_index["keywords"][0] == "model-keyword"
    assert turn_index["summary"] == "Model generated turn index summary."


def test_selected_memory_scope_recalls_only_chosen_session(tmp_path):
    source = build_agent(tmp_path, ["<final>Selected source recorded.</final>"])
    assert source.ask("Selected session memory says use the ledger banana decision") == "Selected source recorded."
    source_id = source.session["id"]

    other = build_agent(tmp_path, ["<final>Other source recorded.</final>"])
    assert other.ask("Other session memory says use the orange decision") == "Other source recorded."

    current = build_agent(tmp_path, [])
    current.set_memory_scope("selected", [source_id])
    prompt, metadata = current._build_prompt_and_metadata("What was the banana decision?")

    assert "Selected session memory says use the ledger banana decision" in prompt
    assert f"session={source_id}" in prompt
    assert "orange decision" not in prompt
    assert metadata["relevant_memory"]["memory_scope"]["mode"] == "selected"
    assert metadata["relevant_memory"]["selected_count"] == 0
    assert metadata["transcript_recall"]["selected_count"] >= 1


def test_memory_scope_off_disables_durable_relevant_memory(tmp_path):
    agent = build_agent(tmp_path, [])
    promoted, _ = agent.memory.promote_durable(
        [("project-conventions", "Use scoped memory instead of global recall.")]
    )
    assert promoted
    agent.set_memory_scope("off")

    prompt, metadata = agent._build_prompt_and_metadata("What memory recall convention should I follow?")

    assert "Use scoped memory instead of global recall." not in prompt
    assert metadata["relevant_memory"]["memory_scope"]["mode"] == "off"
    assert metadata["relevant_memory"]["selected_count"] == 0


def test_memory_scope_slash_command_selects_sessions_by_history_index(tmp_path):
    source = build_agent(tmp_path, ["<final>Scoped CLI source.</final>"])
    assert source.ask("Scoped CLI transcript has the papaya decision") == "Scoped CLI source."
    source_id = source.session["id"]

    current = build_agent(tmp_path, [])
    handled, should_exit, output = handle_repl_command(current, "/memory-scope selected 1")

    assert handled is True
    assert should_exit is False
    assert "## Memory Scope" in output
    assert source_id in output
    assert current.session["memory_scope"]["mode"] == "selected"
    assert current.session["memory_scope"]["session_ids"] == [source_id]

    prompt, _metadata = current._build_prompt_and_metadata("papaya decision")
    assert "Scoped CLI transcript has the papaya decision" in prompt

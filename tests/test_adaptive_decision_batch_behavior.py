import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "overlay/hermes-core"
sys.path.insert(0, str(CORE))

from tools.clarify_gateway import run_sequential_batch_fallback

clarify_namespace = {}
clarify_source = (CORE / "tools/clarify_tool.py").read_text(encoding="utf-8")
exec(clarify_source.partition("# --- Registry ---")[0], clarify_namespace)
TIMEOUT_RESPONSE = clarify_namespace["TIMEOUT_RESPONSE"]
_normalize_questions = clarify_namespace["_normalize_questions"]
_run_batch = clarify_namespace["_run_batch"]


def test_one_question_batch_is_rejected_before_discord_delivery():
    normalized, error = _normalize_questions([{"question": "Only question"}])

    assert normalized is None
    assert error == (
        "questions requires two to five items; use the top-level question fields "
        "for one question"
    )


def test_clarify_schema_requires_two_questions_for_a_batch():
    schema_source = (CORE / "tools/clarify_tool.py").read_text(encoding="utf-8")
    questions_schema = schema_source[schema_source.index('"questions": {') :]

    assert '"minItems": 2' in questions_schema


def _install_clarify_timeout_module(monkeypatch):
    module = types.ModuleType("tools.clarify_tool")
    module.TIMEOUT_RESPONSE = TIMEOUT_RESPONSE
    monkeypatch.setitem(sys.modules, "tools.clarify_tool", module)


def test_sequential_fallback_keeps_json_list_answer_and_only_stops_on_timeout(monkeypatch):
    _install_clarify_timeout_module(monkeypatch)
    questions = [
        {"qid": "q0", "question": "Pick", "choices": ["A", "B"], "multi_select": True},
        {"qid": "q1", "question": "Why", "choices": None, "multi_select": False},
    ]
    replies = iter(['["A", "B"]', "because"])

    result = json.loads(run_sequential_batch_fallback(questions, lambda _item: next(replies)))

    assert result == {"answers": {"q0": '["A", "B"]', "q1": "because"}}


def test_sequential_fallback_uses_exact_canonical_timeout_sentinel(monkeypatch):
    _install_clarify_timeout_module(monkeypatch)
    questions = [
        {"qid": "q0", "question": "First"},
        {"qid": "q1", "question": "Second"},
    ]
    calls = []

    def ask(item):
        calls.append(item["qid"])
        return TIMEOUT_RESPONSE

    result = json.loads(run_sequential_batch_fallback(questions, ask))

    assert result == {"answers": {}, "timed_out": True}
    assert calls == ["q0"]


def test_sequential_fallback_stops_on_gateway_bounded_wait_timeout(monkeypatch):
    _install_clarify_timeout_module(monkeypatch)
    questions = [
        {"qid": "q0", "question": "First"},
        {"qid": "q1", "question": "Second"},
    ]
    calls = []

    def ask(item):
        calls.append(item["qid"])
        return "[user did not respond within 5m]"

    result = json.loads(run_sequential_batch_fallback(questions, ask))

    assert result == {"answers": {}, "timed_out": True}
    assert calls == ["q0"]


def test_native_batch_answer_shape_preserves_canonical_multi_select_values():
    normalized = [
        {
            "qid": "q0",
            "id": "features",
            "question": "Which features?",
            "choices": ["Fast (Recommended)", "Safe"],
            "choices_offered": ["Fast", "Safe"],
            "multi_select": True,
        }
    ]

    def native_callback(_question, _choices, *, questions):
        assert questions is normalized
        return json.dumps({"answers": {"q0": ["Fast", "Safe"]}})

    result = json.loads(_run_batch(normalized, native_callback, "Choose features"))

    assert result["responses"][0]["user_response"] == ["Fast", "Safe"]
    assert result["responses"][0]["choices_offered"] == ["Fast", "Safe"]


def test_native_batch_forwards_structured_decision_surface():
    normalized = [
        {
            "qid": "q0", "id": None, "question": "First", "choices": None,
            "choices_offered": None, "multi_select": False,
        },
        {
            "qid": "q1", "id": None, "question": "Second", "choices": None,
            "choices_offered": None, "multi_select": False,
        },
    ]
    surface = {"title": "Deployment decisions", "kind": "batch"}

    def native_callback(_question, _choices, *, questions, surface):
        assert questions is normalized
        assert surface == {"title": "Deployment decisions", "kind": "batch"}
        return json.dumps({"answers": {"q0": "A", "q1": "B"}})

    result = json.loads(
        _run_batch(normalized, native_callback, "Choose", surface=surface)
    )

    assert [item["user_response"] for item in result["responses"]] == ["A", "B"]


def test_discord_review_close_and_empty_selection_guards_are_explicit():
    source = (
        ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"
    ).read_text(encoding="utf-8")
    start = source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    end = source.index("    class ClarifyChoiceView(discord.ui.View):", start)
    batch = source[start:end]

    assert "class ReviewConfirmationView(discord.ui.View):" in batch
    assert 'label="Submit answers"' in batch
    assert 'label="Back"' in batch
    assert "ephemeral=True" in batch
    assert "self.answers = {}" in batch
    assert "cancel_all=True" in batch
    assert "reviewed_answers=reviewed_answers" in batch
    assert "and current_answers != reviewed_answers" in batch
    assert "if stale_review:" in batch
    assert "Answers changed after this review opened" in batch
    assert "_prefix_within_utf16_limit(review_content, 1900)" in batch
    assert "if self.resolved:" in batch
    assert "if not selected:" in batch


def test_discord_batch_never_reports_success_when_gateway_rejects_resolution():
    source = (
        ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"
    ).read_text(encoding="utf-8")
    start = source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    end = source.index("    class ClarifyChoiceView(discord.ui.View):", start)
    batch = source[start:end]

    assert "gateway_resolved = resolve_gateway_clarify(" in batch
    assert "if gateway_resolved:" in batch
    assert "Response was not accepted" in batch


def test_discord_batch_timeout_unblocks_gateway_with_typed_timeout_result():
    source = (
        ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"
    ).read_text(encoding="utf-8")
    start = source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    end = source.index("    class ClarifyChoiceView(discord.ui.View):", start)
    batch = source[start:end]

    assert '"answers": {}, "timed_out": True' in batch
    assert "resolve_gateway_clarify(self.clarify_id, timeout_payload)" in batch

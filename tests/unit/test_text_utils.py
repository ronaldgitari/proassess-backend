"""Unit tests for parsing/coercion helpers + grader edge cases + timestamp serialization."""
from datetime import datetime, timezone, date

import pytest

from rag.augmentor import (
    extract_json_array, spread_correct_answers, _to_text,
    validate_mcq_item, validate_personality_item,
)
from rag.grader import extract_json_object, grade_context
from timeutil import iso_utc


# ── JSON extraction (robust to markdown fences / surrounding noise) ──
def test_extract_json_array_from_fenced_block():
    raw = 'Here are the questions:\n```json\n[{"q": 1}, {"q": 2}]\n```\nThanks!'
    assert extract_json_array(raw) == [{"q": 1}, {"q": 2}]


def test_extract_json_array_no_array_raises():
    with pytest.raises(ValueError):
        extract_json_array("there is no array here")


def test_extract_json_object_from_noise():
    assert extract_json_object('noise {"verdict": "sufficient"} trailing')["verdict"] == "sufficient"


def test_extract_json_object_no_object_raises():
    with pytest.raises(ValueError):
        extract_json_object("nothing structured")


# ── MCQ answer balancing (correctness must survive the shuffle) ───
def test_spread_correct_answers_preserves_correctness_and_strips_prefix():
    items = [{"options": ["A) alpha", "B) beta", "C) gamma", "D) delta"], "correct_index": 0}
             for _ in range(8)]
    out = spread_correct_answers(items)
    for it in out:
        # The correct option's TEXT is still at the (new) correct index, prefix stripped.
        assert it["options"][it["correct_index"]] == "alpha"
        assert sorted(it["options"]) == ["alpha", "beta", "delta", "gamma"]
    # Across 8 questions the correct answer lands in all four positions.
    assert {it["correct_index"] for it in out} == {0, 1, 2, 3}


# ── list/dict → text coercion (GPT sometimes returns a rubric as a list) ──
def test_to_text_coercion():
    assert _to_text(None) is None
    assert _to_text("plain") == "plain"
    assert _to_text(["a", "b"]) == "- a\n- b"
    assert _to_text(["- already bulleted"]) == "- already bulleted"   # no double-prefix
    assert "k: v" in _to_text({"k": "v"})


# ── Item validators ───────────────────────────────────────────────
def test_validate_mcq_item():
    good = {"question": "q", "options": ["a", "b", "c", "d"], "correct_index": 1,
            "explanation": "e", "source_reference": "s", "difficulty": 3}
    assert validate_mcq_item(good)
    assert not validate_mcq_item({"question": "q"})                      # missing keys
    assert not validate_mcq_item({**good, "options": ["a", "b"]})        # wrong option count
    assert not validate_mcq_item({**good, "correct_index": 5})           # out of range


def test_validate_personality_item():
    assert validate_personality_item({"statement": "I plan ahead.", "dimension": "tactics", "keyed_pole": "J"})
    assert not validate_personality_item({"statement": "x", "dimension": "mind", "keyed_pole": "Z"})   # bad pole
    assert not validate_personality_item({"statement": "x", "dimension": "bogus", "keyed_pole": "E"})  # bad dim


# ── Grader edge cases (no real LLM call) ──────────────────────────
async def test_grade_context_empty_docs_is_insufficient():
    out = await grade_context(topic="t", context_prompt=None, domain="technical",
                              num_questions=5, docs=[])
    assert out["verdict"] == "insufficient"
    assert out["missing"]   # records what was missing


async def test_grade_context_fails_open_on_llm_error(monkeypatch, fake_chat):
    from langchain.schema import Document
    import rag.grader as grader
    # If the grader call/parse errors, it must fail OPEN ("sufficient") so a
    # grader hiccup never blocks otherwise-good generation.
    monkeypatch.setattr(grader, "ChatOpenAI", fake_chat(raises=RuntimeError("boom")))
    out = await grade_context(topic="t", context_prompt=None, domain="technical",
                              num_questions=5, docs=[Document(page_content="x" * 80, metadata={})])
    assert out["verdict"] == "sufficient"


# ── Timestamp serialization (UTC 'Z' marker) ──────────────────────
def test_iso_utc():
    assert iso_utc(None) is None
    assert iso_utc(datetime(2026, 6, 3, 12, 0, 0)).endswith("Z")            # naive → marked UTC
    assert iso_utc(datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)).endswith("Z")
    assert iso_utc(date(2026, 6, 3)) == "2026-06-03"                        # plain date, no tz

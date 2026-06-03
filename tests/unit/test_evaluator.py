"""Unit tests for deterministic scoring + personality aggregation (rag/evaluator.py)."""
from rag.evaluator import (
    evaluate_mcq, compute_summary, compute_personality_result, AnswerResult,
)


# ── MCQ scoring (deterministic) ───────────────────────────────────
def test_mcq_correct_scores_100():
    r = evaluate_mcq(question_id="q1", correct_index=2, given_index=2)
    assert r.is_correct is True and r.score == 100.0


def test_mcq_wrong_scores_0():
    r = evaluate_mcq(question_id="q1", correct_index=2, given_index=0)
    assert r.is_correct is False and r.score == 0.0


def test_mcq_unanswered_scores_0():
    r = evaluate_mcq(question_id="q1", correct_index=2, given_index=None)
    assert r.is_correct is False and r.score == 0.0


# ── Summary aggregation ───────────────────────────────────────────
def test_summary_empty():
    s = compute_summary([])
    assert s == {"score_pct": 0.0, "questions_correct": 0, "questions_total": 0}


def test_summary_mixed():
    results = [
        AnswerResult("a", True, 100.0),
        AnswerResult("b", False, 0.0),
        AnswerResult("c", True, 80.0),
    ]
    s = compute_summary(results)
    assert s["questions_total"] == 3
    assert s["questions_correct"] == 2
    assert s["score_pct"] == 60.0   # (100 + 0 + 80) / 3


# ── Personality aggregation ───────────────────────────────────────
def _q(dim, direction, ai):
    return {"dimension": dim, "direction": direction, "answer_index": ai}


def test_personality_all_strongly_agree_positive_poles():
    # One question per dimension, all keyed positive, all "Strongly Agree" (6).
    qs = [_q(d, 1, 6) for d in ("mind", "energy", "nature", "tactics", "identity")]
    res = compute_personality_result(qs)
    # mind=E, energy=N, nature=T, tactics=J, identity=A
    assert res["base_code"] == "ENTJ"
    assert res["type_code"] == "ENTJ-A"
    assert res["type_name"] == "Commander"
    # Each dimension fully toward its positive pole.
    assert all(d["winning_pct"] == 100.0 for d in res["dimensions"])


def test_personality_blank_answers_treated_neutral():
    qs = [_q(d, 1, None) for d in ("mind", "energy", "nature", "tactics", "identity")]
    res = compute_personality_result(qs)
    # Neutral → no lean; ties resolve to the positive pole, 50% toward it.
    assert all(d["toward_pos_pct"] == 50.0 for d in res["dimensions"])
    assert len(res["dimensions"]) == 5


def test_personality_direction_flips_pole():
    # Strongly agree (6) but keyed negative (direction -1) → toward the negative pole.
    res = compute_personality_result([_q("mind", -1, 6)])
    mind = next(d for d in res["dimensions"] if d["key"] == "mind")
    assert mind["letter"] == "I"   # Introverted (negative pole of mind)

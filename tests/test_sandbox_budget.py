from hivemind.sandbox.budget import Budget


def test_budget_initial_state():
    b = Budget(max_calls=5, max_tokens=1000)
    assert b.check() is None
    s = b.summary()
    assert s["calls"] == 0
    assert s["max_calls"] == 5
    assert s["total_tokens"] == 0
    assert s["max_tokens"] == 1000


def test_budget_record_calls():
    b = Budget(max_calls=3, max_tokens=100_000)
    b.record(prompt_tokens=100, completion_tokens=50)
    assert b.check() is None

    s = b.summary()
    assert s["calls"] == 1
    assert s["prompt_tokens"] == 100
    assert s["completion_tokens"] == 50
    assert s["total_tokens"] == 150


def test_budget_exhausted_by_calls():
    b = Budget(max_calls=2, max_tokens=100_000)
    b.record(prompt_tokens=10, completion_tokens=10)
    b.record(prompt_tokens=10, completion_tokens=10)

    err = b.check()
    assert err is not None
    assert "2 LLM calls" in err


def test_budget_exhausted_by_tokens():
    b = Budget(max_calls=100, max_tokens=500)
    b.record(prompt_tokens=300, completion_tokens=250)

    err = b.check()
    assert err is not None
    assert "500 tokens" in err


def test_budget_not_exhausted_at_boundary():
    b = Budget(max_calls=2, max_tokens=1000)
    b.record(prompt_tokens=100, completion_tokens=100)
    # 1 call used, 200 tokens used — still OK
    assert b.check() is None


def test_budget_summary_updates():
    b = Budget(max_calls=10, max_tokens=10000)
    b.record(prompt_tokens=100, completion_tokens=50)
    b.record(prompt_tokens=200, completion_tokens=100)

    s = b.summary()
    assert s["calls"] == 2
    assert s["prompt_tokens"] == 300
    assert s["completion_tokens"] == 150
    assert s["total_tokens"] == 450

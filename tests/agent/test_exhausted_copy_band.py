"""Regression tests for cooldown copy in issue #140737."""

from agent.turn_failure_copy import _NEXT_STEPS_RETRY, exhausted_copy


def _copy(seconds=None, *, attempts=3):
    return exhausted_copy(
        reason="rate_limit", label="Acme", attempts=attempts,
        summary="429: slow down", reset_seconds=seconds,
    )


def test_known_one_to_two_minute_cooldown_does_not_say_wait_a_minute():
    for seconds in (60, 61, 90, 119.9):
        out = _copy(seconds)
        assert "its limit resets soon" in out
        assert "reset time below" in out
        assert _NEXT_STEPS_RETRY not in out
        assert "switch models with /model" in out


def test_cooldowns_below_one_minute_keep_existing_copy():
    for seconds in (0, 1, 59.9):
        assert _copy(seconds) == _copy(None)


def test_two_minutes_and_above_keep_existing_named_window():
    for seconds in (120, 121, 300):
        out = _copy(seconds)
        assert "its usage limit resets in" in out
        assert "Send /retry after that" in out
        assert "reset time below" not in out


def test_first_attempt_copy_keeps_existing_named_window():
    out = _copy(61, attempts=1)
    assert "its usage limit resets in" in out
    assert "Send /retry after that" in out
    assert "reset time below" not in out


def test_overloaded_503_with_retry_after_never_claims_a_usage_limit():
    """A 503/529 'overloaded' carrying Retry-After is not a usage limit.

    The eager reset resolution is gated on limit-classified reasons, so an
    overloaded verdict must neither name a limit window nor promise a reset
    line the stamp gate refuses to append — even though Retry-After is
    parseable (the premise below proves the eager value exists)."""
    import httpx
    import openai
    from types import SimpleNamespace

    import agent.turn_recovery as tr
    from agent.error_classifier import classify_api_error

    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(503, json={"error": {"message": "overloaded"}},
                              headers={"Retry-After": "90"}, request=request)
    error = openai.APIStatusError("overloaded", response=response, body={"error": {"message": "overloaded"}})
    import time as _time
    agent = SimpleNamespace(provider="acme", base_url="https://api.example.com", model="m",
                            api_key="k", log_prefix="", _rate_limit_state=None,
                            _extract_api_error_context=lambda *_a, **_k: {"reset_at": _time.time() + 90},
                            _has_pending_fallback=lambda: False, _dump_api_request_debug=lambda *a, **k: None,
                            _flush_status_buffer=lambda *a, **k: None, _summarize_api_error=lambda e: str(e),
                            _emit_status=lambda *a, **k: None, _persist_session=lambda *a, **k: None, _plines=lambda *a, **k: None,
                            _vprint=lambda *a, **k: None, _buffer_status=lambda *a, **k: None, _buffer_vprint=lambda *a, **k: None,
                            _emit_diagnostic_status=lambda *a, **k: None, _buffer_diagnostic_status=lambda *a, **k: None,
                            _try_activate_fallback=lambda: False)
    classified = classify_api_error(error, provider="acme", model="m",
                                    base_url="https://api.example.com", api_key="k")
    # Premise: the Retry-After IS parseable — the eager resolution could fire.
    assert tr.limit_reset_epoch(agent, error) is not None
    assert classified.reason not in tr._LIMIT_REASONS

    result = tr.max_retries_exhausted_result(
        agent, error, classified, attempts=3, is_rate_limited=False, error_msg=str(error),
        api_kwargs=None, api_messages=[], messages=[], conversation_history=[], api_call_count=1,
        approx_tokens=10, provider="acme", base_url="https://api.example.com", model="m")
    assert "usage limit resets" not in result["final_response"]
    assert "reset time below" not in result["final_response"]

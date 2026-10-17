"""Explicit local-session launch routes disable provider/model fallback.

Regression: ``hermes chat --provider corp-ai --model default-corp-llm`` stored the
launch route in the session policy, but the gateway only treated later ``/model``
overrides as pins. A failed corp request then silently crossed into OpenAI.
"""

import json
from types import SimpleNamespace


def _policy(*, pinned_policy="disable", **request):
    return SimpleNamespace(
        request_json=json.dumps(request),
        safe_mode=False,
        ignore_user_config=False,
        config=lambda _authority=None: {
            "fallback": {"pinned_model_policy": pinned_policy},
            "fallback_providers": [{"provider": "openai-codex", "model": "gpt-x"}],
        },
    )


def test_launch_receipt_pin_detection_covers_base_url_and_excludes_defaults():
    from gateway.session_policy import launch_route_pinned

    assert launch_route_pinned(_policy(base_url="https://corp.example/v1")) is True
    assert launch_route_pinned(_policy(provider="auto")) is False
    assert launch_route_pinned(_policy(follow_profile_config=True)) is False


def test_saved_explicit_launch_route_is_a_gateway_pin(monkeypatch):
    from gateway.session_execution import GatewaySessionAgentMixin

    runner = SimpleNamespace(_peek_session_state=lambda _key: None)
    obj = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
    obj._runner = runner
    obj._ctx = SimpleNamespace(source=object())
    policy = _policy(model="default-corp-llm", provider="corp-ai")
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda _runner, _source: policy)

    assert obj._session_route_pinned("agent:main:local:dm:test", policy.config()) is True


def test_worker_construct_drops_chain_for_saved_explicit_route():
    from gateway.session_worker_construct import _fallback_chain

    policy = _policy(model="default-corp-llm", provider="corp-ai")
    authority = SimpleNamespace(runner=SimpleNamespace(
        _refresh_fallback_model=lambda: [{"provider": "openai-codex", "model": "gpt-x"}]))

    assert _fallback_chain(authority, policy) is None


def test_pinned_launch_with_inherit_policy_keeps_chain():
    from gateway.session_worker_construct import _fallback_chain

    policy = _policy(pinned_policy="inherit", model="default-corp-llm", provider="corp-ai")
    chain = [{"provider": "openai-codex", "model": "gpt-x"}]
    authority = SimpleNamespace(runner=SimpleNamespace(_refresh_fallback_model=lambda: chain))

    assert _fallback_chain(authority, policy) == chain


def test_default_following_route_keeps_chain():
    from gateway.session_worker_construct import _fallback_chain

    policy = _policy(follow_profile_config=True)
    chain = [{"provider": "openai-codex", "model": "gpt-x"}]
    authority = SimpleNamespace(runner=SimpleNamespace(_refresh_fallback_model=lambda: chain))

    assert _fallback_chain(authority, policy) == chain

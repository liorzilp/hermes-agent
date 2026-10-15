"""A session that explicitly pins its model runs with NO fallback chain (``-m``/``--provider``).

``fallback.pinned_model_policy`` (default ``disable``): the operator chose that provider and
model — exactly what a chain entry is not. Same rule a pinned cron job / delegated child follows
(``scoped_fallback_chain``). The default route keeps the configured chain.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli  # module-level import: its bootstrap must run before the home io-guard engages
from hermes_cli.config import get_config_path

_CHAIN = [{"provider": "xai-oauth", "model": "grok-4.6"}]


@pytest.mark.parametrize(
    ("pinned", "policy", "expected"),
    [
        (True, "disable", []),        # pinned session: no chain (the new default)
        (False, "disable", _CHAIN),   # default route: chain kept
        (True, "inherit", _CHAIN),    # explicit opt-out of the pin rule
    ],
)
def test_pinned_session_chain_decision(pinned, policy, expected):
    """The init-time decision (cli_init_mixin) and the per-turn sync share the policy helper."""
    from hermes_cli.fallback_config import get_fallback_chain, pinned_model_disables_fallback

    effective = {"fallback": {"pinned_model_policy": policy},
                 "fallback_providers": list(_CHAIN)}
    chain = (
        get_fallback_chain(effective)
        if not (pinned and pinned_model_disables_fallback(effective))
        else [])
    assert chain == expected


def _write_config(config_text: str) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text, encoding="utf-8")


def test_pinned_agent_gets_empty_chain_at_turn_sync():
    """``_sync_fallback_chain_with_config`` clears a pinned session's live agent chain.

    Real imports against the real CLI mixin: a pinned session's agent ends with an empty chain
    even though config.yaml still declares one; an unpinned session keeps it.
    """
    _write_config(
        "fallback:\n"
        "  pinned_model_policy: disable\n"
        "fallback_providers:\n"
        "  - provider: xai-oauth\n"
        "    model: grok-4.6\n")

    pinned_shell = SimpleNamespace(_explicit_model_override=True, _fallback_model=None)
    agent = SimpleNamespace(
        _fallback_chain=list(_CHAIN), _fallback_model=_CHAIN[0], _fallback_index=0,
        _fallback_activated=False, _rate_limited_until=0, _unavailable_fallback_keys=set())
    cli.HermesCLI._sync_fallback_chain_with_config(pinned_shell, agent)
    assert agent._fallback_chain == []
    assert agent._fallback_model is None
    assert pinned_shell._fallback_model == []

    unpinned_shell = SimpleNamespace(_explicit_model_override=False, _fallback_model=None)
    agent2 = SimpleNamespace(
        _fallback_chain=[], _fallback_model=None, _fallback_index=0,
        _fallback_activated=False, _rate_limited_until=0, _unavailable_fallback_keys=set())
    cli.HermesCLI._sync_fallback_chain_with_config(unpinned_shell, agent2)
    assert agent2._fallback_chain == _CHAIN
    assert unpinned_shell._fallback_model == _CHAIN

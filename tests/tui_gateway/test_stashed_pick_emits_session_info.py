"""A model picked mid-turn must paint the pick the moment it is queued.

``config.set model`` on a *running* session cannot swap the agent in place (the
worker thread reads ``agent.model`` / ``agent.client`` on every iteration), so
it stashes the pick in ``session["pending_model_switch"]`` and
``_apply_pending_model_switch`` applies it at the next turn start.

The composer pill is server-truth: it repaints only on ``session.info``. The
stash branch returned ``deferred: True`` and emitted nothing, so from the
click until the next turn started the pill kept showing the model the session
was leaving. ``_session_info`` already reports the queued pick's
``display_model`` / ``display_provider`` (precisely so the end-of-turn settle
doesn't blip the UI back), and ``_apply_pending_model_switch`` already
re-syncs the pill off a pick the session never adopted -- the stash moment was
the only edge of that lifecycle that never published.
"""

import threading
import types

from tui_gateway import server

UNGUARDED_MODEL = "anthropic/claude-sonnet-4.6"


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(
            model="old-model", provider="anthropic", base_url="", api_mode="chat_completions",
            reasoning_config=None, service_tier=None, session_id="session-key"),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def _config_set_model(value):
    return server.handle_request(
        {"id": "1", "method": "config.set",
         "params": {"session_id": "sid", "key": "model", "value": value}})


def test_stashed_pick_emits_session_info_with_the_pick(monkeypatch):
    """Queuing a mid-turn pick must publish session.info immediately.

    Without the emit the pill keeps the old model from the click until the
    next turn starts, which reads to the user as "the switch didn't work".
    """
    emitted = []
    monkeypatch.setattr(
        server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "_apply_model_switch ran on the busy path -- it would race the worker thread")

    monkeypatch.setattr(server, "_apply_model_switch", _must_not_run)
    session = _session(running=True)
    server._sessions["sid"] = session
    try:
        resp = _config_set_model(UNGUARDED_MODEL)

        assert resp["result"]["deferred"] is True
        assert session["pending_model_switch"]["display_model"] == UNGUARDED_MODEL

        infos = [payload for (event, sid, payload) in emitted
                 if event == "session.info" and sid == "sid" and payload]
        assert infos, (
            "a queued pick emitted no session.info -- the pill keeps showing "
            "the old model from the click until the next turn starts"
        )
        assert infos[-1].get("model") == UNGUARDED_MODEL, (
            "the stash-time session.info must report the queued pick (display_model), "
            "not the model the session is leaving"
        )
    finally:
        server._sessions.pop("sid", None)

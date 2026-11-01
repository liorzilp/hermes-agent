"""The review handoff must wipe a stale requeue stamp (#119070).

A rate-limited requeue stamps ``tasks.last_failure_error`` without counting a
failure, so nothing on the failure path ever clears it. Once a later run of the
same task succeeds and hands off to review, that stamp is stale, yet
``check_respawn_guard`` still matches it (``blocker_auth``) on every
review-lane tick: no reviewer ever spawns, the card sits parked forever with
``consecutive_failures = 0``, and the breaker never trips. These tests pin the
handoff-side wipe that ``request_review`` performs, mirroring the completion
reset in ``complete_task``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_review_handoff_clears_stale_rate_limit_stamp(kanban_home, monkeypatch):
    """A successful run that hands off to review must wipe the stale requeue
    stamp, and the review lane must not be parked on it forever."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    now = 5_000_000
    stamp = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale-stamp", assignee="a")
        # Rate-limited attempt: run recorded, card requeued ready with the
        # stamp (what _sweep_dead_workers persists for a quota-wall exit).
        kb.claim_task(conn, tid)
        run1 = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run1),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            (stamp, tid),
        )
        conn.commit()

        # The retry succeeds and hands the card to review.
        kb.claim_task(conn, tid)
        run2 = kb.get_task(conn, tid).current_run_id
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.request_review(conn, tid, expected_run_id=run2, summary="ready for review")

        # Precondition (harness, stays red if broken): the run history is the
        # incident shape, a rate-limited attempt superseded by the handoff.
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,))]
        assert outcomes == ["rate_limited", "review_requested"]

        # The handoff is a success transition: the stamp it would leave behind
        # is stale, and the guard must not park the review lane on it forever.
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.last_failure_error is None
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_changes_requested_handoff_clears_stale_rate_limit_stamp(kanban_home, monkeypatch):
    """A reviewer handing the card back must also wipe the stale requeue
    stamp: without the wipe the ready lane parks the implementer forever
    (#119070 sibling on the changes_requested path)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    now = 5_000_000
    stamp = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale-stamp-changes", assignee="impl")
        # Implementer finishes and hands the card to a reviewer.
        kb.claim_task(conn, tid)
        run1 = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, expected_run_id=run1, summary="ready", reviewer="rev")

        # The reviewer's first run is reaped rate-limited: the requeue stamps
        # the card (what _sweep_dead_workers persists for a quota-wall exit).
        kb.claim_review_task(conn, tid)
        run2 = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run2),
        )
        conn.execute(
            "UPDATE tasks SET status='review', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
            (stamp, tid),
        )
        conn.commit()

        # The retry reviewer runs and requests changes: hand back to the
        # implementer, parent gating reapplied.
        kb.claim_review_task(conn, tid)
        run3 = kb.get_task(conn, tid).current_run_id
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        ok, implementer = kb.request_changes(conn, tid, reason="rework", expected_run_id=run3)
        assert ok and implementer == "impl"

        # Precondition (harness, stays red if broken): the run history is the
        # sibling incident shape, a rate-limited reviewer run superseded by
        # the changes handoff.
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,))]
        assert outcomes == ["review_requested", "rate_limited", "changes_requested"]

        # The reviewer's own run finished, so the stamp is stale: the ready
        # lane must not park the implementer's retry on it forever.
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.last_failure_error is None
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_manual_review_reopen_clears_superseded_rate_limit_stamp(kanban_home, monkeypatch):
    """Reopening an already wedged review card must recover the ready lane.

    Older versions could leave a rate-limit stamp beside a later
    ``review_requested`` outcome. A manual reopen supersedes that text and must
    not carry ``blocker_auth`` into the implementer's ready lane.
    """
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    stamp = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale-review-reopen", assignee="impl")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, expected_run_id=run_id, summary="ready", reviewer="rev",
        )
        conn.execute(
            "UPDATE tasks SET last_failure_error=? WHERE id=?",
            (stamp, tid),
        )
        conn.commit()

        assert kbd.check_respawn_guard(conn, tid, lane="review") == "blocker_auth"
        assert kb.reopen_review_task(conn, tid)

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.last_failure_error is None
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None

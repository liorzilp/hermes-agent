"""Review-lane transitions: the ``request_review`` handoff (``running``/``ready`` -> ``review``)
and ``request_changes`` (reviewer run -> back to the implementer), plus the reviewer-provenance
lookup.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any
from typing import Optional


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = _kb.redact_review_value(summary)
    metadata = _kb.redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _kb._merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with _kb.write_txn(conn):
            if not _kb._parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _kb._claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _kb._prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _kb._canonical_assignee(reviewer)
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            params: tuple[Any, ...] = (
                *(() if reviewer is None else (reviewer,)), task_id,
                *(() if expected_run_id is None else (int(expected_run_id),)),
            )
            # The handoff is a success transition: whatever failure text a
            # requeue stamped (a rate-limited requeue stamps without counting
            # a failure) is now stale, yet ``check_respawn_guard`` would keep
            # matching it as ``blocker_auth`` on every review-lane tick, so no
            # reviewer would ever spawn (#119070). Wipe it here, the same way
            # ``complete_task`` wipes it on completion; the failure counter
            # itself keeps accumulating across review rounds.
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       last_failure_error = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + run_guard,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if isinstance(metadata, dict):
                staged_copies = _kb._stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _kb._end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            payload: dict = {
                "summary": _kb._first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            staged = _kb._cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _kb._append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except Exception:
        if staged_copies:
            _kb._discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _kb._latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _kb._json_dict(_kb._row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def request_changes(
    conn: sqlite3.Connection, task_id: str, *, reason: str, expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Close an active reviewer run (claimed from ``review``) and hand the task
    back to the implementer from the latest ``review_requested`` event, parent
    gating reapplied. Returns ``(ok, implementer | reason)``."""
    reason = str(_kb.redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with _kb.write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _kb._latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _kb._json_dict(_kb._row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _kb._latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _kb._nonblank_str(_kb._json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = _kb._canonical_assignee(_kb._nonblank_str(task_row["assignee"]))

        new_status = _kb._landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        # The reviewer's own run just finished, so a requeue stamp it left
        # behind is stale; wiping it here keeps check_respawn_guard from
        # parking the implementer's retry as blocker_auth forever (#119070).
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL,
                   last_failure_error = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _kb._end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        _kb._append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
            },
            run_id=run_id,
        )
    return True, implementer


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb

"""Cross-process claim exclusivity for the usage poller.

The claim's read/check/persist sequence must be atomic across processes:
two gateways (or a gateway plus a CLI poll) deciding "not throttled" at the
same moment both poll Anthropic — and every early poll RESTARTS the sliding
429 window, the exact storm this module exists to prevent. A process-local
threading lock cannot see the other process's claim; the OS-level file
lock must.
"""

from __future__ import annotations

import multiprocessing
import sys

import pytest

import agent.account_usage_cache as auc


def _child_claim(home: str, key_bits: str, barrier, queue) -> None:
    """Claim the refresh for *key_bits* under *home*; report (ok, reason)."""
    import os
    import time

    os.environ["HERMES_HOME"] = home
    for mod in [m for m in list(sys.modules) if m == "agent.account_usage_cache"]:
        del sys.modules[mod]
    import importlib

    importlib.import_module("agent.account_usage_cache")

    # Coordinated start: both children attempt the claim at the same barrier.
    barrier.wait(timeout=30.0)
    key = tuple(key_bits.split("|"))
    import agent.account_usage_cache as fresh

    ok, reason = fresh._claim_refresh_reason(key, time.monotonic())
    queue.put((os.getpid(), ok, reason))


def test_two_processes_cannot_both_claim_the_poll(tmp_path, monkeypatch):
    """Two racing processes, one fresh lockout state: exactly one 'claimed'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Point this parent's cache at the same home so the lock file is shared.
    monkeypatch.setattr(auc, "_lockout_file", lambda: tmp_path / "usage-poll-lockouts.json")

    # "spawn" is the repository's cross-platform pattern: Windows has no "fork"
    # context, and a fresh interpreter re-resolves the lockout file under its own
    # HERMES_HOME exactly like a real second process.
    ctx = multiprocessing.get_context("spawn")
    key_bits = "home|anthropic|acct|https://api.anthropic.com"
    barrier = ctx.Barrier(2, timeout=30.0)
    queue = ctx.Queue()
    procs = [ctx.Process(target=_child_claim, args=(str(tmp_path), key_bits, barrier, queue))
             for _ in range(2)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=30) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)

    claims = [r for r in results if r[1] and r[2] == "claimed"]
    refused = [r for r in results if not (r[1] and r[2] == "claimed")]
    assert len(claims) == 1, f"exactly one process must claim; got {results}"
    assert len(refused) == 1, f"the other must stand down; got {results}"
    # The refusal must be a stand-down reason, never a second claim.
    assert refused[0][2] in ("throttled", "inflight"), refused


def test_claim_still_works_single_process(tmp_path, monkeypatch):
    """The file lock must not break the ordinary single-process claim."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(auc, "_lockout_file", lambda: tmp_path / "usage-poll-lockouts.json")
    import time as _time

    auc._last_try.clear()
    auc._last_try_wall.clear()
    auc._lockout_until.clear()
    auc._inflight.clear()
    key = ("home", "anthropic", "acct", "https://api.anthropic.com")
    ok, reason = auc._claim_refresh_reason(key, _time.monotonic())
    assert ok is True and reason == "claimed"


def _child_lockout(home: str, barrier, queue) -> None:
    """Record a 429 lockout (unclaimed path) while another process claims."""
    import os

    os.environ["HERMES_HOME"] = home
    for mod in [m for m in list(sys.modules) if m == "agent.account_usage_cache"]:
        del sys.modules[mod]
    import importlib

    importlib.import_module("agent.account_usage_cache")
    barrier.wait(timeout=30.0)
    import agent.account_usage_cache as fresh

    fresh.note_usage_lockout("anthropic", 900.0, base_url="https://api.anthropic.com")
    queue.put((os.getpid(), "noted"))


def test_concurrent_claim_cannot_lose_a_recorded_lockout(tmp_path, monkeypatch):
    """A lockout written by one process while another claims must survive.

    The persist is a read/merge/replace under the cross-process claim lock:
    a claim writer replacing the file between our read and our replace
    would otherwise drop the just-recorded 429 deadline."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(auc, "_lockout_file", lambda: tmp_path / "usage-poll-lockouts.json")

    ctx = multiprocessing.get_context("spawn")
    key_bits = "home|anthropic|acct2|https://api.anthropic.com"
    barrier = ctx.Barrier(2, timeout=30.0)
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_child_claim, args=(str(tmp_path), key_bits, barrier, queue)),
        ctx.Process(target=_child_lockout, args=(str(tmp_path), barrier, queue)),
    ]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=30) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)

    import json

    state = json.loads((tmp_path / "cache" / "usage-poll-lockouts.json").read_text(encoding="utf-8-sig"))
    lockouts = state.get("lockouts") or {}
    assert any(
        k.split("|")[1:2] == ["anthropic"] and k.endswith("https://api.anthropic.com")
        for k in lockouts
    ), f"the concurrent lockout was lost: {state} (results={results})"


def _child_claim_against_held_lock(home: str, key_bits, queue) -> None:
    """Claim while the PARENT process holds the file lock past its timeout."""
    import os

    os.environ["HERMES_HOME"] = home
    for mod in [m for m in list(sys.modules) if m == "agent.account_usage_cache"]:
        del sys.modules[mod]
    import importlib

    importlib.import_module("agent.account_usage_cache")
    import agent.account_usage_cache as fresh

    ok, reason = fresh._claim_refresh_reason(tuple(key_bits.split("|")), __import__("time").monotonic())
    queue.put((os.getpid(), ok, reason))


def test_genuinely_held_lock_refuses_the_claim(tmp_path, monkeypatch):
    """A lock HELD past its timeout is contention, not an unusable lock:
    the claim must stand down ('inflight'), never poll unprotected."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(auc, "_lockout_file", lambda: tmp_path / "usage-poll-lockouts.json")

    from hermes_cli.active_sessions import _FileLock

    ctx = multiprocessing.get_context("spawn")
    key_bits = "home|anthropic|acct3|https://api.anthropic.com"
    queue = ctx.Queue()
    # The CHILD resolves its own lockout file under HERMES_HOME (no parent
    # monkeypatch): <home>/cache/usage-poll-lockouts.json — hold THAT lock.
    claim_lock = tmp_path / "cache" / "usage-poll-lockouts.json.claim.lock"
    with _FileLock(claim_lock, timeout=2.0):
        proc = ctx.Process(target=_child_claim_against_held_lock, args=(str(tmp_path), key_bits, queue))
        proc.start()
        result = queue.get(timeout=60)
        proc.join(timeout=30)
    assert result[1] is False and result[2] == "inflight", result

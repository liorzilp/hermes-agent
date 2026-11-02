"""What ``model.options`` knows about subscription usage without waiting on the network.

Usage windows come from a provider API (:func:`agent.account_usage.fetch_account_usage`), so a
picker reads this cache and asks for a background refresh instead of fetching inline. Every fetch
lands here, including the one ``session.usage`` runs after each turn, so a picker is at most one
turn or one refresh interval behind.

Slots are keyed per profile home AND per account: one process may serve many profiles, and a
provider may have a pool of credentials whose usage must not contaminate each other. A fetcher
that can identify its account (a decoded Codex JWT principal) dedupes several credentials of one
account onto a single slot; fetchers that cannot get one slot per credential, never a
provider-wide singleton. The legacy provider slot (``identity_id=None``) remains the single-account
gauge: a per-turn or ``/usage`` fetch still lands there, so non-pooled chips are unchanged.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot

# A picker opened again within this long reuses what it has; usage windows move in minutes, not seconds.
REFRESH_AFTER_S = 120.0
# A snapshot older than this renders as state ``unknown`` instead of a confident gauge: a benched
# credential's windows stop updating, and reporting stale numbers as live would mislabel an
# account that may have recovered (or worsened) in the meantime.
SNAPSHOT_STALE_AFTER_S = 15 * 60.0
# Providers whose poll interval EXCEEDS the default staleness window need a matching
# threshold, or the gauge renders ``unknown`` for most of every cycle. Anthropic:
# 30-minute poll interval + slack.
STALE_AFTER_S_BY_PROVIDER: dict[str, float] = {"anthropic": 40 * 60.0}

# Providers whose usage endpoint enforces a tight server-side poll budget.
# Anthropic's OAuth usage endpoint allows only a few polls per hour and
# answers overruns with 429 + a ~1h sliding retry-after, so the network
# interval for it is deliberately far slower than the generic 2-minute
# cache throttle. All surfaces (picker, per-turn session.usage, /usage,
# /status) share this one gate — there is exactly ONE network sampler per
# provider slot, and everything else reads the cache. 15 minutes
# (owner-approved 2026-11-02): 4/hour keeps the account-limits pane
# current during active sessions; the shared 429 lockout above still
# stands every surface down together when the server says no.
PROVIDER_MIN_INTERVAL_S: dict[str, float] = {"anthropic": 900.0}

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
# slot key (home, provider, identity_id, base_url) → snapshot
_snapshots: dict[tuple[str, str, str, str], AccountUsageSnapshot] = {}
# slot key → WALL-CLOCK deadline until which the server told us to back off
# (honors a 429 ``Retry-After``; retrying earlier only restarts the window).
# Wall clock + a small on-disk mirror make the lockout effective ACROSS
# processes (gateway, TUI, CLI one-shots) — a per-process dict would let each
# restart the server's sliding window independently.
_lockout_until: dict[tuple[str, str, str, str], float] = {}


def _lockout_file():
    from pathlib import Path

    try:
        from hermes_constants import get_default_hermes_root

        home = get_default_hermes_root()
    except Exception:  # health: allow BLE001 -- config-root probe falls back to HERMES_HOME; any failure means "no home"
        import os

        home = os.environ.get("HERMES_HOME")
    if not home:
        return None
    d = Path(home) / "cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / "usage-poll-lockouts.json"


def _load_lockouts() -> None:
    """Best-effort load of persisted cross-process state (wall clock)."""
    import json

    path = _lockout_file()
    if path is None:
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key_str, deadline in (raw.get("lockouts") or {}).items():
            try:
                home_name, provider, identity, base = key_str.split("|")
            except ValueError:
                continue
            if float(deadline) > now:
                _lockout_until[(home_name, provider, identity, base)] = float(deadline)
            else:
                _lockout_until.pop((home_name, provider, identity, base), None)
        for key_str, wall in (raw.get("last_try") or {}).items():
            try:
                home_name, provider, identity, base = key_str.split("|")
            except ValueError:
                continue
            known = _last_try_wall.get((home_name, provider, identity, base))
            if known is None or float(wall) > known:
                _last_try_wall[(home_name, provider, identity, base)] = float(wall)
    except Exception:  # health: allow BLE001 -- corrupt/foreign file: start empty rather than break every fetch
        _logger.debug("usage lockout state unreadable; starting empty", exc_info=True)


# Per-THREAD: set while the calling thread holds the cross-process claim lock
# (the claim path persists inside its locked section; a second acquire on a
# fresh handle would deadlock against ourselves). A module-wide flag would
# let another thread's note_usage_lockout persist unlocked while the claim
# thread still holds the file lock.
_claim_lock_held = threading.local()
class _WarnOnce:
    """Warn-once is per PROCESS (a racy double warning does no harm); attribute
    mutation avoids both a threading.local (re-warns per thread) and a
    function-scope rebinding (shadows the module flag)."""

    warned = False


_claim_lock_warned = _WarnOnce()


def _persist_lockouts() -> None:
    """MERGE (not overwrite): other processes' entries survive our write.

    The read/merge/replace runs under the cross-process claim lock when we
    do not already hold it: a concurrent claim writer replacing the file
    between our read and our replace would otherwise DROP a just-recorded
    429 lockout deadline (``note_usage_lockout`` persists through here)."""
    import json

    if getattr(_claim_lock_held, "value", False):
        _persist_lockouts_locked()
        return
    lock_path = _claim_lock_file()
    if lock_path is None:
        _persist_lockouts_locked()
        return
    from hermes_cli.active_sessions import _FileLock
    try:
        with _FileLock(lock_path, timeout=5.0):
            _persist_lockouts_locked()
    except (RuntimeError, OSError):
        # Best-effort as before the lock existed: merging unlocked still
        # keeps our entries; the claim path (which refuses on lock failure)
        # carries the double-poll protection.
        _persist_lockouts_locked()


def _persist_lockouts_locked() -> None:
    """The merge itself; callers hold the cross-process lock (or accept best-effort)."""
    import json

    path = _lockout_file()
    if path is None:
        return
    now = time.time()
    merged_lockouts, merged_last_try = {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            merged_lockouts = raw.get("lockouts") or {}
            merged_last_try = raw.get("last_try") or {}
    except Exception:  # health: allow BLE001 -- corrupt/foreign file: keep our own entries only
        _logger.debug("usage lockout persist skipped (corrupt/foreign file); keeping own entries", exc_info=True)
    for k, v in _lockout_until.items():
        if v > now:
            merged_lockouts["|".join(k)] = v
    for k, v in _last_try_wall.items():
        key_str = "|".join(k)
        if v > float(merged_last_try.get(key_str, 0.0)):
            merged_last_try[key_str] = v
    try:
        import os
        import tempfile

        payload = json.dumps({"lockouts": merged_lockouts, "last_try": merged_last_try})
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-poll-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)  # atomic: readers never see a torn file
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError:
        pass


_last_try_wall: dict[tuple[str, str, str, str], float] = {}
_load_lockouts()

# The async refresh worker holds the claim its spawner took; the fetcher gate
# must not refuse (or double-claim) for THAT call. A contextvar marks it.
import contextvars

_gate_exempt: contextvars.ContextVar[bool] = contextvars.ContextVar("usage_gate_exempt", default=False)


def gate_exempt() -> bool:
    return _gate_exempt.get()


def _cache_serve_cls():
    """The AccountUsageSnapshot subclass marking a gate stand-down RE-SERVE of
    an already-cached object (not a fresh fetch). ``remember_account_usage``
    skips it — re-membering with a newer start would trip the late-fetch guard
    and drop the inflight worker's fresh result. Defined lazily: the base class
    lives in agent.account_usage, which imports this module only lazily."""
    global _CacheServeSnapshot
    try:
        return _CacheServeSnapshot
    except NameError:
        from agent.account_usage import AccountUsageSnapshot

        @dataclasses.dataclass(frozen=True)
        class _Impl(AccountUsageSnapshot):
            __slots__ = ()

        _CacheServeSnapshot = _Impl
        return _CacheServeSnapshot


def as_cache_serve(snapshot):
    """Re-box *snapshot* as a cache-serve marker (same fields, same data)."""
    cls = _cache_serve_cls()
    if isinstance(snapshot, cls):
        return snapshot
    return cls(**{f.name: getattr(snapshot, f.name) for f in dataclasses.fields(snapshot)})
# slot key → monotonic time the stored snapshot's fetch STARTED (late-fetch guard)
_started: dict[tuple[str, str, str, str], float] = {}
# slot key → monotonic time of the last refresh try (success or failure)
_last_try: dict[tuple[str, str, str, str], float] = {}
_inflight: set[tuple[str, str, str, str]] = set()


def _key(provider: str, identity_id: Optional[str] = None, base_url: Optional[str] = None) -> tuple[str, str, str, str]:
    from hermes_constants import hermes_home_key

    return (
        hermes_home_key(),
        str(provider or "").strip().lower(),
        str(identity_id or "").strip(),
        str(base_url or "").strip().rstrip("/"),
    )


def has_account_usage(provider: str) -> bool:
    """Whether *provider* can report usage windows at all (a built-in fetcher or a plugin hook)."""
    from agent.account_usage import _USAGE_FETCHERS
    from providers import get_provider_profile
    from providers.base import ProviderProfile

    slug = str(provider or "").strip().lower()
    if slug in _USAGE_FETCHERS:
        return True
    profile = get_provider_profile(slug) if slug else None
    return profile is not None and type(profile).fetch_account_usage is not ProviderProfile.fetch_account_usage


def _identity_id_for(provider: str, entry: Any) -> str:
    """Stable non-secret account id of one pool entry, for slot keys and wire ``accounts[].id``.

    A trusted decoded identity (Codex JWT principal) wins: several credentials of one account
    share a slot, so the account is counted once. Without one, the entry's token fingerprint
    stands in — same token ⇒ same account, and a replaced token never reuses the old quota.
    A metadata-only row (no token) falls back to the entry id: nothing better is knowable.
    """
    token = str(getattr(entry, "runtime_api_key", "") or "").strip()
    if not token:
        return f"entry:{getattr(entry, 'id', '')}"
    if provider == "openai-codex":
        from agent.credential_pool import _codex_principal_identity

        principal = _codex_principal_identity(token)
        if principal:
            return f"codex:{principal[0]}:{principal[1]}"
    import hashlib

    return f"fp:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


def remember_account_usage(
    provider: Optional[str], snapshot: Optional[AccountUsageSnapshot],
    *, identity_id: Optional[str] = None, base_url: Optional[str] = None,
    started_monotonic: Optional[float] = None,
) -> None:
    """Land one fetch result.

    ``identity_id`` given (per-credential fetch): the snapshot is stored under that account's
    slot only — one pool entry's usage must never pose as the provider-wide gauge. The snapshot's
    own trusted ``identity`` (decoded from the very token it was fetched with) wins over the
    caller's id, so a replaced credential cannot repopulate its old account's slot.

    ``identity_id`` absent (per-turn / ``/usage`` fetch): stored in the provider's legacy slot —
    the single-account gauge — and additionally in the account's identity slot when the fetcher
    identified it, so a pooled picker sees per-turn data without waiting on its own refresh.

    A snapshot without windows is never stored: a failed or empty fetch must not erase a fresher
    snapshot. A fetch that STARTED before the freshest stored one cannot replace it either
    (``started_monotonic``), however late its response arrives.
    """
    if not provider or snapshot is None or not snapshot.windows:
        return
    if isinstance(snapshot, _cache_serve_cls()):
        return  # a gate stand-down re-served an already-cached snapshot: not a fetch
    started = time.monotonic() if started_monotonic is None else started_monotonic
    with _lock:
        targets = []
        snapshot_identity = getattr(snapshot, "identity", None)
        if identity_id is None:
            targets.append(_key(provider, None, base_url))
            if snapshot_identity:
                targets.append(_key(provider, snapshot_identity, None))
        else:
            targets.append(_key(provider, snapshot_identity or identity_id, None))
        for key in targets:
            if key in _started and _started[key] > started:
                continue  # a fresher fetch already landed; this late result must not replace it
            if _snapshots.get(key) is snapshot and key in _started:
                # Stand-down re-serve: the gate returned the ALREADY-CACHED
                # object. Re-membering it must not advance the late-fetch
                # guard, or the inflight worker's fresh result gets dropped.
                continue
            _snapshots[key] = snapshot
            _started[key] = started


def cached_account_usage(
    provider: str, *, identity_id: Optional[str] = None, base_url: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    with _lock:
        return _snapshots.get(_key(provider, identity_id, base_url))


def snapshot_is_stale(snapshot: Optional[AccountUsageSnapshot]) -> bool:
    """True when the snapshot is too old to render as a live gauge (the picker shows state
    ``unknown`` instead of trusting its percentages)."""
    from agent.account_usage import AccountUsageSnapshot as _Snapshot

    if not isinstance(snapshot, _Snapshot):
        return True
    try:
        age = time.time() - snapshot.fetched_at.timestamp()
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        return True
    threshold = SNAPSHOT_STALE_AFTER_S
    if snapshot is not None:
        threshold = STALE_AFTER_S_BY_PROVIDER.get(
            str(getattr(snapshot, "provider", "") or "").strip().lower(), SNAPSHOT_STALE_AFTER_S
        )
    return age < 0 or age > threshold


def note_usage_lockout(provider: str, seconds: float, *, identity_id: Optional[str] = None,
                       base_url: Optional[str] = None) -> None:
    """Record a server-imposed back-off for *provider*'s slot (e.g. a 429 Retry-After).

    While the deadline is in the future, ``_claim_refresh`` refuses every refresh attempt for
    that slot — retrying sooner would only restart the server's window and perpetuate the
    lockout. All caller surfaces funnel through the same slot key, so one note silences every
    sampler at once."""
    import math

    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = 3600.0
    with _lock:
        _lockout_until[_key(provider, identity_id, base_url)] = time.time() + min(seconds, 7200.0)
    # Persist OUTSIDE the process lock: the persist may wait on the
    # cross-process file lock, and holding _lock through that stalls every
    # in-process reader (picker, per-turn usage) for the wait.
    _persist_lockouts()


def _claim_refresh(key: tuple[str, str, str, str], now: float) -> bool:
    """Take the refresh right for *key* unless one is inflight, tried too recently, or the
    server imposed a back-off that has not elapsed yet.

    A failed or empty fetch still counts as a try, so a dead credential (or a provider with no
    usage API) isn't re-asked on every picker open."""
    ok, reason = _claim_refresh_reason(key, now)
    return ok and reason == "claimed"


def _claim_lock_file():
    """The cross-process claim lock sits next to the persisted lockout state."""
    path = _lockout_file()
    return path.with_name(path.name + ".claim.lock") if path is not None else None


def _claim_refresh_reason(key: tuple[str, str, str, str], now: float) -> tuple[bool, str]:
    """As ``_claim_refresh`` but names why a claim was refused:
    'inflight' | 'lockout' | 'throttled' | 'claimed'. The direct-fetcher gate
    uses the reason: an inflight async worker must still fetch (it holds the
    claim), while a throttled/locked direct caller must stand down.

    CROSS-PROCESS: the read/check/persist sequence runs under an OS-level
    file lock (fcntl on POSIX, msvcrt on Windows) — the in-process threading
    lock alone cannot see another process's claim, so two processes could
    both decide "not throttled" and both poll (restarting the provider's
    sliding 429 window, the exact storm this module exists to prevent)."""
    lock_path = _claim_lock_file()
    if lock_path is None:
        return _claim_refresh_reason_locked(key, now)
    from hermes_cli.active_sessions import _FileLock
    try:
        with _FileLock(lock_path, timeout=5.0):
            _claim_lock_held.value = True
            try:
                return _claim_refresh_reason_locked(key, now)
            finally:
                _claim_lock_held.value = False
    except (RuntimeError, OSError) as exc:
        # Contention vs unusable: _FileLock wraps BOTH shapes in
        # "file lock unavailable: <path>"; the timeout's "still held" lives
        # in the cause chain, not str(exc).
        cause, contended = exc, False
        while cause is not None:
            if "still held" in str(cause):
                contended = True
                break
            cause = cause.__cause__
        if not contended:
            # The lock FILE itself is unusable (unwritable cache dir), not
            # contended: permanent refusal would silently end all usage
            # sampling. Warn once and take the pre-lock best-effort path.
            if not _claim_lock_warned.warned:
                _claim_lock_warned.warned = True
                _logger.warning("usage claim lock unusable (%s); polling without cross-process protection", exc)
            return _claim_refresh_reason_locked(key, now)
        # Another process holds the claim section: stand down rather than
        # risk a double poll.
        return False, "inflight"


def _claim_refresh_reason_locked(key: tuple[str, str, str, str], now: float) -> tuple[bool, str]:
    """The claim check itself; callers hold the cross-process file lock."""
    with _lock:
        if key in _inflight:
            return False, "inflight"
        # Lockout is WALL CLOCK (persisted, cross-process); caller `now` is
        # monotonic — never compare the two clocks. Re-read the mirror when we
        # hold no live entry, so a long-running process sees lockouts that other
        # processes recorded after its import.
        if time.time() < _lockout_until.get(key, 0.0):
            return False, "lockout"
        # No LIVE in-memory entry (absent or expired): re-read the mirror so
        # this process sees lockouts other processes recorded after its import.
        if time.time() >= _lockout_until.get(key, 0.0):
            _load_lockouts()
            if time.time() < _lockout_until.get(key, 0.0):
                return False, "lockout"
        min_interval = PROVIDER_MIN_INTERVAL_S.get(key[1], REFRESH_AFTER_S)
        wall_last = _last_try_wall.get(key)
        recent = (now - _last_try.get(key, float("-inf")) < min_interval) or (
            wall_last is not None and time.time() - wall_last < min_interval
        )
        if recent:
            return False, "throttled"
        _inflight.add(key)
        _last_try[key] = now
        _last_try_wall[key] = time.time()
        _persist_lockouts()
        return True, "claimed"


def _release_refresh(key: tuple[str, str, str, str]) -> None:
    with _lock:
        _inflight.discard(key)


def refresh_account_usage_async(providers: Iterable[str]) -> list[threading.Thread]:
    """Background-refresh the provider-wide (legacy) slot for each provider not tried within
    ``REFRESH_AFTER_S``. Returns the started threads (daemon; callers never join them)."""
    started: list[threading.Thread] = []
    now = time.monotonic()
    for provider in dict.fromkeys(providers):
        key = _key(provider)
        if not _claim_refresh(key, now):
            continue
        thread = threading.Thread(
            target=copy_context().run, args=(_refresh_legacy, provider, key),
            name="hermes-account-usage-refresh", daemon=True)
        thread.start()
        started.append(thread)
    return started


def _refresh_legacy(provider: str, key: tuple[str, str, str, str]) -> None:
    from agent.account_usage import fetch_account_usage

    token = _gate_exempt.set(True)
    try:
        fetch_account_usage(provider)  # remembers its own result
    finally:
        _gate_exempt.reset(token)
        _release_refresh(key)


def refresh_account_usage_entries_async(requests: Iterable[dict]) -> list[threading.Thread]:
    """Background-refresh per-credential usage slots. *requests* entries are
    ``{"provider", "identity_id", "base_url", "api_key"}`` — the live credentials to read.

    One worker thread fetches all due entries serially (no thread storm for a 10-credential
    pool), read-only: a 401 or any other failure is reported, never repaired by rotating or
    refreshing the credential. Throttled per account slot, failures included."""
    due: list[dict] = []
    now = time.monotonic()
    for request in requests:
        provider = str(request.get("provider") or "").strip().lower()
        identity_id = str(request.get("identity_id") or "").strip()
        if not provider or not identity_id or not str(request.get("api_key") or "").strip():
            continue
        if not _claim_refresh(_key(provider, identity_id), now):
            continue
        due.append(request)
    if not due:
        return []
    thread = threading.Thread(
        target=copy_context().run, args=(_refresh_entries, list(due)),
        name="hermes-account-usage-refresh", daemon=True)
    thread.start()
    return [thread]


def _refresh_entries(requests: list[dict]) -> None:
    from agent.account_usage import fetch_account_usage

    for request in requests:
        provider = request["provider"]
        key = _key(provider, request["identity_id"])
        try:
            fetch_account_usage(
                provider, base_url=request.get("base_url"), api_key=request.get("api_key"),
                read_only=True, identity_id=request["identity_id"],
            )
        finally:
            _release_refresh(key)

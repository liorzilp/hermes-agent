# Away-Mode Telegram Bridge — Design Doc

**Status:** Rev 7 (converged) — requirements owner-confirmed 2026-09-28;
design passed independent review round 5 (PASS, no Critical/High/Medium
findings); Low findings folded. **V1 BUILT 2026-09-29** (commit `c020efb9f2`):
broker + CLI + Telegram interceptor + sweep triggers + away-mode skill +
50 tests green. Deployment into the runtime install and `enabled=true`
config flip remain (owner step).
**Owner:** Lior (Telegram account/chat `1682802389`)
**Capability:** A per-session, explicitly armed bridge that relays a session's
input requests to the owner's Telegram and accepts exactly the first answer
successfully claimed from either channel.

## Revision history

- **Rev 1** — initial requirements and dedicated-bridge-session architecture.
  Commit `e9c6a7146`; frozen snapshot `away-bridge-revs/REV-1.md`; SHA-256
  `2224e8145f8afb55001c2088904baf13dcdb2518f240fb97736b2716885d272f`.
- **Rev 2** — removed the orphan bridge session, added heartbeat polling,
  watermarks, chat claims, and documented Telegram dual consumption. Commit
  `edf4a2e55`; frozen snapshot `away-bridge-revs/REV-2.md`; SHA-256
  `2714969129980930a2616a5eeb2331df94d482bedc2a1156fd192462e6d27611`.
- **Rev 3** — folds independent review round 1 (deleg_2c43b27d, verdict
  REVISE). Heartbeats and shared-chat watermarks are removed after source
  verification showed they cannot be armed or consumed by an agent. The
  replacement is a gateway pre-dispatch bridge interceptor plus one managed
  background waiter owned by each originating session. Commit `0dda6126c`;
  frozen snapshot `away-bridge-revs/REV-3.md`; SHA-256
  `d37bd6df7e5dd534d533cf5d02c49be58d9958d9d6692daa6d93762fa19fe75f`.
- **Rev 4** — folds independent review round 2 (deleg_89a935fd, verdict
  REVISE): restart backlog loss + startup sweep; interceptor seam pinned to
  `base.handle_message`; waiter stdout contract; poll-based DISARMED
  detection; broker keyed on gateway `session_key`; chat-win completion
  suppression; PID-aware reconcile + reap recovery; owner-shaped NACK; V1
  environment constraints. Commit `2b5275f52`; frozen snapshot
  `away-bridge-revs/REV-4.md`; SHA-256
  `e0fe41872c1360efb580ac71d37575d2ae05dd869f3a76ae9e5057cd6446bbf6`.
- **Rev 5** — folds independent review round 3 (deleg_40a632f7, verdict
  REVISE). The round-3 reviewer verified the Rev 4 mechanisms (seam,
  backlog semantics, wake-path ownership, claim transaction) and found the
  identity, consumption, matcher, and recovery specs unsound as written.
  Rev 5 re-keys the broker on arm-time identity with lineage-relative
  lookup, replaces the chat-win suppression primitive with kill + log
  draining, makes the interceptor matcher line-anchored, splits reconcile
  (report) from in-turn waiter restart (skill), and specifies sweep
  triggers, poll-set completeness, PID identity validation, and event-loop
  safety. Commit `e2636b01a`; frozen snapshot `away-bridge-revs/REV-5.md`;
  SHA-256 `b4cd3667f7e52e9fdd23ca149b71a94969f9ab0e9bd6e7ffe8fa56e33ee829ef`.
- **Rev 6** — folds independent review round 4 (deleg_1bad8f19, verdict
  REVISE; first round with no Critical/High findings): chat disarm flips
  every lineage-matching armed row; `prepare`'s comparison pinned to
  two-sided tip resolution; `arm` dedupes lineage-matching rows; interceptor
  broker DB errors suppress + notice; multi-`A#`-token batches partitioned;
  `waiter_start_time` provenance pinned to the kernel start-time helper;
  reconcile timing restated as "next turn after resume"; the
  literal-"I'm back"-answer disarm side effect documented; orphan kills
  split by waiter class. Commit `d07efb236`; frozen snapshot
  `away-bridge-revs/REV-6.md`; SHA-256
  `b4a36dffdfc1e5de5a17b2393362ba1bfd833f03c5f0e7247b3cd48ba302bd73`.
- **Rev 7** — folds independent review round 5 (deleg_bc3e0351, verdict
  **PASS** — zero Critical/High/Medium; all citations re-verified). The five
  Low findings folded: the cold-start sweep also GCs orphaned armed rows
  (waiters self-terminate via their DISARMED poll branch); the
  single-awaiting-request-per-session invariant is stated; answer-text
  extraction boundary pinned (token line stripped, same-line remainder
  included); return-intent lines under broker DB error are suppressed with
  a transient-error notice; the registry-kill target-resolution source is
  named. This is the converged revision submitted for owner approval.

## Review round 1 findings folded (all verified against source)

| # | Finding (severity) | Disposition |
|---|---|---|
| 1 | Heartbeat arming is user-only: `_start_heartbeat_watchdog` starts only from the `/heartbeat` slash handler (cli_commands_mixin.py:2497,2536); the cached `HeartbeatManager` never reloads state written by another process; gateway watch registry is in-memory and re-arms only on user `/heartbeat` (run.py:19436-19443). The doc's wake path was unimplementable. (Critical) | Wake path redesigned around managed background processes + process-notification delivery, which is agent-addressable and verified live (tui_gateway/server.py:9233+ `_notification_poller_loop`). |
| 2 | Shared-chat per-session watermarks can wedge permanently on foreign/unknown keys and the 200-cap window can starve valid answers. (High) | Watermark scanning removed entirely. Answers are routed by key at the gateway interceptor; no session reads the shared Telegram history. |
| 3 | Rev 2 §4.3 skip-rule contradicted its own parenthetical for acked/unknown keys ("do not advance" vs "provably not addressed"). (High) | Contradictory section deleted with the mechanism it specified. |
| 4 | "Do not ack" bullet contradicted §5's "second gets the small ack" and the ack-idempotency bullet. (Medium) | Ack semantics now specified once, in §4.2/§4.4, as an idempotent DB insert. |
| 5 | Dual consumption on Telegram violated R6 ("never consume") rather than being a cosmetic wart; an armed normal DM session or the DM agent could act on answers. (High) | The pre-dispatch interceptor suppresses bridge traffic from normal dispatch entirely (§4.4). |
| 6 | Keyless owner answers and ambiguous disarm phrases caused silent stalls or mis-disarm. (Medium) | Answer/disarm protocol re-specified with exact match sets and no implicit combine (§4.5, §4.6). |
| 7 | Gateway-down failure-table wording was wrong ("answers queue in state.db"). (Low) | Corrected in §4.7 (and re-corrected in Rev 4 — see R2-1). |

## Review round 2 findings folded (all verified against source)

| # | Finding (severity) | Disposition in Rev 4 (carried into Rev 7) |
|---|---|---|
| R2-1 | §4.7 "Gateway restart" row was wrong: a cold process restart drops Telegram's pending queue (`drop_pending_updates=not is_reconnect`, adapter.py:4171-4178; tests/gateway/test_telegram_conflict.py:573). Answers sent during downtime would be silently discarded. (High) | §4.7 distinguishes reconnect (backlog preserved) from restart (backlog dropped); interceptor startup sweep re-sends `awaiting` questions. Loss mode documented and accepted in V1. |
| R2-2 | The interceptor seam was underspecified; the literal "after authorization" reading (run.py `_handle_message`, 14805+) runs after batching and session-guard queueing (base.py:6119-6124 merge_text), causing delayed/garbled claims. (High) | Seam named exactly: top of `BasePlatformAdapter.handle_message` (base.py:5931), platform == Telegram — after adapter auth and batching, before session guard/persistence/dispatch. |
| R2-3 | The answer rides the waiter's stdout, capped at the last 2,000 chars (process_registry.py:1563-1579) — long Telegram answers silently truncated. (High) | Waiter prints only a one-line envelope; the skill fetches the full answer from the broker (`away-bridge fetch`), idempotent under `delivered`. |
| R2-4 | The waiter could never detect DISARMED: "signals all waiters" names a push primitive that does not exist; disarm mutates only `armed_sessions`. (High) | Waiter poll set joins `armed_sessions.status`; disarm is poll-observed; chat-side disarm may also kill the waiter directly. |
| R2-5 | Session identity was conflated (agent `session_id` vs UI tab id vs gateway `session_key`). (Medium) | Broker keyed on the gateway `session_key` (Rev 5 re-specifies which generation — see R3-C1). |
| R2-6 | Every waiter exit costs a full model turn; "ignorable cleanup" hid a real cost. (Medium) | Chat-win consumption specified (Rev 5 replaces the primitive — see R3-H1). |
| R2-7 | Session-reap paths drop addressed completions when the owner session is gone (server.py:813, 9315-9330); §4.7 covered only backend exit. Crash-exit backends leave orphan waiters. (Medium) | Reconcile runs at the start of the first turn of any armed session after resume; waiter restart PID-aware (Rev 5 adds start-time identity — see R3-M3). |
| R2-8 | Owner-sent, bridge-shaped but unresolvable keys fell through to the ordinary DM agent, which could execute an answer as a fresh instruction. (Medium) | Owner-origin `A#`-shaped failures get a NACK and are suppressed from dispatch. |
| R2-9 | Waiter-start environment assumptions unstated: approval prompts away-time; sandboxed sessions cannot reach the broker; `hermes send` resolves the default profile (send_cmd.py:331-334). (Medium) | V1 constraints stated at arm time: local backend, allowlisted `away-bridge` commands, profile-scoped `hermes send`, sandboxed/non-local sessions refused. |
| R2-10 | Residual wording defects (leftover "heartbeat-free", 4-char example keys, "UI session identity"). (Low) | All fixed. |

## Review round 3 findings folded (all verified against source)

| # | Finding (severity) | Disposition in Rev 5 (carried into Rev 7) |
|---|---|---|
| R3-C1 | The gateway `session_key` is NOT compression-stable: on compression the TUI re-anchors `session["session_key"]` to the agent's new `session_id` (server.py:4943-4975); `resolve_resume_session_id` maps stale→tip forward-only (hermes_state.py:8566+). Keying the broker on the *current* key breaks the waiter JOIN (no-row), the chat claim predicate (rejects a genuinely pending answer), and reconcile (misses the rows it exists to recover) after any mid-armed-window compression — routine for away mode. (Critical) | §4.1 re-specified: requests **inherit the arm-time key** of their `armed_sessions` row; `prepare` resolves the session's armed row **lineage-relatively**. The JOIN always matches by construction. Reconcile iterates rows and forward-resolves each row's key. The chat claim's `AND session_key=:current` predicate is dropped (the 128-bit request key is the capability; see §4.2). `PRAGMA foreign_keys=ON` pinned; poll-set no-row outcome defined (exit ERROR). (Rev 6 pins the comparison as two-sided — see R4-2.) |
| R3-H1 | `process(action="poll")` never marks `_completion_consumed` — it deliberately does not (#10156, process_registry.py:1834-1844); the cited line 1889 belongs to `read_log()`. (High) | §4.5: primary consumption is `process(action="kill")` (default `consume_output=True` marks `_completion_consumed` **before** `_move_to_finished` enqueues, process_registry.py:2118-2126 — race-free for a running waiter); if the waiter already exited, `process(action="log")` marks the enqueued completion consumed (1888) before the poller drains (server.py:9333 skips consumed). Residual race documented (at most one rare extra turn). |
| R3-H2 | §4.4's start-anchored matcher contradicted its own mixed-batch rule; a batched `hm\nA#… do X` fell through to the DM agent, which executes the answer — the exact hazard R2-8 closed. (High) | §4.4 matcher re-specified as **line-anchored** (token begins some line; content before discarded; return-intent line anywhere triggers disarm; claim-then-disarm regardless of order; mid-line occurrences don't match; owner-origin batches with a matching `A#` line are always `handled=true`). (Rev 6 adds multi-token partitioning — see R4-4.) |
| R3-H3 | `reconcile` is a CLI subprocess; children it spawns are invisible to the gateway's process registry — a waiter it "restarts" enqueues no completion and wakes nobody. (High) | §4.7 split: `reconcile` only **reports**; the **skill**, inside its turn, restarts the waiter via `terminal(background=true, notify_on_complete=true)` and records the new PID + start time. |
| R3-M1 | Startup sweep trigger ill-defined (watcher reconnects also call connect(); 409-conflict recovery also drops the backlog without a restart); no per-request re-send cap; read-then-send race. (Medium) | §4.4 sweep fires exactly when polling is established with `drop_pending_updates=True` (cold start **and** conflict recovery; never watcher reconnect); per-request `last_swept_at` rate limit (default once per hour); re-check `state='awaiting'` immediately before each send. |
| R3-M2 | The poll set had no branch for the state the FIRST poll always sees (`prepared`, `prepared`+`disarmed`), and no DB-error behavior. (Medium) | §4.3 poll set completed: `prepared`+armed → keep waiting; `prepared`+`disarmed` → exit DISARMED; no row → exit ERROR; DB error → exit ERROR envelope, never spin silently. |
| R3-M3 | Bare `waiter_pid` liveness is unsafe — the kernel recycles PIDs; the registry itself refuses to adopt PIDs without start-time validation (process_registry.py:2553-2560). (Medium) | Schema adds `waiter_start_time`; liveness and kill decisions validate PID + start time; kills additionally require an argv identity match. (Rev 6 pins the provenance — see R4-5.) |
| R3-M4 | The interceptor's synchronous SQLite runs on the gateway's shared event loop; a desktop-side claim holding the write lock stalls all platforms' message processing. (Medium) | §4.4: all broker I/O in the interceptor and sweep goes through `asyncio.to_thread` with a short dedicated busy timeout. (Rev 6 specifies the failure outcome — see R4-3.) |
| R3-L1 | Ack examples hardcoded the winning channel. (Low) | Acks parameterized: "Already answered via {chat|Telegram} — ignored." |
| R3-L2 | "Telegram 'I'm back' disarms all armed sessions" is per-profile; two armed profiles need two returns. (Low) | §4.6 scopes the global disarm to the profile's broker; documented acceptance. |
| R3-L3 | Build-plan looseness: poll interval unspecified; `PRAGMA foreign_keys` unspecified; no GC for never-resumed sessions; `prepared`-stuck requests appear in no reconcile outcome. (Low) | §4.3 poll interval default 2 s; §4.1 `foreign_keys=ON` pinned; §4.7 reconcile/GC covers prepared-stuck, orphaned armed rows, terminal-row waiters, delivered-row pruning. |

## Review round 4 findings folded (all verified against source)

| # | Finding (severity) | Disposition in Rev 6 (carried into Rev 7) |
|---|---|---|
| R4-1 | Chat disarm's row selection was unspecified in the multi-armed-row state the doc itself anticipates (arm → compress → arm again). Disarming only the newest row lets the older row silently re-arm the session after further compression (violates R3) or leaves a waiter polling forever on the older row's still-`armed` status. (Medium) | §4.6: chat disarm flips **every** armed row whose forward-resolved tip equals the session's current tip. §4.1: `arm` dedupes — if an existing row's resolved tip already equals the current session's, `arm` refreshes that row instead of inserting a second one. |
| R4-2 | `prepare`'s comparison was one-sided ("resolution equals the current key"); after compression the skill only holds the arm-time key K0, so resolution(K0)=K1 ≠ K0 fails closed on exactly gate 11's scenario, and the documented recovery ("re-arm") manufactures the duplicate-row state from R4-1. (Medium) | §4.1/§4.3: comparison pinned as **two-sided tip resolution** — `resolve(row.key) == resolve(current_key)` — where the CLI derives the current key from the bridged `HERMES_SESSION_KEY` (verified bridged per spawn from the turn contextvar, tools/environments/local.py `_inject_session_context_env`; terminal_tool.py:2764-2766) or forward-resolves the `--session` argument. |
| R4-3 | Interceptor behavior on broker DB failure was unspecified; the naive `handled=false` default on `SQLITE_BUSY`/corrupt-file would dispatch the owner's answer to the ordinary DM agent — the R2-8/R6 hazard. (Medium) | §4.4: broker DB errors on an owner-origin bridge-shaped batch are **suppressed** (`handled=true`) with a transient-error notice asking the owner to resend; never fall through to dispatch. Added to §6 and gate 9. (Rev 7 extends this to return-intent lines — see R5-4.) |
| R4-4 | Two distinct `A#` tokens in one batch: the single-token scan polluted the first answer with the second token's text and silently stalled the second request (no claim, no NACK) until the 1-hour sweep. (Medium) | §4.4: token lines **partition** the batch — after the first claim, the matcher continues scanning for further `A#` lines; each segment claims its own token (resolvable) or NACKs (unresolvable). Added to §6 and gate 14. |
| R4-5 | `waiter_start_time` provenance unspecified with a wrong-quantity trap: spawn results expose `pid` only (terminal_tool.py:3007-3013); deriving from `uptime_seconds`/`started_at` records wall-clock, not kernel start time — identity validation would always report "dead". (Low) | §4.3 step 4: `publish` records the kernel start time of the waiter PID via the same helper the registry uses for `_host_pid_is_ours` (process_registry.py:738-767) — never from wall-clock records. |
| R4-6 | "Reconciled when that session is resumed" overstated: `session.resume` rebuilds the session but injects no turn (only the mid-turn-crash marker auto-continues, server.py:9762-9773); a resume-without-message delivers nothing. (Low) | §3/§4.7 reworded: reconcile fires on the session's **next turn** after resume. |
| R4-7 | A return-intent line inside the answer text (owner literally answers "I'm back" to a question) deterministically triggers the global disarm while the owner is still away — consistent with the exact-match philosophy but undocumented. (Low) | §4.6 documents the side effect as an accepted consequence of exact matching. |
| R4-8 | Orphan-kill executor conflated: registry `process(action="kill")` works only for registry-tracked waiters; true orphans (spawned by a dead backend) need a CLI direct kill. (Low) | §4.7: kill classes split — registry kill for registry-tracked waiters (the session's own, incl. checkpoint-recovered); CLI direct kill validated by PID + start time + argv for true orphans. |

## Review round 5 findings folded (verdict: PASS — all Low)

| # | Finding (severity) | Disposition in Rev 7 |
|---|---|---|
| R5-1 | "Nothing polls forever" had no gateway-side GC trigger: if no session in the profile ever turns again and the host never reboots, the waiter polls every 2 s indefinitely (bounded cost, no wrong state). (Low) | §4.4/§4.7: the cold-start sweep also runs the orphan-armed-row GC (marking rows whose lineage tip is neither live nor resumable and that have no non-terminal requests `disarmed`); the still-running waiter then observes DISARMED on its next poll and exits by itself — no kill needed. The claim is also scoped honestly: a waiter can poll indefinitely only if no session ever turns, the gateway never cold-starts, and the host never reboots — accepted bounded cost. |
| R5-2 | The chat answer path assumed a single pending request per session; the invariant was never stated, leaving the implementer to guess which request a keyless chat reply answers. (Low) | §4.5 invariant (normative): **at most one `awaiting` request per session at any time.** The skill batches multiple questions into one request or defers the second ask until the first resolves. |
| R5-3 | Answer-text extraction boundary unspecified — whether the token line itself and the same-line remainder after the token are answer text. (Low) | §4.4: the token line is stripped; the answer is the segment's remaining lines, including the same-line remainder after the token. |
| R5-4 | Return-intent handling under broker DB error unspecified — "I'm back" with a busy/corrupt broker would fall to the DM agent as ordinary chat and the disarm is deferred. (Low) | §4.4: the DB-error suppression norm covers return-intent lines too — an owner-origin batch containing a return-intent line while the broker is unavailable is suppressed with a transient-error notice (consistent with the A# rule: owner-origin protocol-shaped traffic never falls through). |
| R5-5 | Registry-kill target resolution after resume implied, not stated (`process(action="kill")` needs the registry process id; the row records only PID + start time). (Low) | §4.7: the skill resolves the registry target via `process(action="list")` filtered by argv match on `--request <full-id>` (or the spawn result recorded in the transcript); if unresolvable, it degrades to the CLI identity-validated kill, which works and costs at most one recognized-and-ignored completion turn. |

## 1. Problem and success criterion

Desktop and Telegram are separate Hermes sessions. When the owner steps away
from the desk, a desktop session that needs input stalls without reaching the
owner's phone.

This design succeeds when an explicitly armed session can ask in both places,
accept exactly one winning answer, resume in the original session, reject the
losing duplicate with a small acknowledgement, and never route an answer to an
unrelated session.

## 2. Requirements (owner-confirmed)

| ID | Requirement | Decision |
|---|---|---|
| R1 | Scope | Input **requests only**: clarification, decision, or approval. Ordinary chat, status updates, and background reports remain on their original channel. |
| R2 | Arming | **Per-session and explicit.** The owner says "I'm stepping out / I won't be here" in that session. No standing default. |
| R3 | Lifetime | The session remains armed until the owner says "I'm back" in that session or from Telegram. No time-based expiry. Telegram "I'm back" means the owner is globally back and disarms all armed sessions in that profile's broker. |
| R4 | First answer wins | The first channel whose claim transaction succeeds becomes authoritative. A later answer from the other channel is ignored and receives one small acknowledgement identifying the winning channel. |
| R5 | Authorized responder | Only the configured owner Telegram identity/chat may answer. |
| R6 | Multi-session isolation | Every request has a unique key bound to one originating session. No other session, including the ordinary Telegram DM session, receives or acts on its answer. |
| R7 | Non-bridge Telegram traffic | Messages without a valid active bridge key are processed normally by the Telegram DM session. |

## 3. Non-goals and v1 boundary

- No whole-conversation mirroring or `/handoff` takeover.
- No group-chat support.
- No bridge for background progress/status messages.
- No delivery while the originating desktop/TUI backend is completely shut
  down. Telegram answers remain durable and are reconciled on the session's
  next turn after resume (`session.resume` itself injects no turn,
  server.py:9762-9773); Hermes cannot continue a desktop turn while its
  backend is absent.
- V1 supports persistent Desktop/TUI sessions whose terminal tool reports
  async completion delivery as supported, on a **local** terminal backend, with
  the `away-bridge` command family pre-approved. Stateless one-shot/API
  requests, cron runs, Kanban workers, and sandboxed/non-local sessions are
  rejected at arm time (see §4.3 step 2).

## 4. Architecture overview

The capability has three parts:

1. **Session protocol skill (`away-mode`)** — recognizes owner intent, creates
   bridge requests, starts the exact-session waiter, and handles chat answers.
2. **Durable bridge broker** — a small SQLite database under the active profile
   with atomic first-writer-wins claim semantics.
3. **Telegram gateway pre-dispatch interceptor** — recognizes valid bridge
   protocol messages from the configured owner, claims them before normal
   Telegram session dispatch, and suppresses them from that unrelated session.

There is no dedicated bridge session, no heartbeat, no shared Telegram
watermark, and no cron-based session injection.

### 4.1 State, identity, and profile scope

State lives at:

```
$HERMES_HOME/away-bridge/bridge.db
```

The path is resolved through Hermes profile helpers; it is never hardcoded to
`~/.hermes`. The Telegram gateway and originating session must use the same
profile. V1 fails closed if the receiver profile does not contain the request.
Every connection opens with `PRAGMA foreign_keys=ON` and WAL mode; FK
violations surface as visible errors, never silent hangs.

**Session identity (normative).** The gateway `session_key` is **rotated on
context compression** — the TUI re-anchors it to the agent's new
`session_id` (server.py:4943-4975), and `resolve_resume_session_id`
(hermes_state.py:8566+) maps stale keys to their lineage tip **forward only**.
The design therefore never keys broker rows on "the current key":

- `away-bridge arm` reads the current session key K0 (via `HERMES_SESSION_KEY`,
  bridged fresh per spawn from the turn contextvar,
  tools/environments/local.py `_inject_session_context_env`;
  terminal_tool.py:2764-2766). **Dedupe rule (normative):** if an existing
  `armed_sessions` row's forward-resolved tip already equals `resolve(K0)`,
  `arm` refreshes that row (re-activates it, updates `armed_at`) instead of
  inserting a second row. Otherwise it inserts `armed_sessions(K0)` and
  returns K0 to the skill, which records it.
- `away-bridge prepare --session <key>` finds this session's armed row
  **lineage-relatively, by two-sided tip comparison (normative)**: iterate the
  (few) `status='armed'` rows; for each, compute `resolve(row.key)`; derive
  the caller's current key (from the bridged `HERMES_SESSION_KEY`, or by
  forward-resolving the `--session` argument); select the newest row
  (max `armed_at`) where `resolve(row.key) == resolve(current_key)`. The
  comparison is **two-sided** because both sides may be stale generations of
  the same lineage after compression — a one-sided equality against the raw
  argument fails exactly in that case (R4-2). The new request **inherits that
  row's arm-time key**. If none resolves, prepare fails closed: the session
  is not armed (re-arm — which, per the dedupe rule, refreshes rather than
  duplicates).
- Because `requests.session_key` is the armed row's key by construction —
  under the two-sided reading above — the waiter's JOIN (§4.3) always
  matches, FKs always hold, and reconcile can always find the row by
  forward-resolving it.
- Wake routing needs no key stability: the waiter's completion stamp carries
  the then-current key, which the TUI poller forward-resolves through
  compression lineage (server.py:9012-9019).

Schema (logical form):

```sql
CREATE TABLE armed_sessions (
    session_key      TEXT PRIMARY KEY,
    armed_at         REAL NOT NULL,
    surface          TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('armed','disarmed')),
    disarmed_at      REAL
);

CREATE TABLE requests (
    req_key          TEXT PRIMARY KEY,
    session_key      TEXT NOT NULL,
    question         TEXT NOT NULL,
    state            TEXT NOT NULL CHECK(state IN
                        ('prepared','awaiting','claimed','cancelled')),
    winning_channel  TEXT CHECK(winning_channel IN ('chat','telegram')),
    answer           TEXT,
    created_at       REAL NOT NULL,
    published_at     REAL,
    claimed_at       REAL,
    cancelled_at     REAL,
    delivered        INTEGER NOT NULL DEFAULT 0,
    waiter_pid       INTEGER,
    waiter_start_time REAL,
    last_swept_at    REAL,
    FOREIGN KEY(session_key) REFERENCES armed_sessions(session_key)
);

CREATE TABLE duplicate_acks (
    req_key          TEXT NOT NULL,
    late_channel     TEXT NOT NULL CHECK(late_channel IN ('chat','telegram')),
    sent_at          REAL NOT NULL,
    PRIMARY KEY(req_key, late_channel)
);
```

SQLite runs in WAL mode with a busy timeout. All state transitions use
explicit transactions. `BEGIN IMMEDIATE` serializes competing chat and
Telegram claims. The `delivered` flag makes origin-side processing idempotent
across resumes. `waiter_pid` + `waiter_start_time` make waiter liveness and
restart decisions identity-safe (§4.7). `last_swept_at` rate-limits the
startup sweep (§4.4).

### 4.2 First-answer-wins transaction

Both channels call the same broker operation:

```sql
UPDATE requests
SET state='claimed', winning_channel=:channel,
    answer=:answer, claimed_at=:now
WHERE req_key=:key AND state='awaiting';
```

- `rowcount == 1`: this channel won.
- `rowcount == 0`: load the row. If already `claimed`, this channel lost. If
  unknown, prepared, or cancelled, reject without treating it as an answer.
- The answer is considered "successfully processed first" at the successful
  conditional update, not at message arrival time. That gives one durable,
  testable ordering point across processes.

There is no chat-side session-key predicate: channel authorization is enforced
elsewhere — the Telegram path by owner identity (§4.8), the chat path by
knowledge of the 128-bit request key, which only the asking session (from
`prepare`) and the owner (from the question text) hold. A chat claim on a
foreign request requires the owner to deliberately paste that key into
another session; even then first-answer-wins holds and the asking session's
waiter resolves normally.

Duplicate acknowledgement is separately idempotent:

```sql
INSERT OR IGNORE INTO duplicate_acks(req_key, late_channel, sent_at)
VALUES (:key, :channel, :now);
```

Only the process whose insert succeeds sends the small acknowledgement. The
ack identifies the winning channel: "Already answered via {chat|Telegram} —
ignored." (final wording is a §10 choice).

### 4.3 Request publication and waiter startup

When an armed session needs input, the skill performs this ordered protocol:

1. `away-bridge prepare --session <key>` inserts a `prepared` request bound
   to the arm-time key (§4.1 lineage-relative, two-sided lookup) and returns
   a random 128-bit request key: a full internal identifier plus the short
   display token used in the question text (`A#` + 16-char base32, ≥80 bits,
   e.g. `A#K7QM4XT9P2WBN5F`).
2. The originating session starts:

   ```text
   away-bridge wait --request <full-id>
   ```

   through the managed terminal tool with `background=true` and
   `notify_on_complete=true`.

   **V1 environment constraints (checked at arm time; violations refuse to
   arm):**
   - Local terminal backend only. Sandboxed or non-local spawns cannot reach
     `$HERMES_HOME/away-bridge/bridge.db` or the bot credentials, and their
     waiters are unrecoverable after checkpoint recovery skips them
     (process_registry.py:2541-2551).
   - The `away-bridge` command family is allowlisted/pre-approved in the
     session's approval policy. An approval prompt fired precisely when the
     owner is away would defeat the bridge.
3. Hermes's terminal runtime stamps the spawn with the session's then-current
   `session_key` (terminal_tool.py:2764-2766, 2897); the completion poller
   forward-resolves it through compression lineage (server.py:8937-8964,
   9012-9019) and injects the one-shot completion only into the session that
   owns it; foreign sessions requeue or drop rather than adopt it
   (tui_gateway/server.py:9233+, 9300+, 9315-9330).
4. After the waiter is confirmed started, `away-bridge publish` transitions
   `prepared → awaiting`, records the waiter PID in the request row, and
   records the waiter's **kernel start time** — queried from the PID via the
   same helper the registry uses for `_host_pid_is_ours`
   (process_registry.py:738-767). Spawn results expose `pid` only
   (terminal_tool.py:3007-3013); wall-clock records (`uptime_seconds`,
   `started_at`) are **not** kernel start times and must never be stored in
   `waiter_start_time` — identity validation would then always report "dead"
   (R4-5). `publish` then sends the question via
   `hermes send -t telegram:<chat>` with the session's own profile scope
   (`--profile` / `HERMES_HOME` passthrough; `hermes send` otherwise resolves
   the default profile, send_cmd.py:331-334). A profile mismatch would mark
   `awaiting` in a broker the gateway interceptor never reads.
5. If Telegram delivery fails, `publish` changes the request to `cancelled`.
   The waiter exits `CANCELLED`; the session asks in chat only and says the
   Telegram copy failed.

**Waiter poll set and exit protocol (complete).** There is no push mechanism
anywhere in this design — the broker DB is the only channel — so each poll
interval (default 2 s, `away_bridge.waiter_poll_seconds`) the waiter performs
one indexed point query:

```sql
SELECT r.state, r.winning_channel, a.status AS armed_status
FROM requests r JOIN armed_sessions a USING(session_key)
WHERE r.req_key = ?;
```

- `prepared` + `armed` → keep waiting (the publish step has not run yet).
- `prepared` + `disarmed` → exit `DISARMED`.
- `awaiting` + `armed` → keep waiting.
- `awaiting` + `disarmed` → exit `DISARMED`; the request row stays `awaiting`
  and visibly pending in the session.
- `claimed` → exit `TELEGRAM_WON` (winning_channel telegram) or `CHAT_WON`
  (winning_channel chat).
- `cancelled` → exit `CANCELLED`.
- **no row** (deleted/corrupt) → exit `ERROR`. Any DB error — busy beyond the
  timeout, missing or corrupt file — also exits `ERROR`. The waiter never
  spins silently; the skill reports and may restart it once.

A chat-side disarm may additionally kill the waiter directly, since the
session owns the process (kill and poll-detect are redundant, not exclusive).

**Waiter stdout contract.** The waiter prints exactly one line — banner-free,
ANSI-free, no answer text:

```text
AWAY-BRIDGE outcome=<OUTCOME> req_key=<full-id>
```

The completion notification carries only the last 2,000 characters of process
output (process_registry.py:1563-1579), so the answer **never** rides the
waiter's stdout. On `TELEGRAM_WON`, the skill fetches the full answer from the
broker with `away-bridge fetch --request <full-id>`, idempotent under the
`delivered` flag.

If Telegram wins before the waiter's first poll, the durable claimed row is
already present and the waiter exits immediately. There is no lost-wake
window. Idle cost is a sleeping local process — no model turns. Waiter exits
cost model turns as follows: the chat-win path suppresses its completion turn
(§4.5); DISARMED, CANCELLED, and ERROR wakes each cost exactly one turn by
design (counted, not hidden).

### 4.4 Telegram gateway pre-dispatch interceptor

**Seam (normative).** The interceptor is a pre-dispatch hook at the top of
`BasePlatformAdapter.handle_message` (gateway/platforms/base.py:5931), gated
on `platform == "telegram"`. This position is:

- **after** adapter-level platform authorization and owner identity resolution
  (`_is_user_authorized_from_message`, adapter.py:8920),
- **after** text batching (`_enqueue_text_event`, adapter.py:9040-9074, which
  merges rapid successive texts from the same chat with `\n`),
- **before** the active-session guard and `_pending_messages` merge
  (base.py:6119-6124), persistence, session lookup, and dispatch to
  `run.py _handle_message` (run.py:14805+).

An earlier placement (inside the adapter, pre-batching) would claim
chunk-split long answers on their first chunk only; a later placement
(`run.py`) runs after session-guard queueing and violates "before queueing."
The named seam is the only position satisfying all constraints. In-handler
sends have direct precedent inside `handle_message` itself (base.py:6016,
6069).

**Event-loop safety (normative).** All broker I/O performed by the
interceptor and the sweep runs through `asyncio.to_thread` with a short
dedicated busy timeout. The gateway's shared event loop never blocks on the
broker; a desktop-side claim holding the write lock cannot stall other
platforms' message processing. **Failure outcome (normative):** if the broker
I/O errors (busy beyond the short timeout, missing or corrupt file) while
resolving or claiming an owner-origin bridge-shaped batch — an `A#` batch or
a return-intent line — the interceptor **suppresses the batch**
(`handled=true`) and sends a transient-error notice asking the owner to
resend; it never falls through to dispatch (R4-3, R5-4). An answer lost this
way is re-asked by the sweep.

**Matcher (normative, line-anchored and token-partitioned).** Batched text is
multi-line by construction (adapter.py:9061 merges with `\n`). The matcher
therefore:

- recognizes `A#<token>` when the token begins **some line** of the batched
  text; content before the first token line is discarded;
- **partitions on token lines (normative):** after the first matching `A#`
  line, the matcher continues scanning the remaining lines for further `A#`
  token lines. Each token line starts a segment. **Extraction boundary
  (normative, R5-3):** the token line itself is stripped; a segment's answer
  is the segment's remaining lines, including the same-line remainder after
  the token. Each segment is processed independently — claimed if
  resolvable, NACKed if not. Two requests answered in one message (or
  rapid-fire and merged by batching) are both handled; neither pollutes the
  other's answer nor stalls silently (R4-4);
- treats an exact normalized return-intent line (`I'm back`, `Im back`,
  `חזרתי`) **anywhere** in the batch as a disarm intent;
- processes mixed batches claim-then-disarm regardless of line order;
- does **not** match mid-line or quoted occurrences ("what does A#… mean?");
- guarantees that any owner-origin batch containing a matching `A#` line is
  always `handled=true` (claim, NACK, or transient-error suppression) — it
  never falls through to the ordinary DM agent.

**One protocol reply per message.** Telegram splits messages over 4,096
characters into multiple updates; only the first would carry the key. V1
defines the answer as the single update carrying the token. The question text
instructs: "reply in one message (up to 4,096 characters), starting with the
key." Continuation chunks without a key are ordinary traffic (R7).

The handler is enabled only when `away_bridge.enabled=true` and the configured
owner chat/user match the inbound event. It recognizes:

- An `A#<token>` line (line-anchored, partitioned as above) — resolve the
  active request, attempt the Telegram claim transaction (§4.2), send
  success/late-duplicate feedback, and return `handled=true` so the normal
  Telegram DM session never receives the message.
- Exact normalized return intents while at least one session is armed:
  globally disarm all armed sessions in this profile's broker, leave existing
  request rows durable, send one confirmation, and suppress the message from
  normal dispatch.
- **Owner-origin, bridge-shaped, unresolvable** — a message from the
  configured owner that contains a matching `A#<token>` line but fails
  resolution (unknown key, typo, cancelled request, profile mismatch): send a
  NACK ("No active bridge request matches that key — if you meant to answer a
  pending question, reply with the key shown in the question.") and return
  `handled=true` (suppressed from dispatch). The sender already passed the
  owner check, so key-existence disclosure does not apply; letting such a
  message fall to the ordinary DM agent would let an unrelated agent execute
  an answer as a fresh instruction.
- **Anything else** — return `handled=false`; it remains ordinary Telegram
  traffic (R7). No key-existence disclosure to unauthorized senders.

For a winning Telegram answer, the interceptor stores the answer in the broker
and sends a small Telegram confirmation such as
"Accepted for A#K7QM4XT9P2WBN5F." The originating waiter observes
`TELEGRAM_WON`, exits, and the existing managed process-completion path wakes
exactly the original session.

For a late Telegram duplicate, the interceptor's idempotent ack insert decides
whether to send "Already answered via chat — ignored." It still returns
`handled=true`, so the ordinary DM agent cannot act on the duplicate.

**Startup sweep (normative).** The sweep fires exactly when Telegram polling
is established with `drop_pending_updates=True` — that is, on a cold gateway
start **and** on 409-conflict recovery (both drop Telegram's pending queue;
tests/gateway/test_telegram_conflict.py:146-178, 572-573). Watcher reconnects
(`is_reconnect=True`, backlog preserved) never trigger it. When it fires, the
interceptor re-sends each `awaiting` question for its profile
("Still waiting for A#<token> — <question>"), subject to:

- a per-request rate limit: `last_swept_at`, at most once per sweep window
  (default 1 hour) — a crash-looping gateway cannot spam the owner;
- a re-check of `state='awaiting'` immediately before each send (an answer
  claiming the row mid-sweep yields at most one harmless duplicate that the
  ack path absorbs).

The sweep **also runs the orphan-armed-row GC** (R5-1): it marks `disarmed`
every armed row whose lineage tip is neither live nor resumable and that has
no non-terminal requests. A still-running waiter on such a row then observes
`disarmed` on its next poll and exits `DISARMED` by itself — no kill needed.

### 4.5 Chat answer path

**Invariant (normative, R5-2):** at most one `awaiting` request per session
at any time. The skill batches multiple questions into a single request or
defers the second ask until the first resolves.

The skill records the active request key in the originating transcript. On the
owner's next chat message while a request is awaiting:

1. Treat that message as the proposed answer unless it is an explicit control
   command such as "I'm back."
2. Call `away-bridge claim --request <id> --channel chat --answer <text>`
   before acting on the answer.
3. If chat wins, continue using that answer, then suppress the waiter's
   completion turn:
   - **Primary:** `process(action="kill")` on the waiter (tool default
     `consume_output=True` marks `_completion_consumed` **before**
     `_move_to_finished` enqueues the completion, process_registry.py:2118-
     2126 — race-free while the waiter is still running).
   - **If the waiter already exited** (its CHAT_WON completion was already
     enqueued): `process(action="log")` on it — `read_log` marks the
     completion consumed when the process has exited with observed output
     (process_registry.py:1888), and the poller skips consumed events at
     delivery time (server.py:9333).
   - Residual race: if the poller drains the queue in the instant between
     the waiter's exit-enqueue and the skill's `log` call, one extra turn
     arrives carrying the one-line envelope; the skill recognizes and ignores
     it. At most one rare extra turn per chat win, never more.
   - (`process(action="poll")` is explicitly **not** a consumption mechanism:
     it deliberately does not mark `_completion_consumed`, #10156,
     process_registry.py:1834-1844.)
4. If chat loses to Telegram, ignore the late chat answer and insert the
   idempotent chat acknowledgement. The session responds once: "Already
   answered via Telegram — ignored," then continues with the stored winning
   Telegram answer.

A message cannot both answer and disarm implicitly. "I'm back" is a control
message only when it matches the exact normalized return-intent set (§4.6). If
the owner wants to answer and return in one message, the documented form is:

```text
<answer>; I'm back
```

The session first claims the answer, then disarms after claim resolution. A
keyless answer is never silently consumed as a bridge answer; the question
remains pending and the wait continues until the owner uses the documented
form or returns to the desk.

### 4.6 Disarm semantics

- **Chat "I'm back"** disarms the originating session: it flips **every**
  `armed_sessions` row whose forward-resolved tip equals the session's
  current tip (two-sided resolution, §4.1) — not just one row. This closes
  the arm → compress → arm-again duplicate-row state: no stale sibling row
  survives to silently re-arm the session after further compression, and no
  waiter polls forever on a still-`armed` stale row (R4-1).
- **Telegram "I'm back"** disarms all armed sessions **in the same profile's
  broker** (one broker per profile home, §4.1). Telegram has no
  originating-session context and the statement represents global presence;
  sessions armed under a different profile are unaffected — two armed
  profiles require two returns. This scope is a documented acceptance, not an
  accident.
- Disarm changes `armed_sessions.status`. Waiters observe it on their next
  poll (§4.3 poll set joins `armed_sessions`) and exit `DISARMED` — there is
  no signal primitive; polling is the mechanism. A chat-side disarm may also
  kill its session's waiter directly.
- **Accepted side effect (exact-match philosophy):** an answer whose text
  itself contains a return-intent line (the owner literally answers "I'm
  back" to a pending question) still triggers the disarm after the claim —
  on Telegram, the global one. The owner who does not want this uses
  phrasing that is not an exact return-intent line (R4-7).
- Awaiting requests are not deleted. The originating session is woken by the
  waiter completion and reports: "You're back; this question is still waiting
  here." It does not silently consume a keyless Telegram message as an answer.
- Claimed answers remain immutable and process normally even if disarm races
  with their delivery. The claim transaction and disarm transaction serialize;
  whichever commits first determines the observable order, but neither loses
  the stored answer.

### 4.7 Restart and recovery

- **Telegram network outage (watcher reconnect):** backlog preserved
  (`drop_pending_updates=not is_reconnect`, adapter.py:4171-4178). Answers
  sent during the outage are delivered on reconnect and claimed normally. No
  sweep fires.
- **Gateway process restart or 409-conflict recovery (cold connect / queue
  drop):** Telegram's pending update queue is **dropped**. An answer sent
  while the gateway was down or conflicted never reaches Hermes. The bridge
  cannot recover what Telegram does not redeliver; the startup sweep (§4.4)
  re-sends the pending question so the loss surfaces as a re-ask rather than
  a silent stall. This loss mode is documented and accepted in V1.
- **Desktop/TUI backend stays alive:** the managed waiter remains active and
  wakes its exact session on claim.
- **Desktop/TUI backend exits gracefully:** waiters are killed with the
  process registry (run.py:13065 kill_all). The answer can still be claimed
  and stored by the Telegram gateway. When the owner resumes the originating
  session, reconcile fires on the session's **next turn** (resume itself
  injects no turn, server.py:9762-9773).
- **Backend crash-exits:** waiters survive as orphan subprocesses (no
  PDEATHSIG). Recovery is identity-validated (below), never bare-PID.
- **Session reaped while armed** (idle_timeout, lru_evict, ws_orphan_reap —
  server.py:813; addressed completions for reaped sessions are dropped, not
  deferred, server.py:9315-9330): the claim and answer remain durable in the
  broker, but the wake is lost until the session resumes. Mitigation: the
  skill runs `away-bridge reconcile` at the start of its first turn in any
  armed session — including after any resume. V1 does not modify reap
  policy.

**Reconcile is a reporter, not a restarter (normative).** A CLI subprocess
cannot register completions with the gateway's in-process registry: any child
it spawns enqueues nothing and wakes nobody (process_registry.py:1563-1579
enqueues only for registry-tracked sessions; the TUI poller drains the
in-process queue, server.py:9296). Therefore:

- `away-bridge reconcile` **only reports**, iterating the broker's rows
  (lineage-relative, §4.1): a stored Telegram winner awaiting delivery
  (guarded by `delivered`); an `awaiting` request whose waiter is missing or
  identity-dead; a request stuck in `prepared` (crash between prepare and
  publish — recommend cancel); disarm or cancellation states.
- The **skill**, inside its turn, acts on the report: fetch and process a
  stored winner (`away-bridge fetch`, idempotent under `delivered`); restart
  the waiter via `terminal(background=true, notify_on_complete=true)` and
  record the new `waiter_pid` + kernel `waiter_start_time`; cancel
  prepared-stuck rows; report disarm/cancellation to the owner.
- **Waiter liveness and kills are identity-validated:** a waiter is
  considered live only if PID **and** kernel start time match the recorded
  pair (mirroring `_host_pid_is_ours`, process_registry.py:738-767,
  2553-2560 — the kernel recycles PIDs). **Kill executors are split by class
  (R4-8):** registry-tracked waiters (the session's own, including
  checkpoint-recovered ones) are killed via the process tool
  (`process(action="kill")`); true orphans (spawned by a dead backend, not
  in this gateway's registry) are killed by a CLI direct kill validated by
  PID + start time + argv containing the exact `--request <full-id>`. No
  bare-PID or pattern-based kill is ever performed. **Registry-target
  resolution (R5-5):** the skill resolves the registry process id via
  `process(action="list")` filtered by argv match on
  `--request <full-id>` (or the spawn result recorded in the transcript);
  if unresolvable, it degrades to the CLI identity-validated kill, which
  works and costs at most one recognized-and-ignored completion turn.
- **GC (reconcile report, skill-executed; sweep-executed on cold start,
  §4.4):** armed rows whose lineage tip is neither live nor resumable and
  that have no non-terminal requests are marked `disarmed` (orphaned);
  identity-validated waiters for terminal or orphaned rows are killed;
  `claimed`+`delivered` rows are pruned past a retention window (default 30
  days). **Scope (R5-1, honest):** nothing polls forever once any session in
  the profile turns, the gateway cold-starts, the gateway recovers from a
  409 conflict, or the host reboots. A waiter can poll indefinitely only if
  none of these ever happens again — accepted bounded cost (one sleeping
  process, one indexed point query per 2 s).
- Reconciliation is idempotent: the `delivered` flag prevents double-processing
  of the same claimed answer across repeated resumes.

### 4.8 Configuration and security

User-facing settings live in `config.yaml`, not `.env`:

```yaml
away_bridge:
  enabled: true
  waiter_poll_seconds: 2
  sweep_window_hours: 1
  retention_days: 30
  telegram:
    owner_chat_id: "1682802389"
    owner_user_id: "1682802389"
```

- Existing Telegram credentials remain in Hermes's credential store/environment.
- Both chat ID and sender user ID must match.
- Bridge answers are owner-authored user input, not trusted system
  instructions. The skill delivers the fetched answer as user-role content in
  a fixed envelope; it is never injected as system/developer instructions.
- Request tokens use cryptographic randomness; the short display token
  retains at least 80 bits (16-char base32) or is backed by an unambiguous
  indexed prefix with collision rejection.
- Broker files use owner-only permissions.
- The interceptor NACKs owner-origin bridge-shaped failures (§4.4) but never
  discloses key existence to unauthorized senders.

## 5. State machine

```text
prepared --publish success--> awaiting --chat claim------> claimed(chat)
    |                           |       \\--telegram claim-> claimed(telegram)
    |                           |\\--disarm---------------> awaiting + waiter exits
    \\--publish failure--------> cancelled                     DISARMED
    \\
     \\--crash before publish--> prepared-stuck --> reconcile reports;
                                   skill cancels
```

`claimed` is terminal and immutable. `cancelled` is terminal. Disarm changes
session arming state but does not rewrite a request's answer state.

## 6. Failure and concurrency cases

| Case | Required behavior |
|---|---|
| Chat and Telegram claim concurrently | One conditional update succeeds; the loser receives one idempotent acknowledgement identifying the winning channel. |
| Two armed sessions ask simultaneously | Unique keys, separate request rows, and separately owned waiter processes; no shared cursor or queue ownership ambiguity. |
| Compression occurs between arming and asking | Two-sided lineage-relative prepare finds the armed row; the request inherits the arm-time key; JOIN, claim, and reconcile are unaffected (§4.1). The waiter's completion stamp forward-resolves to the resumed session. |
| arm → compress → arm again (duplicate-row state) | `arm` dedupes lineage-matching rows (refresh, not insert); chat disarm flips every lineage-matching row — no silent re-arm, no forever-polling waiter (§4.1, §4.6). |
| Telegram answer reaches gateway before waiter's first poll | Durable claimed row already present; waiter exits immediately on first query. The prepare/start/publish ordering makes pre-publish claims impossible; a bypass still converges. |
| Waiter's first poll runs before publish | `prepared`+armed → keep waiting (§4.3 complete poll set). |
| Waiter exits unexpectedly while awaiting | Session sees process failure if backend is alive and restarts it once; reconcile reports after session resume and the skill restarts it in-turn (identity-validated). Request remains durable. |
| Broker DB locked, missing, or corrupt at waiter poll | Waiter exits `ERROR` envelope; never spins silently; skill reports and may restart once. |
| Broker DB error at the interceptor while the owner's answer or return intent arrives | Owner-origin bridge-shaped batch (A# or return-intent line) is suppressed (`handled=true`) with a transient-error notice asking to resend — never dispatched to the DM agent (§4.4). |
| Gateway process down or 409-conflict recovery | Outbound publish may still send through direct `hermes send`; an inbound answer sent during the outage is dropped by Telegram's queue reset — the sweep re-sends the pending question (§4.4). No claim occurs until then. |
| Telegram network outage (no restart, no conflict) | Backlog preserved on reconnect; answer delivered and claimed normally; no sweep fires. |
| Telegram send fails | Request is cancelled, waiter exits, question remains chat-only. |
| Owner omits key | Not bridge traffic; normal Telegram session handles it. The question text repeats the required reply format. Pending request remains awaiting. |
| Owner prefixes the answer with stray text (batched) | Line-anchored matcher claims from the `A#` line; content before it is discarded (§4.4). |
| Owner answers two pending requests in one message (two `A#` tokens, either order) | Token lines partition the batch; each segment claims its own token or NACKs; no answer pollution, no silently stalled second request (§4.4). |
| Owner asks "what does A#… mean?" (mid-line occurrence) | Not matched; ordinary traffic (R7). |
| Owner typos the key / answers a cancelled request | Owner-origin, bridge-shaped, unresolvable → NACK + suppressed from dispatch (§4.4). Never falls to the ordinary DM agent. |
| Owner's answer exceeds one Telegram message | Answer is the single update carrying the token; continuation chunks are ordinary traffic (R7). Question text instructs one-message replies. |
| Owner sends answer + "I'm back" rapid-fire (merged batch, either order) | Line-anchored match; claim processed first, disarm second, regardless of line order (§4.4). No fall-through. |
| Owner's answer text is literally "I'm back" | Claim succeeds, then the disarm fires (globally on Telegram) — documented accepted side effect of exact matching (§4.6). |
| DM session mid-turn when the answer arrives | Interceptor sits before the session guard (§4.4 seam); the answer is claimed immediately, never merged into `_pending_messages`. |
| Desktop-side claim holds the broker write lock when the interceptor runs | Interceptor broker I/O is `asyncio.to_thread` with a short busy timeout; the gateway event loop never blocks; on timeout the batch is suppressed with a resend notice (§4.4). |
| Owner answers Telegram twice | First claims; second is intercepted, ignored, and receives at most one duplicate acknowledgement. |
| Owner answers both channels | First successful claim wins; other answer is ignored and acknowledged once. |
| Owner says Telegram "I'm back" with pending questions | All armed sessions in the profile disarm; waiters wake their sessions with "still waiting here." No answer is inferred. |
| Ordinary Telegram message while bridge active | Interceptor returns unhandled; normal Telegram DM behavior is unchanged. |
| Unauthorized sender guesses a valid key | Identity mismatch; no claim, no key-existence disclosure. |
| Origin session compressed mid-wait | Completion ownership forward-resolves through lineage (server.py:9012-9019); request rows keep the arm-time key; nothing is orphaned. |
| Origin session reaped while armed | Wake is lost; claim and answer stay durable; reconcile on the next turn after resume recovers them (§4.7). |
| Session resumed without any message | No turn is injected (server.py:9762-9773); reconcile fires on the owner's first prompt after resume (§3, §4.7). |
| Waiter PID recycled onto an unrelated process | Start-time identity validation (and argv match for kills) refuses false liveness and false kills (§4.7). |
| Request stuck in `prepared` (crash between prepare and publish) | Reconcile reports it; the skill cancels it (§4.7). |
| Session armed but never resumed again | Sweep GC (on cold start/conflict recovery) or reconcile GC marks the armed row disarmed (orphaned); a still-running waiter exits DISARMED on its next poll; identity-validated waiters for terminal rows are killed; delivered rows pruned past retention (§4.4, §4.7). Bounded residual: if no session ever turns and the gateway never cold-starts and the host never reboots, the waiter polls indefinitely — accepted. |
| Session needs two inputs while one is pending | Invariant: at most one `awaiting` request per session; the skill batches both questions into the one pending request or defers the second ask (§4.5). |

## 7. Alternatives considered

- **Session heartbeat polling** — rejected. An agent cannot reliably invoke the
  user-only `/heartbeat` arming path (watchdog starts only from the slash
  handler; cached manager never reloads external writes; gateway watch registry
  is in-memory), and it also creates continuous model turns.
- **Per-session Telegram watermarks** — rejected. Foreign/unknown keyed messages
  can wedge cursors and bounded windows can starve older valid answers.
- **Cron fallback** — rejected for session wake. Cron delivery is isolated and
  does not inject into an existing conversation.
- **Accept normal Telegram dual consumption** — rejected because it violates
  R6 and could cause an unrelated Telegram agent to act on an answer.
- **Interceptor inside the adapter before batching** — rejected: chunk-split
  long answers would be claimed on the first chunk only (§4.4 seam analysis).
- **Interceptor in `run.py _handle_message`** — rejected: runs after
  session-guard queueing; busy DM sessions would delay/garble claims
  (§4.4 seam analysis).
- **Waiter pushes the answer through its stdout** — rejected: completion
  events carry only the last 2,000 output characters; long answers would be
  silently truncated (§4.3 stdout contract).
- **Signal-based waiter disarm** — rejected: no push primitive exists; disarm
  is poll-observed (§4.3, §4.6).
- **Keying broker rows on the current session key** — rejected: the gateway
  `session_key` rotates on compression (server.py:4943-4975); current-key
  rows break the JOIN, the chat claim, and reconcile (R3-C1). Arm-time
  inheritance with two-sided lineage-relative lookup is used instead (§4.1).
- **`process(action="poll")` as chat-win consumption** — rejected: `poll`
  deliberately never marks `_completion_consumed` (#10156); kill + log
  draining is used instead (§4.5).
- **Start-anchored interceptor matcher** — rejected: contradicts mixed-batch
  handling and lets owner answers fall through to the DM agent; the matcher is
  line-anchored and token-partitioned (§4.4).
- **Single-token batch handling** — rejected: a two-token batch would pollute
  the first answer and silently stall the second request; token lines
  partition the batch (§4.4, R4-4).
- **CLI reconcile restarting waiters** — rejected: CLI-spawned children are
  invisible to the gateway's process registry and wake nobody; reconcile
  reports, the skill restarts in-turn (§4.7).
- **Registry kill for orphan waiters** — rejected: registry `process(action="kill")`
  reaches only registry-tracked waiters; true orphans get a CLI
  identity-validated direct kill (§4.7).
- **Separate Telegram bot** — technically clean but adds credentials and a
  second user conversation. Retained as fallback if the pre-dispatch
  interceptor proves unmaintainable.
- **`/handoff telegram`** — rejected because it moves the full conversation
  instead of relaying only input requests.

## 8. Implementation plan (after design approval)

1. **Broker and tests** — SQLite schema/migrations (incl. `waiter_pid`,
   `waiter_start_time`, `last_swept_at`; `PRAGMA foreign_keys=ON` on every
   connection), arm (with dedupe)/prepare (two-sided lineage-relative
   lookup)/publish (kernel start-time capture)/claim/fetch, duplicate ack,
   disarm (all lineage-matching rows), reconcile reporting + GC (incl.
   sweep-side orphan marking), single-awaiting invariant, and concurrency
   tests using two real SQLite connections.
2. **Managed waiter and exact-session wake tests** — the complete poll set
   (all outcomes incl. `prepared` branches, no-row, and DB-error `ERROR`),
   the one-line stdout envelope, the 2 s default poll interval, kill+log
   completion suppression after chat wins, and a Desktop/TUI integration test
   proving session A's completion never enters session B, including
   compression-lineage ownership, a compression event between arm and ask,
   and the arm → compress → arm-again dedupe/disarm sequence.
3. **Telegram pre-dispatch interceptor at the named seam** — owner
   authorization, line-anchored matching (stray-text-prefixed answers claimed;
   mid-line mentions not), multi-token batch partitioning with the pinned
   extraction boundary, mixed answer+disarm batches in both orders,
   chunked-answer handling, owner-shaped NACK, broker DB-error suppression
   (A# and return-intent lines) with resend notice, duplicate
   acknowledgement, global disarm, sweep triggers (cold start **and**
   409-conflict recovery; never watcher reconnect) with per-request rate
   limit, pre-send re-check, and orphan-armed-row GC, `asyncio.to_thread`
   broker I/O, and proof that handled messages are neither persisted nor
   dispatched to the ordinary Telegram session — including the
   busy-DM-session and rapid-follow-up batching cases.
4. **Skill** — natural-language arm/disarm behavior with environment checks
   (local backend, allowlisted commands), ordered prepare/start/publish
   protocol with profile-scoped `hermes send` and the single-awaiting
   invariant, chat-side claims with kill+log consumption, and resume
   reconciliation (report → in-turn waiter restart, identity-validated;
   prepared-stuck cancellation; GC; split kill executors).
5. **End-to-end acceptance** — two simultaneous desktop sessions; Telegram and
   chat races in both orders; late duplicates; ordinary Telegram traffic;
   gateway restart and conflict recovery (sweep re-ask + orphan GC) vs network
   outage (backlog preserved, no sweep); desktop restart/reconcile;
   session-reap recovery; compression between arm and ask; duplicate-arm
   dedupe/disarm; global Telegram disarm.

## 9. Acceptance gates

The design is implemented only when all are demonstrated against an isolated
Hermes home and real Telegram test chat:

1. Telegram-first resumes only the correct originating desktop session.
2. Chat-first prevents Telegram from changing the outcome.
3. A late answer on either channel produces exactly one small acknowledgement
   identifying the winning channel.
4. Two concurrent armed sessions never exchange answers or notifications.
5. A bridge reply never appears in ordinary Telegram session history and never
   invokes its agent — including when the DM session is mid-turn, when the
   owner rapid-fires an answer plus "I'm back" (batched), and when the answer
   is prefixed with stray text.
6. Ordinary Telegram messages remain unaffected.
7. Gateway restart and 409-conflict recovery preserve claimability and trigger
   the sweep re-ask (rate-limited) and orphan-armed-row GC (a still-running
   orphan waiter exits DISARMED by itself); a network outage preserves the
   backlog and triggers no sweep. Desktop restart preserves durable answer
   recovery without double-processing.
8. Exact "I'm back" semantics match §4.6 in both channels, including the
   per-profile scope of the Telegram global disarm and the flip of every
   lineage-matching armed row on chat disarm.
9. An owner-origin malformed, unknown, or **broker-unavailable** `A#` reply or
   return-intent line is NACKed (or suppressed with a transient-error notice)
   and never dispatched to the ordinary Telegram session.
10. A winning chat answer's waiter produces no model turn (kill with consumed
    output, or log-drain if already exited); in the rare race where the
    completion is delivered before the skill can consume it, exactly one
    extra turn arrives and is recognized and ignored. DISARMED, CANCELLED,
    and ERROR wakes produce exactly one turn each.
11. A compression event between arming and asking breaks nothing: the request
    inherits the arm-time key, the waiter JOIN matches, the chat claim
    succeeds, reconcile finds the row, and the wake lands in the resumed
    session.
12. A stray-text-prefixed batched answer is claimed; a mid-line "what does
    A#… mean?" mention is not.
13. The interceptor's broker I/O never blocks the gateway event loop under a
    held write lock (verified by a lock-holding fixture during an inbound
    message).
14. A two-token batch (two pending requests answered in one message, either
    order) claims both requests independently — no answer pollution, no
    stalled second request. The stored answers exclude the token lines but
    include same-line remainders.
15. A session never has two `awaiting` requests: a second needed input is
    batched into the pending request or deferred until the first resolves.

## 10. Remaining product choices for owner approval

- Final user-visible wording for Telegram questions, NACKs, transient-error
  notices, and duplicate acknowledgements (must identify the winning channel —
  R4).
- Whether the optional separate-bot fallback should remain documented or be
  removed after the interceptor prototype succeeds.
- Whether armed sessions should be exempted from idle reaping in a future
  Hermes revision (V1 documents the loss and reconciles instead).
- Default retention/poll/sweep knobs in §4.8 (2 s poll, 1 h sweep window,
  30 d retention) — sensible defaults proposed, owner-tunable.

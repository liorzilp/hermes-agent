# Away-Mode Telegram Bridge — Design Doc

**Status:** Rev 4 — requirements owner-confirmed 2026-09-28; design in
independent review loop (rounds 1–2 folded)
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
  background waiter owned by each originating session. The normal Telegram DM
  session never sees bridge protocol messages. Commit `0dda6126c`; frozen
  snapshot `away-bridge-revs/REV-3.md`; SHA-256
  `d37bd6df7e5dd534d533cf5d02c49be58d9958d9d6692daa6d93762fa19fe75f`.
- **Rev 4** — folds independent review round 2 (deleg_89a935fd, verdict
  REVISE). The round-2 reviewer verified the Rev 3 core architecture as sound
  (wake path, claim transaction, prepare/start/publish ordering, interceptor
  feasibility) and raised mechanistic gaps, all folded here: gateway-restart
  backlog loss, the exact interceptor seam, the waiter stdout contract,
  DISARMED detection via polling, session identity pinned to the gateway
  `session_key`, completion-turn cost suppression, reap-path recovery,
  owner-shaped NACKs, and waiter environment constraints.

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

| # | Finding (severity) | Disposition in Rev 4 |
|---|---|---|
| R2-1 | §4.7 "Gateway restart" row was wrong: a cold process restart drops Telegram's pending queue (`drop_pending_updates=not is_reconnect`, adapter.py:4171-4178; tests/gateway/test_telegram_conflict.py:573). Answers sent during downtime would be silently discarded. (High) | §4.7 now distinguishes reconnect (backlog preserved) from restart (backlog dropped). Mitigation: the interceptor runs a startup sweep that re-sends `awaiting` questions. The loss mode is documented and accepted in V1. |
| R2-2 | The interceptor seam was underspecified; the literal "after authorization" reading (run.py `_handle_message`, 14805+) runs after batching and session-guard queueing (base.py:6119-6124 merge_text), causing delayed/garbled claims. (High) | §4.4 names the seam exactly: top of `BasePlatformAdapter.handle_message` (base.py:5931), gated on platform == Telegram — after adapter auth and batching, before session guard/persistence/dispatch. Batching, chunking, and mixed answer+disarm rules specified. |
| R2-3 | The answer rides the waiter's stdout, capped at the last 2,000 chars (process_registry.py:1563-1579) — long Telegram answers silently truncated. (High) | §4.3 waiter stdout contract: the waiter prints only a one-line envelope (outcome + req_key). The skill fetches the full answer from the broker via `away-bridge fetch`, idempotent under `delivered`. |
| R2-4 | The waiter could never detect DISARMED: §4.6 "signals all waiters" names a push primitive that does not exist; disarm mutates only `armed_sessions`. (High) | §4.3 waiter poll set joins `armed_sessions.status` in the same point query; §4.6 reworded to polling. Chat-side disarm may also kill the waiter directly. |
| R2-5 | Session identity was conflated (agent `session_id` vs UI tab id vs gateway `session_key`); terminal spawns are stamped with `session_key` (terminal_tool.py:2764-2766, 2897), and only the gateway `session_key` survives compression (server.py:8937-8964, 9012-9019). (Medium) | §4.1: the broker stores the gateway `session_key` only; schema columns renamed; §4.2 specifies the Telegram claim predicate. |
| R2-6 | Every waiter exit costs a full model turn (`_notification_poller_loop` chains a submit for every delivered completion, server.py:9363-9382); "ignorable cleanup" hid a real cost. (Medium) | §4.5: after a winning chat claim the skill consumes the waiter's completion (`process(action="poll")` → `_completion_consumed`, process_registry.py:1889; poller skips consumed events, server.py:9333). DISARMED/CANCELLED wakes are counted honestly in the cost model. |
| R2-7 | Session-reap paths (idle_timeout, lru_evict, ws_orphan_reap — server.py:813) drop addressed completions when the owner session is gone (server.py:9315-9330); §4.7 covered only backend exit. Crash-exit backends leave orphan waiters (no PDEATHSIG; graceful exit kills via run.py:13065). (Medium) | §4.7: reconcile runs at the start of the first turn of any armed session after resume, not only after backend exit; waiter restart is PID-aware via `requests.waiter_pid`; the reap-path wake loss is documented. |
| R2-8 | Owner-sent, bridge-shaped but unresolvable keys (typo, cancelled, profile mismatch) fell through to the ordinary DM agent, which could execute an answer as a fresh instruction. (Medium) | §4.4: owner-origin messages matching the `A#<token>` shape that fail resolution receive a NACK and are suppressed from dispatch. `handled=false` is reserved for non-bridge-shaped traffic. |
| R2-9 | Waiter-start environment assumptions unstated: approval prompts would fire exactly when the owner is away; sandboxed sessions cannot reach the broker or credentials; `hermes send` resolves the default profile unless scoped (send_cmd.py:331-334). (Medium) | §4.3 step 2 states V1 constraints: local terminal backend only, `away-bridge` command family allowlisted/pre-approved, sandboxed/non-local sessions refused at arm time; `publish` passes the session's profile scope to `hermes send`. |
| R2-10 | Residual wording: leftover "heartbeat-free" phrase (§4.5), 4-char example keys next to the 80-bit rule (§4.4), "UI session identity" instead of `session_key` (§4.3). (Low) | All fixed; key examples now show the specified ≥80-bit form. |

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
| R3 | Lifetime | The session remains armed until the owner says "I'm back" in that session or from Telegram. No time-based expiry. Telegram "I'm back" means the owner is globally back and disarms all armed sessions. |
| R4 | First answer wins | The first channel whose claim transaction succeeds becomes authoritative. A later answer from the other channel is ignored and receives one small acknowledgement identifying the winning channel. |
| R5 | Authorized responder | Only the configured owner Telegram identity/chat may answer. |
| R6 | Multi-session isolation | Every request has a unique key bound to one originating session. No other session, including the ordinary Telegram DM session, receives or acts on its answer. |
| R7 | Non-bridge Telegram traffic | Messages without a valid active bridge key are processed normally by the Telegram DM session. |

## 3. Non-goals and v1 boundary

- No whole-conversation mirroring or `/handoff` takeover.
- No group-chat support.
- No bridge for background progress/status messages.
- No delivery while the originating desktop/TUI backend is completely shut
  down. Telegram answers remain durable and are reconciled when that session is
  resumed; Hermes cannot continue a desktop turn while its backend is absent.
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

**Session identity (normative).** The broker stores the **gateway
`session_key`** — the durable TUI/gateway identifier that survives context
compression via `resolve_resume_session_id` (tui_gateway/server.py:8937-8964,
9012-9019). It is not the agent's per-compression `session_id` and not a UI
tab id. This is the same value the terminal runtime stamps on background
spawns (terminal_tool.py:2764-2766, 2897) and the completion poller resolves,
so request rows, waiter wake routing, and `reconcile` all agree on exactly one
identity.

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
    FOREIGN KEY(session_key) REFERENCES armed_sessions(session_key)
);

CREATE TABLE duplicate_acks (
    req_key          TEXT NOT NULL,
    late_channel     TEXT NOT NULL CHECK(late_channel IN ('chat','telegram')),
    sent_at          REAL NOT NULL,
    PRIMARY KEY(req_key, late_channel)
);
```

SQLite runs in WAL mode with a busy timeout. All state transitions use explicit
transactions. `BEGIN IMMEDIATE` serializes competing chat and Telegram claims.
The `delivered` flag makes origin-side processing idempotent across resumes.
`waiter_pid` makes waiter restarts PID-aware (§4.7).

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

The chat path additionally passes `AND session_key=:session_key` as
defense-in-depth (the asking session should only claim its own request). The
Telegram interceptor cannot know a session key, so it pre-resolves ownership —
owner identity (§4.8) plus the unique `req_key` — and runs the same
conditional update on `req_key AND state='awaiting'`. Both paths converge on
the identical single-row conditional update as their ordering point.

Duplicate acknowledgement is separately idempotent:

```sql
INSERT OR IGNORE INTO duplicate_acks(req_key, late_channel, sent_at)
VALUES (:key, :channel, :now);
```

Only the process whose insert succeeds sends the small acknowledgement. This
resolves the Rev 2 contradiction between "do not ack" and "second gets the
ack": the ack is defined exactly once, here.

### 4.3 Request publication and waiter startup

When an armed session needs input, the skill performs this ordered protocol:

1. `away-bridge prepare` inserts a `prepared` request and returns a random
   128-bit request key: a full internal identifier plus the short display
   token used in the question text (`A#` + 16-char base32, ≥80 bits, e.g.
   `A#K7QM4XT9P2WBN5F`).
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
3. Hermes's terminal runtime stamps the spawn with the originating session's
   durable `session_key` (terminal_tool.py:2764-2766, 2897) — the same value
   the completion poller resolves, including through compression lineage
   (server.py:8937-8964, 9012-9019). Desktop/TUI's process-notification
   poller injects the one-shot completion only into the session that owns it;
   foreign sessions requeue or drop rather than adopt it
   (tui_gateway/server.py:9233+; exact-session ownership filter and
   compression-lineage guard verified at server.py:9300+, 9315-9330).
4. After the waiter is confirmed started, `away-bridge publish` transitions
   `prepared → awaiting`, records the waiter PID in `requests.waiter_pid`,
   and sends the question via `hermes send -t telegram:<chat>`.
   `publish` invokes `hermes send` with the session's own profile scope
   (`--profile` / `HERMES_HOME` passthrough; `hermes send` otherwise resolves
   the default profile, send_cmd.py:331-334). A profile mismatch would mark
   `awaiting` in a broker the gateway interceptor never reads.
5. If Telegram delivery fails, `publish` changes the request to `cancelled`.
   The waiter exits `CANCELLED`; the session asks in chat only and says the
   Telegram copy failed.

**Waiter poll set and exit protocol.** There is no push mechanism anywhere in
this design — the broker DB is the only channel — so each poll interval the
waiter performs one indexed point query:

```sql
SELECT r.state, r.winning_channel, a.status AS armed_status
FROM requests r JOIN armed_sessions a USING(session_key)
WHERE r.req_key = ?;
```

- `state='awaiting'` and `armed_status='armed'` → keep waiting.
- `state='claimed'` → exit `TELEGRAM_WON` (winning_channel telegram) or
  `CHAT_WON` (winning_channel chat).
- `armed_status='disarmed'` (with `state='awaiting'`) → exit `DISARMED`; the
  request row stays `awaiting` and visibly pending in the session.
- `state='cancelled'` → exit `CANCELLED`.

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
window. Idle cost is a sleeping local process — no model turns. Each waiter
exit normally produces exactly one completion turn: the chat-win path
suppresses its cost (§4.5), and DISARMED/CANCELLED wakes each cost one model
turn by design (counted, not hidden).

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
The named seam is the only position satisfying all constraints.

**Placement consequences (normative):**

- **Answers are claimed from the batched text.** The matcher recognizes
  `A#<token>` at the start of the (possibly multi-line) batched text and
  treats everything after the token line — embedded newlines included — as
  the answer.
- **One protocol reply per message.** Telegram splits messages over 4,096
  characters into multiple updates; only the first would carry the key. V1
  defines the answer as the single update carrying the token. The question
  text instructs: "reply in one message (up to 4,096 characters), starting
  with the key." Continuation chunks without a key are ordinary traffic (R7).
- **Mixed answer + disarm batches are deterministic.** If a batch contains
  both an `A#` line and an exact normalized return-intent line (in either
  order), the bridge processes the claim first and the disarm second,
  regardless of line order. This matches the chat-side documented form
  (§4.5). No mixed batch falls through to the DM agent.

The handler is enabled only when `away_bridge.enabled=true` and the configured
owner chat/user match the inbound event. It recognizes:

- `A#<token> <answer>` — resolve the active request, attempt the Telegram
  claim transaction (§4.2), send success/late-duplicate feedback, and return
  `handled=true` so the normal Telegram DM session never receives the message.
- Exact normalized return intents while at least one session is armed:
  `I'm back`, `Im back`, or `חזרתי` — globally disarm all armed sessions,
  leave existing request rows durable, send one confirmation, and suppress
  the protocol message from normal dispatch.
- **Owner-origin, bridge-shaped, unresolvable** — a message from the
  configured owner that matches the `A#<token>` shape but fails resolution
  (unknown key, typo, cancelled request, profile mismatch): send a NACK
  ("No active bridge request matches that key — if you meant to answer a
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
whether to send "Already answered in chat — ignored." It still returns
`handled=true`, so the ordinary DM agent cannot act on the duplicate.

**Startup sweep.** When the gateway (hence the interceptor) starts and the
broker contains `awaiting` requests for its profile, the interceptor re-sends
each pending question to the owner ("Still waiting for A#<token> —
<question>"). This is the mitigation for the restart backlog loss (§4.7) and
doubles as a post-outage reassurance. The sweep is rate-limited (once per
gateway start; not per poll cycle).

Sending from inside the inbound handler is precedented and safe: the pairing
flow already does `await adapter.send(...)` inside `_handle_message`
(run.py:14996); no re-entry into the update pipeline, no deadlock.

### 4.5 Chat answer path

The skill records the active request key in the originating transcript. On the
owner's next chat message while a request is awaiting:

1. Treat that message as the proposed answer unless it is an explicit control
   command such as "I'm back."
2. Call `away-bridge claim --channel chat --request <id> --answer <text>` before
   acting on the answer.
3. If chat wins, continue using that answer, then immediately consume the
   waiter's completion so the `CHAT_WON` exit costs zero model turns:
   `process(action="poll")` on the waiter marks it consumed
   (`_completion_consumed`, process_registry.py:1889) and the notification
   poller skips consumed events (server.py:9333). (Killing the waiter and
   draining is an equivalent fallback.)
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

- **Chat "I'm back"** disarms only that originating session.
- **Telegram "I'm back"** disarms all armed sessions, because Telegram has no
  originating-session context and the statement represents global presence.
- Disarm changes `armed_sessions.status`. Waiters observe it on their next
  poll (§4.3 poll set joins `armed_sessions`) and exit `DISARMED` — there is
  no signal primitive; polling is the mechanism. A chat-side disarm may also
  kill its session's waiter directly.
- Awaiting requests are not deleted. The originating session is woken by the
  waiter completion and reports: "You're back; this question is still waiting
  here." It does not silently consume a keyless Telegram message as an answer.
- Claimed answers remain immutable and process normally even if disarm races
  with their delivery. The claim transaction and disarm transaction serialize;
  whichever commits first determines the observable order, but neither loses
  the stored answer.

### 4.7 Restart and recovery

- **Telegram network outage (watcher reconnect):** backlog preserved
  (`drop_pending_updates=not is_reconnect`, adapter.py:4171-4178; verified by
  tests/gateway/test_telegram_conflict.py:573). Answers sent during the
  outage are delivered on reconnect and claimed normally.
- **Gateway process restart (cold connect):** Telegram's pending update queue
  is **dropped** on cold connect. An answer sent while the gateway process
  was down never reaches Hermes. The bridge cannot recover what Telegram does
  not redeliver; the startup sweep (§4.4) re-sends the pending question so the
  loss surfaces as a re-ask rather than a silent stall. This loss mode is
  documented and accepted in V1.
- **Desktop/TUI backend stays alive:** the managed waiter remains active and
  wakes its exact session on claim.
- **Desktop/TUI backend exits gracefully:** waiters are killed with the
  process registry (run.py:13065 kill_all). The answer can still be claimed
  and stored by the Telegram gateway. When the owner resumes the originating
  session, the skill's first action is `away-bridge reconcile --session
  <session_key>`: process a stored Telegram winner (guarded by `delivered`),
  restart a missing waiter for an awaiting request, or report
  disarm/cancellation.
- **Backend crash-exits:** waiters survive as orphan subprocesses (no
  PDEATHSIG). `reconcile` is PID-aware: it checks `requests.waiter_pid` for
  liveness before spawning a replacement, and never double-spawns.
- **Session reaped while armed** (idle_timeout, lru_evict, ws_orphan_reap —
  server.py:813; addressed completions for reaped sessions are dropped, not
  deferred, server.py:9315-9330): the claim and answer remain durable in the
  broker, but the wake is lost until the session resumes. Mitigation: the
  skill runs `away-bridge reconcile --session <session_key>` at the start of
  its first turn in any armed session — including after any resume — not only
  after backend exit. V1 does not modify reap policy.
- Reconciliation is idempotent: the `delivered` flag prevents double-processing
  of the same claimed answer across repeated resumes.

### 4.8 Configuration and security

User-facing settings live in `config.yaml`, not `.env`:

```yaml
away_bridge:
  enabled: true
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
    |                           |       \--telegram claim-> claimed(telegram)
    |                           |\--disarm---------------> awaiting + waiter exits
    \--publish failure--------> cancelled                     DISARMED
```

`claimed` is terminal and immutable. `cancelled` is terminal. Disarm changes
session arming state but does not rewrite a request's answer state.

## 6. Failure and concurrency cases

| Case | Required behavior |
|---|---|
| Chat and Telegram claim concurrently | One conditional update succeeds; the loser receives one idempotent acknowledgement. |
| Two armed sessions ask simultaneously | Unique keys, separate request rows, and separately owned waiter processes; no shared cursor or queue ownership ambiguity. |
| Telegram answer reaches gateway before waiter's first poll | Durable claimed row already present; waiter exits immediately on first query. The prepare/start/publish ordering makes pre-publish claims impossible; a bypass still converges. |
| Waiter exits unexpectedly while awaiting | Session sees process failure if backend is alive and restarts it once; reconciliation restarts it after session resume (PID-aware). Request remains durable. |
| Gateway process down | Outbound publish may still send through direct `hermes send`; an inbound answer sent during downtime is dropped by Telegram's cold-connect queue reset — the startup sweep re-sends the pending question (§4.7). No claim occurs until then. |
| Telegram network outage (no process restart) | Backlog preserved on reconnect; answer delivered and claimed normally. |
| Telegram send fails | Request is cancelled, waiter exits, question remains chat-only. |
| Owner omits key | Not bridge traffic; normal Telegram session handles it. The question text repeats the required reply format. Pending request remains awaiting. |
| Owner typos the key / answers a cancelled request | Owner-origin, bridge-shaped, unresolvable → NACK + suppressed from dispatch (§4.4). Never falls to the ordinary DM agent. |
| Owner's answer exceeds one Telegram message | Answer is the single update carrying the token; continuation chunks are ordinary traffic (R7). Question text instructs one-message replies. |
| Owner sends answer + "I'm back" rapid-fire (merged batch) | Claim processed first, disarm second, regardless of line order (§4.4). No fall-through. |
| DM session mid-turn when the answer arrives | Interceptor sits before the session guard (§4.4 seam); the answer is claimed immediately, never merged into `_pending_messages`. |
| Owner answers Telegram twice | First claims; second is intercepted, ignored, and receives at most one duplicate acknowledgement. |
| Owner answers both channels | First successful claim wins; other answer is ignored and acknowledged once. |
| Owner says Telegram "I'm back" with pending questions | All armed sessions disarm; waiters wake their sessions with "still waiting here." No answer is inferred. |
| Ordinary Telegram message while bridge active | Interceptor returns unhandled; normal Telegram DM behavior is unchanged. |
| Unauthorized sender guesses a valid key | Identity mismatch; no claim, no key-existence disclosure. |
| Origin session compressed | Process-notification ownership follows durable/compression lineage through existing Hermes routing; the request remains bound to the gateway `session_key`, which is compression-stable. |
| Origin session reaped while armed | Wake is lost; claim and answer stay durable; reconcile on next turn after resume recovers them (§4.7). |

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
- **Separate Telegram bot** — technically clean but adds credentials and a
  second user conversation. Retained as fallback if the pre-dispatch
  interceptor proves unmaintainable.
- **`/handoff telegram`** — rejected because it moves the full conversation
  instead of relaying only input requests.

## 8. Implementation plan (after design approval)

1. **Broker and tests** — SQLite schema/migrations (incl. `waiter_pid`),
   prepare/publish/claim/fetch, duplicate ack, disarm, reconcile, and
   concurrency tests using two real SQLite connections.
2. **Managed waiter and exact-session wake tests** — waiter poll-set outcomes
   (TELEGRAM_WON/CHAT_WON/DISARMED/CANCELLED), the one-line stdout envelope,
   completion-consumption suppression after chat wins, and a Desktop/TUI
   integration test proving session A's completion never enters session B,
   including compression-lineage ownership.
3. **Telegram pre-dispatch interceptor at the named seam** — owner
   authorization, valid-key claim from batched text, mixed answer+disarm
   batches in both orders, chunked-answer handling, owner-shaped NACK,
   duplicate acknowledgement, global disarm, startup sweep, and proof that
   handled messages are neither persisted nor dispatched to the ordinary
   Telegram session — including the busy-DM-session and rapid-follow-up
   batching cases.
4. **Skill** — natural-language arm/disarm behavior with environment checks
   (local backend, allowlisted commands), ordered prepare/start/publish
   protocol with profile-scoped `hermes send`, chat-side claims with
   completion consumption, and resume reconciliation (PID-aware).
5. **End-to-end acceptance** — two simultaneous desktop sessions; Telegram and
   chat races in both orders; late duplicates; ordinary Telegram traffic;
   gateway restart (startup sweep re-ask) vs network outage (backlog
   preserved); desktop restart/reconcile; session-reap recovery; global
   Telegram disarm.

## 9. Acceptance gates

The design is implemented only when all are demonstrated against an isolated
Hermes home and real Telegram test chat:

1. Telegram-first resumes only the correct originating desktop session.
2. Chat-first prevents Telegram from changing the outcome.
3. A late answer on either channel produces exactly one small acknowledgement.
4. Two concurrent armed sessions never exchange answers or notifications.
5. A bridge reply never appears in ordinary Telegram session history and never
   invokes its agent — including when the DM session is mid-turn and when the
   owner rapid-fires an answer plus "I'm back" (batched).
6. Ordinary Telegram messages remain unaffected.
7. Gateway restart preserves claimability and triggers the startup sweep
   re-ask; a network outage preserves the backlog. Desktop restart preserves
   durable answer recovery without double-processing.
8. Exact "I'm back" semantics match §4.6 in both channels.
9. An owner-origin malformed or unknown `A#` reply is NACKed and never
   dispatched to the ordinary Telegram session.
10. A winning chat answer's waiter exit produces no model turn (completion
    consumed); DISARMED wakes produce exactly one.

## 10. Remaining product choices for owner approval

- Final user-visible wording for Telegram questions, NACKs, and duplicate
  acknowledgements.
- Whether the optional separate-bot fallback should remain documented or be
  removed after the interceptor prototype succeeds.
- Whether armed sessions should be exempted from idle reaping in a future
  Hermes revision (V1 documents the loss and reconciles instead).

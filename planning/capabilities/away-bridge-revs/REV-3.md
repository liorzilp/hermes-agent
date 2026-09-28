# Away-Mode Telegram Bridge — Design Doc

**Status:** Rev 3 — requirements owner-confirmed 2026-09-28; design in
independent review loop
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
  session never sees bridge protocol messages.

## Review round 1 findings folded (all verified against source)

| # | Finding (severity) | Disposition in Rev 3 |
|---|---|---|
| 1 | Heartbeat arming is user-only: `_start_heartbeat_watchdog` starts only from the `/heartbeat` slash handler (cli_commands_mixin.py:2497,2536); the cached `HeartbeatManager` never reloads state written by another process; gateway watch registry is in-memory and re-arms only on user `/heartbeat` (run.py:19436-19443). The doc's wake path was unimplementable. (Critical) | Wake path redesigned around managed background processes + process-notification delivery, which is agent-addressable and verified live (tui_gateway/server.py:9233+ `_notification_poller_loop`). |
| 2 | Shared-chat per-session watermarks can wedge permanently on foreign/unknown keys and the 200-cap window can starve valid answers. (High) | Watermark scanning removed entirely. Answers are routed by key at the gateway interceptor; no session reads the shared Telegram history. |
| 3 | Rev 2 §4.3 skip-rule contradicted its own parenthetical for acked/unknown keys ("do not advance" vs "provably not addressed"). (High) | Contradictory section deleted with the mechanism it specified. |
| 4 | "do not ack" bullet contradicted §5's "second gets the small ack" and the ack-idempotency bullet. (Medium) | Ack semantics now specified once, in §4.2/§4.4, as an idempotent DB insert. |
| 5 | Dual consumption on Telegram violated R6 ("never consume") rather than being a cosmetic wart; an armed normal DM session or the DM agent could act on answers. (High) | The pre-dispatch interceptor suppresses bridge traffic from normal dispatch entirely (§4.4). |
| 6 | Keyless owner answers and ambiguous disarm phrases caused silent stalls or mis-disarm. (Medium) | Answer/disarm protocol re-specified with exact match sets and no implicit combine (§4.5, §4.6). |
| 7 | Gateway-down failure-table wording was wrong ("answers queue in state.db"). (Low) | Corrected in §4.7. |

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
  async completion delivery as supported. Stateless one-shot/API requests,
  cron runs, and Kanban workers are rejected at arm time.

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

### 4.1 State and profile scope

State lives at:

```
$HERMES_HOME/away-bridge/bridge.db
```

The path is resolved through Hermes profile helpers; it is never hardcoded to
`~/.hermes`. The Telegram gateway and originating session must use the same
profile. V1 fails closed if the receiver profile does not contain the request.

Schema (logical form):

```sql
CREATE TABLE armed_sessions (
    session_id       TEXT PRIMARY KEY,
    armed_at         REAL NOT NULL,
    surface          TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('armed','disarmed')),
    disarmed_at      REAL
);

CREATE TABLE requests (
    req_key          TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
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
    FOREIGN KEY(session_id) REFERENCES armed_sessions(session_id)
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

### 4.2 First-answer-wins transaction

Both channels call the same broker operation:

```sql
UPDATE requests
SET state='claimed', winning_channel=:channel,
    answer=:answer, claimed_at=:now
WHERE req_key=:key AND session_id=:session_id AND state='awaiting';
```

- `rowcount == 1`: this channel won.
- `rowcount == 0`: load the row. If already `claimed`, this channel lost. If
  unknown, prepared, or cancelled, reject without treating it as an answer.
- The answer is considered "successfully processed first" at the successful
  conditional update, not at message arrival time. That gives one durable,
  testable ordering point across processes.

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
   128-bit request key rendered as a short human-safe token plus full internal
   identifier.
2. The originating session starts:

   ```text
   away-bridge wait --request <full-id>
   ```

   through the managed terminal tool with `background=true` and
   `notify_on_complete=true`.
3. Hermes terminal runtime stamps that process with the originating UI session
   identity. Desktop/TUI's existing process-notification poller injects its
   one-shot completion only into the session that owns it; foreign sessions
   requeue or drop it rather than adopting it (tui_gateway/server.py:9300+,
   verified: exact-session ownership filter, compression-lineage aware).
4. After the waiter is confirmed started, `away-bridge publish` transitions
   `prepared → awaiting` and sends the question via the configured Telegram
   adapter/send engine (`hermes send -t telegram:<chat>`).
5. If Telegram delivery fails, `publish` changes the request to `cancelled`.
   The waiter exits with `CANCELLED`; the session asks in chat only and says the
   Telegram copy failed.

The waiter queries only its exact `req_key`; it never scans unrelated Telegram
history. It exits on one of:

- `TELEGRAM_WON` plus the stored answer;
- `CHAT_WON` (no new answer to process; completion is an ignorable cleanup);
- `DISARMED` (owner returned; question remains visibly pending in chat);
- `CANCELLED`;
- process/backend shutdown.

If Telegram wins before the waiter reaches its first query, the durable claimed
row is already present and the waiter exits immediately. There is no lost-wake
window. Wait cost while idle is a sleeping local process — no model turns.

### 4.4 Telegram gateway pre-dispatch interceptor

The Telegram inbound pipeline invokes an away-bridge handler **after platform
authorization and owner identity resolution, but before session lookup,
message persistence, queueing, or agent dispatch**.

The handler is enabled only when `away_bridge.enabled=true` and a configured
owner chat/user match the inbound event. It recognizes:

- `A#<short-key> <answer>` — resolve the active request, attempt the Telegram
  claim transaction (§4.2), send success/late-duplicate feedback, and return
  `handled=true` so the normal Telegram DM session never receives the message.
- Exact normalized return intents while at least one session is armed:
  `I'm back`, `Im back`, or `חזרתי` — globally disarm all armed sessions,
  leave existing request rows durable, send one confirmation, and suppress the
  protocol message from normal dispatch.
- Any malformed, unknown, expired, cancelled, or non-owner key — return
  `handled=false`; it remains ordinary Telegram traffic (R7). Do not reveal
  whether a key exists to an unauthorized sender.

For a winning Telegram answer, the interceptor stores the answer in the broker
and sends a small Telegram confirmation such as "Accepted for Q#7K4M." The
originating waiter observes `TELEGRAM_WON`, exits, and the existing managed
process-completion path wakes exactly the original session.

For a late Telegram duplicate, the interceptor's idempotent ack insert decides
whether to send "Already answered in chat — ignored." It still returns
`handled=true`, so the ordinary DM agent cannot act on the duplicate.

### 4.5 Chat answer path

The skill records the active request key in the originating transcript. On the
owner's next chat message while a request is awaiting:

1. Treat that message as the proposed answer unless it is an explicit control
   command such as "I'm back."
2. Call `away-bridge claim --channel chat --request <id> --answer <text>` before
   acting on the answer.
3. If chat wins, continue using that answer. The waiter observes `CHAT_WON` and
   exits; its cleanup completion is ignored.
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
remains pending and the next heartbeat-free wait continues until the owner uses
the documented form or returns to the desk.

### 4.6 Disarm semantics

- **Chat "I'm back"** disarms only that originating session.
- **Telegram "I'm back"** disarms all armed sessions, because Telegram has no
  originating-session context and the statement represents global presence.
- Disarm changes `armed_sessions.status` and signals all waiters for affected
  sessions to exit `DISARMED`.
- Awaiting requests are not deleted. The originating session is woken by the
  waiter completion and reports: "You're back; this question is still waiting
  here." It does not silently consume a keyless Telegram message as an answer.
- Claimed answers remain immutable and process normally even if disarm races
  with their delivery. The claim transaction and disarm transaction serialize;
  whichever commits first determines the observable order, but neither loses
  the stored answer.

### 4.7 Restart and recovery

- **Gateway restart:** bridge rows remain in SQLite. The Telegram interceptor
  resumes with the gateway. Telegram updates queued by Telegram servers are
  handled on reconnect — while the gateway is down, answers are not in
  state.db; they wait at Telegram (correcting Rev 2's wording).
- **Desktop/TUI backend stays alive:** the managed waiter remains active and
  wakes its exact session on claim.
- **Desktop/TUI backend exits:** its waiter process is gone. The answer can still
  be claimed and stored by the Telegram gateway. When the owner resumes the
  originating session, the skill's first action is `away-bridge reconcile
  --session <id>`: process a stored Telegram winner (guarded by `delivered`),
  restart a missing waiter for an awaiting request, or report disarm/cancellation.
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
- Bridge answers are owner-authored user input, not trusted system instructions.
  Waiter completion wraps the answer in a fixed envelope and preserves it as
  user-role content.
- Request tokens use cryptographic randomness; the short display token must
  retain at least 80 bits or be backed by an unambiguous indexed prefix with
  collision rejection.
- Broker files use owner-only permissions.

## 5. State machine

```text
prepared --publish success--> awaiting --chat claim------> claimed(chat)
    |                           |       \--telegram claim-> claimed(telegram)
    |                           |\--disarm---------------> awaiting + waiter exits
    \--publish failure--------> cancelled
```

`claimed` is terminal and immutable. `cancelled` is terminal. Disarm changes
session arming state but does not rewrite a request's answer state.

## 6. Failure and concurrency cases

| Case | Required behavior |
|---|---|
| Chat and Telegram claim concurrently | One conditional update succeeds; the loser receives one idempotent acknowledgement. |
| Two armed sessions ask simultaneously | Unique keys, separate request rows, and separately owned waiter processes; no shared cursor or queue ownership ambiguity. |
| Telegram answer reaches gateway before waiter starts | Impossible after ordered prepare/start/publish. If an operator bypasses ordering, durable claim still lets a later waiter exit immediately. |
| Waiter exits unexpectedly while awaiting | Session sees process failure if backend is alive and restarts it once; reconciliation restarts it after session resume. Request remains durable. |
| Gateway is down | Outbound publish may still send through direct `hermes send`; inbound answer remains at Telegram until gateway reconnects. No claim occurs until then. |
| Telegram send fails | Request is cancelled, waiter exits, question remains chat-only. |
| Owner omits/typos key | Not bridge traffic; normal Telegram session handles it. Telegram question explicitly repeats the required reply format. Pending request remains awaiting. |
| Owner answers Telegram twice | First claims; second is intercepted, ignored, and receives at most one duplicate acknowledgement. |
| Owner answers both channels | First successful claim wins; other answer is ignored and acknowledged once. |
| Owner says Telegram "I'm back" with pending questions | All armed sessions disarm; waiters wake their sessions with "still waiting here." No answer is inferred. |
| Ordinary Telegram message while bridge active | Interceptor returns unhandled; normal Telegram DM behavior is unchanged. |
| Unauthorized sender guesses a valid key | Identity mismatch; no claim, no key-existence disclosure. |
| Origin session compressed | Process-notification ownership follows durable/compression lineage through existing Hermes routing; request remains bound to durable session identity. |

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
- **Separate Telegram bot** — technically clean but adds credentials and a
  second user conversation. Retained as fallback if the pre-dispatch
  interceptor proves unmaintainable.
- **`/handoff telegram`** — rejected because it moves the full conversation
  instead of relaying only input requests.

## 8. Implementation plan (after design approval)

1. **Broker and tests** — SQLite schema/migrations, prepare/publish/claim,
   duplicate ack, disarm, reconcile, and concurrency tests using two real
   SQLite connections.
2. **Managed waiter and exact-session wake tests** — waiter outcomes plus a
   Desktop/TUI integration test proving session A's completion never enters
   session B, including compression-lineage ownership.
3. **Telegram pre-dispatch interceptor** — owner authorization, valid-key claim,
   duplicate acknowledgement, global disarm, and proof that handled messages
   are neither persisted nor dispatched to the ordinary Telegram session.
4. **Skill** — natural-language arm/disarm behavior, ordered
   prepare/start/publish protocol, chat-side claims, and resume reconciliation.
5. **End-to-end acceptance** — two simultaneous desktop sessions; Telegram and
   chat races in both orders; late duplicates; ordinary Telegram traffic;
   gateway restart; desktop restart/reconcile; global Telegram disarm.

## 9. Acceptance gates

The design is implemented only when all are demonstrated against an isolated
Hermes home and real Telegram test chat:

1. Telegram-first resumes only the correct originating desktop session.
2. Chat-first prevents Telegram from changing the outcome.
3. A late answer on either channel produces exactly one small acknowledgement.
4. Two concurrent armed sessions never exchange answers or notifications.
5. A bridge reply never appears in ordinary Telegram session history and never
   invokes its agent.
6. Ordinary Telegram messages remain unaffected.
7. Gateway restart preserves claimability; desktop restart preserves durable
   answer recovery without double-processing.
8. Exact "I'm back" semantics match §4.6 in both channels.

## 10. Remaining product choices for owner approval

- Final user-visible wording for Telegram questions and duplicate acknowledgements.
- Whether the optional separate-bot fallback should remain documented or be
  removed after the interceptor prototype succeeds.

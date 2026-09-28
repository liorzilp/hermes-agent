# Away-Mode Telegram Bridge — Design Doc

**Status:** Rev 2 — requirements owner-confirmed 2026-09-28; design in review
loop (self-review findings F1–F6 + should-fix folded)
**Owner:** Lior (Telegram DM `1682802389`)
**Capability:** A per-session, explicitly-armed bridge that relays a session's
input requests to the owner's Telegram and accepts the first answer that
arrives, from either channel.
**Revision history:** Rev 1 (`e9c6a7146`, snapshot
`away-bridge-revs/REV-1.md`, sha256
`2224e8145f8afb55001c2088904baf13dcdb2518f240fb97736b2716885d272f`) —
initial owner-confirmed requirements; dedicated-bridge-session architecture.
Rev 2 folds the self-review: removes the orphan bridge session, specifies the
reply-consumption protocol, cuts the cron fallback, documents the
dual-consumption wart, adds chat-side claim + disarm-clears-heartbeat to the
session protocol, and corrects the status line.

---

## 1. Problem

Today the desktop chat and the Telegram DM are separate sessions by design
(gateway keys one session per platform/chat). When the owner steps away from
the desk mid-task, a session that needs input simply stalls in chat — the
owner never sees the question on the phone.

## 2. Requirements (owner-confirmed)

| # | Requirement | Decision |
|---|---|---|
| R1 | Scope | Input **requests only** (clarify, decision, approval). Regular chat, statuses, background reports stay on their own channels. |
| R2 | Arming | **Per-session, explicit only.** The owner must say "I'm stepping out / I won't be here" in that session. No standing default. |
| R3 | Lifetime | Until the owner says "I'm back" — in the session chat **or** from Telegram. No auto-expire. |
| R4 | First answer wins | Whichever channel's answer the session processes first becomes the answer of record. A late duplicate from the other channel is dropped with a **small one-line ack** ("already answered via <channel> — ignored"). **Ack scope: while armed only** — after disarm nobody polls, so a late duplicate arriving on the disarmed channel is silently ignored (it never reaches a live session). |
| R5 | Who may answer | Only the owner account in the bound Telegram DM (chat `1682802389`). Everyone else is invisible to the bridge. |
| R6 | Multi-session isolation | Keys are per-session; idle or unrelated sessions (including the normal Telegram DM session) never consume a bridged answer. Concurrent pending questions each match their own key. |
| R7 | A Telegram reply arriving while nothing is pending | Not touched by the bridge (normal Telegram session handles it). |

## 3. Non-goals

- No general cross-platform session mirroring or `/handoff`-style takeover.
- No bridging of background task notifications — those already deliver
  correctly (heartbeat / notify channels).
- No group-chat support. Bound channel = the owner's Telegram DM only.
- No suppression of the normal Telegram DM session's own reply to bridged
  traffic (see §4.5).

## 4. Architecture

The bridge is a tool plus a skill — there is no dedicated bridge session.
State lives in one small store owned by the bridge tool:

```
$HERMES_HOME/away-bridge/state.json   (default ~/.hermes/away-bridge/state.json)
```

```json
{
  "version": 2,
  "armed_sessions": {
    "<session_id>": { "armed_at": "<iso>", "last_seen_msg_id": 0 }
  },
  "pending": {
    "<req_key>": {
      "session_id": "<session_id>",
      "question": "<text>",
      "sent_at": "<iso>",
      "state": "awaiting | claimed | acked",
      "answered_channel": null,
      "answer": null,
      "answered_at": null
    }
  },
  "recent_acks": [ { "req_key": "...", "acked_at": "<iso>" } ]
}
```

`recent_acks` retains keys for 24h so a duplicate that arrives after the
session already consumed the answer can still be answered with the one-line
ack instead of being misrouted. `armed_sessions[].last_seen_msg_id` is the
per-session consumption watermark (see §4.4).

### 4.1 Components

1. **Bridge tool (`away_bridge.py`)** — a small Python module
   (`$HERMES_HOME/away-bridge/away_bridge.py`) exposing subcommands:
   `arm`, `disarm`, `ask`, `claim`, `ack`, `poll`, `status`. Every mutation
   is an atomic read-modify-write under a lockfile (`fcntl.flock`), so
   concurrent sessions cannot double-claim.

2. **Session-side protocol (skill `away-mode`)** — instructions the agent in
   any session follows when the owner arms away-mode:
   - On "I'm stepping out": run `away_bridge arm` (records this session_id),
     confirm, set the session heartbeat
     (`/heartbeat every 1m away-bridge poll`), and keep working
     autonomously under standing rules.
   - When input is needed: ask in-chat **and** run
     `away_bridge ask --session <id> --text "<question>"`, which registers a
     pending request (unique `req_key`), sends the question to Telegram via
     `hermes send -t telegram:1682802389` prefixed with the short key
     (e.g. `Q#k3f9c2 — <question> … reply with: A#k3f9c2 <answer>`), and
     writes the exact key mapping into the session transcript.
   - The session then **ends its turn** and relies on the wake path (§4.3)
     to receive the answer.
   - **Chat-side claim:** when the owner answers **in chat** before the
     heartbeat fires, the session runs
     `away_bridge claim --req <key> --channel chat --answer "<text>"` as its
     next action. First caller wins; if the claim returns `lost`, the
     session ignores the owner's chat answer, responds with the small ack
     line ("already answered via telegram — ignored"), and proceeds on the
     winning answer.
   - **Disarm:** on "I'm back" the session runs `away_bridge disarm` (drops
     the arm record) **and clears the heartbeat** (`/heartbeat clear`), then
     confirms. Disarm does NOT clear pending answers already received; they
     remain claimable by their session.

3. **Wake path** — a single consumer: the armed session's own heartbeat
   (`/heartbeat every 1m away-bridge poll`). The poll is an ordinary
   user-role turn, cache-safe, and gateway recovery restores heartbeats
   after restart, so no fallback watcher is needed. Cron cannot serve as a
   fallback wake path because cron deliveries are never mirrored into
   session history (documented gateway behavior) — a cron job cannot inject
   into a session. **"I'm back" from Telegram** is handled on the next
   heartbeat poll: the poll command matches the disarm pattern and runs
   `away_bridge disarm --self`, answers stay claimable for their sessions.
   An optional owner-nudge cron (§8) may inform the owner that a question is
   still waiting; it never claims, acks, or injects.

### 4.2 Wake latency budget

Armed → heartbeat (≤1m) → poll claims and processes. Owner answers in chat →
next session turn claims immediately. Expected answer-to-resume latency:
≤ 1 minute + one model turn. The heartbeat prompt is fixed and tiny
(`away-bridge poll`); a no-op poll produces a near-empty turn (one short
line of output), keeping per-poll cost at the cache-hit floor.

### 4.3 Reply consumption protocol (per-session watermark)

Replies are read from `state.db` (`source='telegram'`,
`chat_id='1682802389'`, `role='user'`), ordered by `timestamp` then `id`.

- Each armed session owns a watermark (`last_seen_msg_id`). A poll fetches
  all owner replies with `id > watermark`, **in id order**.
- A reply carrying `A#<key>` whose key maps to a pending request of *this*
  session → claim it (atomic; first caller wins) and stop scanning.
- A reply carrying `A#<key>` that maps to *another* session's request, an
  unknown key, or an acked key → **do not claim, do not ack, and do not
  advance the watermark past it**. Skip it in this poll only; it stays
  visible for the next poll of its rightful consumer. (Watermark advances
  only past replies the poll has fully handled or that are provably not
  addressed to any armed session's pending requests.)
- A reply without any `A#<key>` token → not bridge traffic; watermark
  advances past it.
- **"I'm back" pattern:** if a no-key reply matches the disarm intent, the
  poll runs `away_bridge disarm --self` and reports.
- **Ack idempotency:** the ack emission for a given (`req_key`, channel) is
  itself claim-once — recorded in `recent_acks` — so repeated polls never
  ack-spam the same duplicate.
- **Scan window cap:** a poll scans at most the last 200 unconsumed replies
  (bounded work per poll; normal traffic is ≪200).

### 4.4 Late-duplicate handling (R4)

- If a `claim` arrives after the request was already claimed on the other
  channel, `claim` returns `lost` plus the winning channel; the claiming
  session emits the small ack line to the *late* channel
  ("already answered via <winning channel> — ignored") once (idempotent,
  see §4.3 ack idempotency), and the request state moves to `acked`.
- After `acked`, further `A#<key>` replies are ignored silently — nobody is
  waiting on them.

### 4.5 Known wart: dual consumption on Telegram (documented, accepted)

The owner's Telegram DM is also a live Hermes session
(`agent:main:telegram:dm:1682802389`). Every owner DM — including an
`A#<key>` reply — is processed by that session as a normal turn; it may
reply to the answer with a confused response. The bridge cannot suppress
this without a platform-plugin interceptor, which is out of scope (§3,
non-goals). The session-side protocol accepts this echo; the away-mode skill
will phrase the Telegram question so the owner understands the DM session
may also chime in ("my main Telegram chat may also respond — ignore it").

### 4.6 Alternatives considered

- **`/handoff telegram`** — moves the whole conversation to Telegram
  (new forum topic, same session id). Wrong semantics: the owner wants
  *questions relayed* while the session stays put, not the conversation
  migrating. Rejected.
- **Gateway hooks (inbound-message event)** — no such sanctioned event
  exists (hook set: startup, session:start/end, agent:step/end, command:*).
  Rejected — no extension point.
- **Telegram platform-plugin interceptor** (suppress DM session for
  `A#*` traffic) — feasible in principle, but a core-adjacent custom fork
  of a bundled adapter; rejected for v1, revisit only if the §4.5 wart
  becomes unbearable.

## 5. Failure & edge cases

| Case | Behavior |
|---|---|
| Machine asleep / gateway down | Questions queue in Telegram; answers queue in state.db. On resume, gateway heartbeat recovery re-arms the poll and it catches up via watermark. No expiry while armed. |
| Owner answers Telegram twice | First `A#key` claims; second is `acked` (one ack line, idempotent). |
| Owner answers both channels | First processed wins; other channel gets the one ack line. |
| Two sessions pending simultaneously | Each question has its own key; each session's poll consumes only replies matching its own keys (watermark never advances past others' traffic). |
| Unrelated message arrives on Telegram while pending | No `A#key` token → watermark advances, nothing bridged. |
| Session dies mid-wait | `arm` record persists; on resume the agent re-arms its identity and re-checks pending requests for its session_id. |
| Telegram send fails | `ask` exits non-zero; session falls back to chat-only ask and says so. |
| Owner arms twice | Idempotent — `arm` refreshes `armed_at`. |
| Heartbeat fires while turn running | Idle-only firing; it defers to the next idle poll (documented heartbeat behavior). |
| Owner says "I'm back" in Telegram | Next poll disarms (`--self`), clears heartbeat, confirms; pending answers stay claimable. |
| `recent_acks` pruning | Entries older than 24h are dropped on each state write; state file stays small. |

## 6. Security & privacy

- Only `chat_id=1682802389` is ever read or messaged. Owner-only.
- The bridge store contains question text + keys only; no secrets.
- `hermes send` uses existing gateway credentials; no new secrets.
- The bridge reads state.db read-only (`?mode=ro`), same as the existing
  watcher.

## 7. Build plan (when approved)

1. `away_bridge.py` store + `arm/disarm/ask/claim/ack/poll/status` (TDD;
   atomicity tests incl. concurrent claim, watermark skip semantics, ack
   idempotency).
2. `away-mode` skill (session protocol, trigger phrases, wake setup,
   chat-side claim, disarm-clears-heartbeat, Telegram wording templates).
3. E2E dry-run: arm a test session, ask, answer from Telegram, verify claim
   + ack; answer duplicate, verify one ack; chat-side claim race; disarm.

## 8. Open items

- Heartbeat interval: 1m fast path is a default; owner may tune per arming
  ("bridge me, check every 5m").
- Wording templates (question prefix, ack line, §4.5 disclaimer) — finalized
  at build time.
- Optional owner-nudge cron: if a question sits unanswered > 30m, send the
  owner a single reminder (never claims/acks/injects). Off by default.

# Away-Mode Telegram Bridge — Design Doc

**Status:** Design — approved by owner 2026-09-28, awaiting build approval
**Owner:** Lior (Telegram DM `1682802389`)
**Capability:** A per-session, explicitly-armed bridge that relays a session's
input requests to the owner's Telegram and accepts the first answer that
arrives, from either channel.

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
| R4 | First answer wins | Whichever channel's answer the session processes first becomes the answer of record. A late duplicate from the other channel is dropped with a **small one-line ack** ("already answered via <channel> — ignored"). |
| R5 | Who may answer | Only the owner account in the bound Telegram DM (chat `1682802389`). Everyone else is invisible to the bridge. |
| R6 | Multi-session isolation | Keys are per-session; idle or unrelated sessions (including the normal Telegram DM session) never consume a bridged answer. Concurrent pending questions each match their own key. |
| R7 | A Telegram reply arriving while nothing is pending | Not touched by the bridge (normal Telegram session handles it). |

## 3. Non-goals

- No general cross-platform session mirroring or `/handoff`-style takeover.
- No bridging of background task notifications — those already deliver
  correctly (heartbeat / notify channels).
- No group-chat support. Bound channel = the owner's Telegram DM only.

## 4. Architecture

One dedicated Hermes session ("the away-bridge session") runs persistently in
the desktop app. All state lives in a single small store it owns:

```
~/.hermes/away-bridge/state.json
```

```json
{
  "version": 1,
  "armed_sessions": { "<session_id>": { "armed_at": "<iso>" } },
  "pending": {
    "<req_key>": {
      "session_id": "<session_id>",
      "question": "<text>",
      "sent_at": "<iso>",
      "channel": null,
      "answer": null,
      "answered_at": null
    }
  },
  "recent_acks": [ { "req_key": "...", "acked_at": "<iso>" } ]
}
```

`recent_acks` retains keys for 24h so a duplicate that arrives after the
session already consumed the answer can still be answered with the one-line
ack instead of being misrouted.

### 4.1 Components

1. **Bridge tool (`away_bridge.py`)** — a small Python module
   (`~/.hermes/away-bridge/away_bridge.py`) exposing subcommands:
   `arm`, `disarm`, `ask`, `claim`, `status`. Every mutation is an atomic
   read-modify-write under a lockfile (`fcntl.flock`), so concurrent desktop
   and watcher processes cannot double-claim.

2. **Session-side protocol (skill `away-mode`)** — instructions the agent in
   any session follows when the owner arms away-mode:
   - On "I'm stepping out": run `away_bridge arm` (records this session_id),
     confirm, and keep working; continue autonomously under standing rules.
   - When input is needed: ask in-chat **and** run
     `away_bridge ask --session <id> --text "<question>"`, which registers a
     pending request (unique `req_key`), sends the question to Telegram via
     `hermes send -t telegram:1682802389` prefixed with the short key
     (e.g. `Q#k3f9c2 — <question> … reply with: A#k3f9c2 <answer>`), and
     writes the exact key mapping into the session transcript.
   - The session then **ends its turn** (or starts a background wait) and
     relies on the wake path (4.2) to receive the answer.

3. **Wake path** — two independent consumers of owner Telegram traffic:
   - **In-session fast path:** when armed, the session sets a session
     heartbeat (`/heartbeat every 1m away-bridge poll`) whose prompt is
     `away_bridge poll` — claims any answer for this session and processes
     it in-context (cache-safe: ordinary user-role turn).
   - **Fallback watcher:** the existing 5-minute
     `telegram-owner-reply-watcher` cron pattern gains a second monitor
     script that fires only when output changes AND a matching pending
     request exists; it sends `hermes send`-formatted wake notice to the
     desktop chat. ( belt-and-suspenders; the heartbeat is primary. )
   - "I'm back" from Telegram matches the disarm pattern and runs
     `away_bridge disarm --all` — answers already pending stay claimable.

4. **Telegram reply handling (owner-only, key-verified):**
   - Replies are read from `state.db` exactly like the existing watcher
     (`source='telegram' AND chat_id='1682802389' AND role='user'`,
     last-occurrence wins).
   - A reply carrying `A#<key>` is claimable **only** by the session that
     registered that key. `claim` is atomic: first caller wins; the request
     moves to `channel='telegram'`, `answered_at` set.
   - A reply without a valid key (or for an expired/acked key): the bridge
     ignores it entirely (it belongs to the normal Telegram DM session).
   - **Late duplicate:** if the claim arrives after the request was already
     answered on the other channel, the bridge emits the small ack line to
     the *late* channel ("already answered via <other channel> — ignored")
     and marks the key acked so it is never re-processed.

5. **Disarm paths:** owner says "I'm back" (either channel) → disarm +
   confirmation line in the session chat. Disarm does NOT clear pending
   answers already received; they remain claimable by their session.

## 5. Failure & edge cases

| Case | Behavior |
|---|---|
| Bridge session not running (machine asleep) | Questions queue in Telegram; answers claimable when it resumes (`sent_at` retained; no expiry while armed). |
| Owner answers Telegram twice | First `A#key` claims; second gets the small ack. |
| Owner answers both channels | First processed wins; other channel gets the small ack. |
| Two sessions pending simultaneously | Each question has its own key; answers match their own session. |
| Unrelated message arrives on Telegram while pending | Ignored by the bridge (only `A#key` replies are bridged). |
| Session dies mid-wait | `arm` record persists; on session resume the agent re-arms its identity and re-checks pending requests for its session_id. |
| Telegram send fails | Bridge tool exits non-zero; session falls back to chat-only ask and says so. |
| Owner arms twice ("stepping out" again) | Idempotent — `arm` refreshes `armed_at`. |

## 6. Security & privacy

- Only `chat_id=1682802389` is ever read or messaged. Owner-only.
- The bridge store contains question text + keys only; no secrets.
- `hermes send` uses existing gateway credentials; no new secrets.

## 7. Build plan (when approved)

1. `away_bridge.py` store + `arm/disarm/ask/claim/status` (TDD, atomicity
   tests incl. concurrent claim).
2. `away-mode` skill (session protocol, trigger phrases, wake setup).
3. Watcher monitor variant `away-bridge-watcher.py` for the existing cron
   pattern (output-change-gated).
4. E2E dry-run: arm a test session, ask, answer from Telegram, verify claim
   + ack; answer duplicate, verify ack; disarm.

## 8. Open items

- Heartbeat interval: 1m fast path is a default; owner may tune per arming
  ("bridge me, check every 5m").
- Wording templates (question prefix, ack line) — finalized at build time.

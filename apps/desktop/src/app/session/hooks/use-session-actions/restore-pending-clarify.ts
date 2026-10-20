import { type GatewayEventPayload, QUESTION_CARD_TOOLS } from '@/lib/chat-messages'
import { $clarifyRequests, type ClarifyRequest, clearClarifyRequest } from '@/store/clarify'
import type { SessionResumeResult } from '@/types/hermes'

export interface PendingClarifyResumeState {
  authoritativeAbsent: boolean
  cleared: ClarifyRequest | null
  request: ClarifyRequest | null
}

/** The resume/activate snapshot shapes a live clarify can arrive in. */
type ClarifyResumeSnapshot = Pick<SessionResumeResult, 'open_requests' | 'pending_clarify' | 'prompts'>

/**
 * The snapshot's live clarify entry, whichever wire shape it arrived in:
 * `null` when the snapshot authoritatively lists no clarify.
 *
 * The legacy TUI gateway replays unanswered server→client requests as
 * `open_requests` rows (`{id, method}`). The canonical shared gateway instead
 * carries its live prompts as `prompts` / `pending_clarify` entries keyed by a
 * STABLE `prompt_id` and never sends `open_requests` — reading only the legacy
 * field read every canonical snapshot as "no clarify waiting", so a warm
 * session.activate cleared the answerable card the channel had just re-parked
 * under the same prompt_id, leaving a disabled preview over a backend prompt
 * that still blocks the turn (2026-10-19: prompts entry live, open_requests
 * null, running true, later inputs queued).
 *
 * A canonical clarify whose id cannot be read returns `{ id: null }`: the
 * backend does list a live clarify, so the snapshot is NOT authoritative-absent
 * — clearing a card the wait may still own would recreate the lost-card bug.
 */
function snapshotPendingClarify(response: ClarifyResumeSnapshot): { id: string | null } | null {
  const legacy = (response.open_requests ?? []).find(entry => QUESTION_CARD_TOOLS.has(entry.method))

  if (legacy) {
    return { id: legacy.id }
  }

  // `pending_clarify` is the canonical protocol's kind-projected prompt; the
  // raw `prompts` list is the un-projected fallback.
  const canonical = response.pending_clarify
    ?? (response.prompts ?? []).find(entry => entry?.kind === 'clarify')
    ?? null

  if (!canonical) {
    return null
  }

  const id =
    typeof canonical.prompt_id === 'string' && canonical.prompt_id
      ? canonical.prompt_id
      : typeof canonical.request_id === 'string' && canonical.request_id
        ? canonical.request_id
        : null

  return { id }
}

/**
 * Reconcile the parked clarify for `sessionId` against a resume/activate
 * snapshot.
 *
 * The snapshot names every request still blocking the session — legacy
 * `open_requests` rows, canonical `prompts` / `pending_clarify` entries (see
 * `snapshotPendingClarify`). The shared channel has ALREADY re-delivered those
 * to the request handlers (which parked the clarify card) before the caller
 * sees the response, so this only has to (a) report the parked request when
 * the snapshot confirms it and (b) treat a snapshot WITHOUT a clarify as
 * authoritative for requests that already existed when the RPC began — a
 * newer request that arrived while the response was in flight is left alone.
 */
export function restorePendingClarifyFromSnapshot(
  response: ClarifyResumeSnapshot,
  sessionId: string,
  resumeStartedAt: number,
  requestIdAtStart?: string
): PendingClarifyResumeState {
  const pending = snapshotPendingClarify(response)

  if (!pending) {
    const current = $clarifyRequests.get()[sessionId]

    const existedAtStart = Boolean(current && requestIdAtStart && current.requestId === requestIdAtStart)
    const definitelyOlder = Boolean(current?.receivedAt !== undefined && current.receivedAt < resumeStartedAt)
    const legacyWithoutTime = Boolean(current && current.receivedAt === undefined && !requestIdAtStart)

    if (current && (existedAtStart || definitelyOlder || legacyWithoutTime)) {
      clearClarifyRequest(current.requestId, sessionId)

      return { authoritativeAbsent: true, cleared: current, request: null }
    }

    return { authoritativeAbsent: true, cleared: null, request: null }
  }

  // The request handler parked it under this session when the channel
  // re-delivered the snapshot's prompts; a card the handler declined (empty
  // question) is simply not there.
  const parked = $clarifyRequests.get()[sessionId]

  return {
    authoritativeAbsent: false,
    cleared: null,
    request: pending.id && parked?.requestId === pending.id ? parked : null
  }
}

export function pendingClarifyToolPayload(request: ClarifyRequest): GatewayEventPayload {
  return {
    args: {
      questions: request.questions.map(question => ({
        choices: question.choices ?? undefined,
        multi_select: question.multiSelect || undefined,
        question: question.question
      }))
    },
    tool_id: request.requestId
  }
}

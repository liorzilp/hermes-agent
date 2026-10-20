import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as clarifyStore from '@/store/clarify'

const { setClarifyRequestMock, clearClarifyRequestMock } = vi.hoisted(() => ({
  clearClarifyRequestMock: vi.fn(),
  setClarifyRequestMock: vi.fn()
}))

vi.mock('@/store/clarify', async importOriginal => {
  const actual = await importOriginal<typeof clarifyStore>()

  return {
    ...actual,
    clearClarifyRequest: clearClarifyRequestMock,
    setClarifyRequest: setClarifyRequestMock
  }
})

import { $clarifyRequests } from '@/store/clarify'

import { pendingClarifyToolPayload, restorePendingClarifyFromSnapshot } from './restore-pending-clarify'

const resumeStartedAt = 1_700_000_000

describe('restorePendingClarifyFromSnapshot', () => {
  beforeEach(() => {
    clearClarifyRequestMock.mockClear()
    setClarifyRequestMock.mockClear()
    $clarifyRequests.set({})
  })

  it('hands back the card the request handler already parked for a replayed open clarify request', () => {
    $clarifyRequests.set({
      'sess-1': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Proceed?' }],
        receivedAt: resumeStartedAt + 5,
        requestId: 'rid1',
        sessionId: 'sess-1'
      }
    })

    const state = restorePendingClarifyFromSnapshot(
      {
        open_requests: [{ id: 'rid1', method: 'clarify', params: { questions: [{ qid: 'q0', question: 'Proceed?' }] } }]
      },
      'sess-1',
      resumeStartedAt
    )

    expect(state.authoritativeAbsent).toBe(false)
    expect(state.request?.requestId).toBe('rid1')
    expect(clearClarifyRequestMock).not.toHaveBeenCalled()
  })

  it('reports an open request the handler declined (no card parked) without inventing one', () => {
    const state = restorePendingClarifyFromSnapshot(
      { open_requests: [{ id: 'rid4', method: 'clarify', params: {} }] },
      'sess-4',
      resumeStartedAt
    )

    expect(state.authoritativeAbsent).toBe(false)
    expect(state.request).toBeNull()
    expect(setClarifyRequestMock).not.toHaveBeenCalled()
  })

  it('clears a stale local request when the snapshot has none, and leaves a newer in-flight one', () => {
    $clarifyRequests.set({
      'sess-6': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Old' }],
        receivedAt: resumeStartedAt - 10,
        requestId: 'old-rid',
        sessionId: 'sess-6'
      }
    })

    const cleared = restorePendingClarifyFromSnapshot({}, 'sess-6', resumeStartedAt, 'old-rid')

    expect(cleared.authoritativeAbsent).toBe(true)
    expect(cleared.cleared?.requestId).toBe('old-rid')
    expect(clearClarifyRequestMock).toHaveBeenCalledWith('old-rid', 'sess-6')

    $clarifyRequests.set({
      'sess-6': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Newer' }],
        receivedAt: resumeStartedAt + 1,
        requestId: 'new-rid',
        sessionId: 'sess-6'
      }
    })

    const kept = restorePendingClarifyFromSnapshot({}, 'sess-6', resumeStartedAt, 'old-rid')

    expect(kept.cleared).toBeNull()
    expect(kept.request).toBeNull()
    expect(clearClarifyRequestMock).toHaveBeenCalledTimes(1)
  })

  // The canonical shared gateway carries its live prompts as `prompts` /
  // `pending_clarify` keyed by a STABLE `prompt_id` — it never sends legacy
  // `open_requests`. Reading only `open_requests` read every canonical snapshot
  // as "no clarify waiting" and cleared the card the channel had just
  // re-parked, leaving a live backend prompt with a disabled preview card.
  it('keeps the parked card answerable when a canonical snapshot lists the prompt without open_requests', () => {
    $clarifyRequests.set({
      'sess-7': {
        questions: [{ choices: ['safe', 'fast'], multiSelect: false, qid: 'q0', question: 'Which path?' }],
        receivedAt: resumeStartedAt - 10,
        requestId: '1fd5d3fc05',
        sessionId: 'sess-7'
      }
    })

    const state = restorePendingClarifyFromSnapshot(
      {
        pending_clarify: {
          kind: 'clarify',
          prompt_id: '1fd5d3fc05',
          request_id: '1fd5d3fc05',
          execution_generation: 69,
          question: 'Which path?',
          choices: ['safe', 'fast'],
          multi_select: false,
          timeout_s: 3600
        }
      },
      'sess-7',
      resumeStartedAt,
      '1fd5d3fc05'
    )

    expect(state.authoritativeAbsent).toBe(false)
    expect(state.request?.requestId).toBe('1fd5d3fc05')
    expect(clearClarifyRequestMock).not.toHaveBeenCalled()
  })

  it('reads the raw canonical prompts list when the projected pending_clarify is absent', () => {
    $clarifyRequests.set({
      'sess-8': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Proceed?' }],
        receivedAt: resumeStartedAt - 5,
        requestId: 'p2',
        sessionId: 'sess-8'
      }
    })

    const state = restorePendingClarifyFromSnapshot(
      {
        prompts: [
          { kind: 'approval', prompt_id: 'a9', execution_generation: 3, command: 'build', choices: ['once'] },
          { kind: 'clarify', prompt_id: 'p2', execution_generation: 3, question: 'Proceed?' }
        ]
      },
      'sess-8',
      resumeStartedAt,
      'p2'
    )

    expect(state.authoritativeAbsent).toBe(false)
    expect(state.request?.requestId).toBe('p2')
    expect(clearClarifyRequestMock).not.toHaveBeenCalled()
  })

  it('still clears the card when a canonical snapshot lists no clarify prompt', () => {
    $clarifyRequests.set({
      'sess-9': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Done waiting?' }],
        receivedAt: resumeStartedAt - 10,
        requestId: 'p-settled',
        sessionId: 'sess-9'
      }
    })

    const state = restorePendingClarifyFromSnapshot(
      { prompts: [{ kind: 'approval', prompt_id: 'a9', execution_generation: 3, command: 'build', choices: ['once'] }] },
      'sess-9',
      resumeStartedAt,
      'p-settled'
    )

    expect(state.authoritativeAbsent).toBe(true)
    expect(state.cleared?.requestId).toBe('p-settled')
    expect(clearClarifyRequestMock).toHaveBeenCalledWith('p-settled', 'sess-9')
  })

  it('leaves the card alone when a canonical clarify prompt has no readable id', () => {
    // The backend lists a live clarify the reconcile cannot key: the snapshot
    // is NOT authoritative-absent, so the parked card must survive.
    $clarifyRequests.set({
      'sess-10': {
        questions: [{ choices: null, multiSelect: false, qid: 'q0', question: 'Proceed?' }],
        receivedAt: resumeStartedAt - 10,
        requestId: 'p-live',
        sessionId: 'sess-10'
      }
    })

    const state = restorePendingClarifyFromSnapshot(
      { pending_clarify: { kind: 'clarify', execution_generation: 3, question: 'Proceed?' } },
      'sess-10',
      resumeStartedAt,
      'p-live'
    )

    expect(state.authoritativeAbsent).toBe(false)
    expect(state.cleared).toBeNull()
    expect(clearClarifyRequestMock).not.toHaveBeenCalled()
  })
})

describe('pendingClarifyToolPayload', () => {
  it('mirrors the batch wire shape for in-place re-arm', () => {
    expect(
      pendingClarifyToolPayload({
        questions: [{ choices: ['Yes', 'No'], multiSelect: false, qid: 'q0', question: 'Proceed?' }],
        requestId: 'rid',
        sessionId: 'sess'
      })
    ).toEqual({
      args: {
        questions: [{ choices: ['Yes', 'No'], question: 'Proceed?' }]
      },
      tool_id: 'rid'
    })
  })
})

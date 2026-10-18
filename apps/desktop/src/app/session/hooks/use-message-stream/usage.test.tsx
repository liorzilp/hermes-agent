import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $currentUsage } from '@/store/session'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'
// $currentUsage mirrors the primary session; ClientSessionState.usage drives
// the same status bar when a secondary tile is focused.
const BASELINE = { calls: 2, compressions: 1, input: 500, output: 40, total: 540 }

let stream: MessageStreamHarness
let sessionStates = new Map<string, ClientSessionState>()

function mountStream() {
  stream = renderMessageStream(SID, { states: sessionStates })
}

describe('useMessageStream status-bar usage scoping', () => {
  beforeEach(() => {
    sessionStates = new Map([[SID, { ...createClientSessionState(), usage: { ...BASELINE } }]])
    $currentUsage.set({ ...BASELINE })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('merges a live session.usage tick from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { compressions: 2, context_percent: 42, input: 1200, total: 1280 } },
        session_id: SID,
        type: 'session.usage'
      })
    )

    // Merge, not replace: fields absent from the tick keep their prior values.
    expect($currentUsage.get()).toEqual({ ...BASELINE, compressions: 2, context_percent: 42, input: 1200, total: 1280 })
    expect(sessionStates.get(SID)?.usage).toEqual({
      ...BASELINE,
      compressions: 2,
      context_percent: 42,
      input: 1200,
      total: 1280
    })
  })

  it('caches a background session.usage tick without overwriting the primary status bar', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { compressions: 9, input: 9999, total: 9999 } },
        session_id: 'background-session',
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get('background-session')?.usage).toEqual({
      calls: 0,
      compressions: 9,
      input: 9999,
      output: 0,
      total: 9999
    })
  })

  it('caches authoritative session.info usage for a background tile', () => {
    sessionStates.set('background-session', { ...createClientSessionState(), usage: null })
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { calls: 3, compressions: 4, input: 700, output: 60, total: 760 } },
        session_id: 'background-session',
        type: 'session.info'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get('background-session')?.usage).toEqual({
      calls: 3,
      compressions: 4,
      input: 700,
      output: 60,
      total: 760
    })
  })

  it('clears a stale background compression count when session.info omits it', () => {
    sessionStates.set('background-session', {
      ...createClientSessionState(),
      usage: { ...BASELINE, compressions: 4, context_percent: 55 }
    })
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { calls: 3, input: 700, output: 60, total: 760 } },
        session_id: 'background-session',
        type: 'session.info'
      })
    )

    const usage = sessionStates.get('background-session')?.usage
    expect($currentUsage.get()).toEqual(BASELINE)
    expect(usage?.compressions).toBeUndefined()
    expect(usage?.context_percent).toBe(55)
    expect(usage?.calls).toBe(3)
  })

  it('applies message.complete usage from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { text: 'done', usage: { calls: 3, compressions: 3, input: 1500, output: 90, total: 1590 } },
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual({ calls: 3, compressions: 3, input: 1500, output: 90, total: 1590 })
  })

  it('ignores message.complete usage from a background session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { text: 'done', usage: { calls: 9, compressions: 9, input: 9999, output: 999, total: 9999 } },
        session_id: 'background-session',
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
  })

  // Wave-A2 local-viewer publishers (gateway/session_usage.py::publish_usage)
  // send session.usage ticks as the FLAT snake_case stats dict, not nested
  // under `usage`. Both generations must populate the same session state.
  it('merges a flat snake_case session.usage tick from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          api_calls: 12,
          cache_read_tokens: 1000,
          completion_tokens: 5343,
          compression_count: 1,
          context_length: 200000,
          context_source: 'provider_usage',
          context_tokens: 150000,
          input_tokens: 359745,
          last_prompt_tokens: 150000,
          output_tokens: 5343,
          prompt_tokens: 4000,
          tokens_per_s: 103.36,
          total_tokens: 365088
        },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual({
      ...BASELINE,
      avg_tps: 103.36,
      cache_hit_pct: 25,
      calls: 12,
      compressions: 1,
      context_max: 200000,
      context_percent: 75,
      context_source: 'provider_usage',
      context_used: 150000,
      input: 359745,
      output: 5343,
      total: 365088
    })
  })

  it('merges flat snake_case usage from message.complete', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          api_calls: 7,
          input_tokens: 2100,
          output_tokens: 140,
          text: 'done',
          total_tokens: 2240
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual({
      ...BASELINE,
      calls: 7,
      input: 2100,
      output: 140,
      total: 2240
    })
  })

  it('ignores a payload that carries neither usage shape', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { unrelated: true },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get(SID)?.usage).toEqual(BASELINE)
  })

  it('keeps accumulated counters when a flat tick omits them (merge, not replace)', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { total_tokens: 999 },
        session_id: SID,
        type: 'session.usage'
      })
    )

    // Only the field the tick actually carried moves; the rest keep prior values.
    expect($currentUsage.get()).toEqual({ ...BASELINE, total: 999 })
    expect(sessionStates.get(SID)?.usage).toEqual({ ...BASELINE, total: 999 })
  })
})

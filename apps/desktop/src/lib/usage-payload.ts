import type { UsageStats } from '@/types/hermes'

/**
 * Usage off a gateway event payload, in the app's `UsageStats` shape.
 *
 * Two wire shapes carry usage, and a desktop client meets both depending on
 * the gateway generation:
 *
 *  - Nested: `{ usage: { input, output, total, calls, ... } }` — `session.info`,
 *    `message.complete` (the gateway converts via `tui_usage`), and the legacy
 *    usage ticker emit this.
 *  - Flat snake_case: `{ input_tokens, output_tokens, total_tokens, api_calls,
 *    prompt_tokens, completion_tokens, context_tokens, context_length,
 *    context_source, compression_count, tokens_per_s, cache_read_tokens }` —
 *    the wave-A2 local-viewer publisher (`gateway/session_usage.py::
 *    publish_usage`) sends these verbatim as `session.usage` live ticks.
 *
 * Returns `null` when the payload carries neither shape, so callers can keep
 * their "no usage in this frame" branches unchanged.
 */
export function payloadUsage(payload: unknown): Partial<UsageStats> | null {
  if (!payload || typeof payload !== 'object') {
    return null
  }

  const source = payload as Record<string, unknown>

  const nested = source.usage
  if (nested && typeof nested === 'object') {
    return nested as Partial<UsageStats>
  }

  // Flat snake_case tick (gateway/session_usage.py::usage_stats). Guard on the
  // counter trio so a payload that merely shares a field name never matches.
  const hasFlatCounters =
    typeof source.input_tokens === 'number' ||
    typeof source.total_tokens === 'number' ||
    typeof source.api_calls === 'number'
  if (!hasFlatCounters) {
    return null
  }

  const num = (value: unknown): number | undefined =>
    typeof value === 'number' && Number.isFinite(value) ? value : undefined

  // Merge-not-replace: a key is only emitted when the tick actually carried
  // it, so a partial tick never resets an accumulated counter to zero. (The
  // gateway's usage_stats always emits all counters; this guards hypothetical
  // partial senders and future field retirements.)
  const usage: Partial<UsageStats> = {}

  const calls = num(source.api_calls)
  if (calls != null) {
    usage.calls = calls
  }
  const input = num(source.input_tokens) ?? num(source.prompt_tokens)
  if (input != null) {
    usage.input = input
  }
  const output = num(source.output_tokens) ?? num(source.completion_tokens)
  if (output != null) {
    usage.output = output
  }
  const total = num(source.total_tokens)
  if (total != null) {
    usage.total = total
  }

  const compressions = num(source.compression_count)
  if (compressions != null) {
    usage.compressions = compressions
  }
  if (typeof source.context_source === 'string') {
    usage.context_source = source.context_source
  }

  // Mirrors the gateway's own conversion (tui_usage): occupancy is
  // last_prompt_tokens when present, else context_tokens; always clamped.
  const contextMax = num(source.context_length)
  if (contextMax && contextMax > 0) {
    usage.context_max = contextMax
    const used = num(source.last_prompt_tokens) ?? num(source.context_tokens)
    if (used != null) {
      usage.context_used = Math.min(Math.max(used, 0), contextMax)
      usage.context_percent = Math.max(0, Math.min(100, (usage.context_used / contextMax) * 100))
    }
  }

  const promptTotal = num(source.prompt_tokens) ?? 0
  const cacheRead = num(source.cache_read_tokens) ?? 0
  if (promptTotal > 0 && cacheRead > 0) {
    usage.cache_hit_pct = Math.max(0, Math.min(100, Math.round((cacheRead / promptTotal) * 100)))
  }

  const tps = num(source.tokens_per_s)
  if (tps != null) {
    usage.avg_tps = tps
  }

  return usage
}

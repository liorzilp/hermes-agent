/**
 * E2E regression: a canonical clarify card must stay ANSWERABLE across a
 * gateway reconnect, and the blocked turn plus its queued follow-up must drain
 * once the clarification resolves.
 *
 * 2026-10-19 repro (orchestrator1910): on the canonical wire the
 * resume/activate snapshot carries live prompts as `prompts` / `pending_clarify`
 * and never sends legacy `open_requests`. `restorePendingClarifyFromSnapshot`
 * read only `open_requests`, decided "no clarify pending", and cleared the card
 * the channel had just re-parked under the same stable prompt_id. The field
 * trigger: the user waits a couple of minutes without answering, the gateway
 * WebSocket drops (network blip / sleep), the app auto-reconnects and
 * `useRouteResume` re-resumes the active session through the warm
 * `session.activate` path — which cleared the card. Symptom: the card stays
 * visible with disabled choices/confirm (args-preview fallback), the backend
 * prompts entry stays live, running stays true, later inputs stay queued, and
 * typed text is admitted as an ordinary queued input.
 *
 * Runs the real chain: composer → local canonical gateway → agent → clarify
 * tool → canonical prompt registration → renderer card, against the mock
 * inference server. The forced renderer-side WS drop (the gateway process and
 * its live prompt survive) exercises the reconnect → warm session.activate
 * path whose snapshot must keep the card answerable.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, type Page, test } from './test'

const TRIGGER = 'E2E_QUEUE_STOP_TRIGGER'
const QUESTION = 'Keep working?'
const TOOL_DONE_TEXT = 'The paused task completed.'
const FOLLOWUP = 'E2E_CLARIFY_DRAIN_FOLLOWUP'

// The sidebar "+" opens a NEW TAB beside the current chat: the prior session
// stays mounted in its own surface, so every read must use the LAST (active)
// surface (see correction-session-switch.spec.ts).
const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'

function activeSurface(page: Page) {
  return page.locator(SURFACE).last()
}

async function send(page: Page, text: string): Promise<void> {
  const composer = activeSurface(page).locator('[contenteditable="true"]').first()
  await composer.waitFor({ state: 'visible', timeout: 15_000 })
  await composer.click()
  await composer.type(text, { delay: 5 })
  await page.keyboard.press('Enter')
}

/** Queue a message behind the busy turn (Control+Enter) instead of answering
 * the parked clarify card — the path the 2026-10-19 report showed wedged. */
async function queue(page: Page, text: string): Promise<void> {
  const composer = activeSurface(page).locator('[contenteditable="true"]').first()
  await composer.click()
  await composer.type(text, { delay: 5 })
  await page.keyboard.press('Control+Enter')
}

/** The active surface's live clarify card, when one is mounted. */
function activeClarifyCard(page: Page) {
  return activeSurface(page).locator('form[data-clarify-batch]')
}

/** Switch to a fresh draft, then back to the clarify session: the return runs
 * the warm session.activate whose canonical snapshot still lists the live
 * clarify prompt — the reconcile step that lost answerability pre-fix. */
async function switchAwayAndBack(page: Page): Promise<void> {
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await page.waitForFunction(
    ([priorText, surfaceSelector]: [string, string]) => {
      const surfaces = document.querySelectorAll(surfaceSelector)
      const active = surfaces[surfaces.length - 1]
      const transcript = active?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''

      return surfaces.length > 0 && !transcript.includes(priorText)
    },
    [TRIGGER, SURFACE] as [string, string],
    { timeout: 15_000 }
  )

  // A still-running turn keeps the source prompt as the sidebar row title.
  const row = page.locator('[data-slot="sidebar"] button').filter({ hasText: TRIGGER }).first()
  await row.waitFor({ state: 'visible', timeout: 30_000 })
  await row.click()
}

async function waitForTranscriptText(page: Page, expected: string): Promise<void> {
  await page.waitForFunction(
    ([text, surfaceSelector]: [string, string]) => {
      const surfaces = document.querySelectorAll(surfaceSelector)
      const active = surfaces[surfaces.length - 1]

      return (active?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? '').includes(text)
    },
    [expected, SURFACE] as [string, string],
    { timeout: 60_000 }
  )
}

test.describe('canonical clarify card lifecycle', () => {
  let fixture: MockBackendFixture | null = null

  test.beforeEach(async () => {
    // Boot (sandboxed gateway + Electron) can exceed Playwright's default 90s
    // ceiling on a loaded machine; extend it for the hook + test.
    test.setTimeout(300_000)
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterEach(async () => {
    await fixture?.cleanup()
    fixture = null
  })

  test('stays answerable across a session switch; the answer completes the tool and drains the queue', async () => {
    const { mock, page } = fixture!

    // 1. Create the clarification: the real gateway registers a canonical
    //    prompt and the card mounts from the clarify.request event.
    await send(page, TRIGGER)

    const card = activeClarifyCard(page)
    await card.first().waitFor({ state: 'visible', timeout: 60_000 })
    await expect(card).toHaveCount(1)
    await expect(card.getByText(QUESTION)).toBeVisible()

    // 2. The card is ANSWERABLE: a choice button is clickable. (No pick is
    //    staged yet — the answer comes after the switch, like the field repro.)
    const yesButton = card.getByRole('button', { name: /Yes/ })
    await expect(yesButton).toBeEnabled()

    // 3. Queue a follow-up behind the blocked turn — the exact wedged state
    //    from the report (later inputs queued behind the clarify).
    await queue(page, FOLLOWUP)
    await expect(page.getByText('1 Queued')).toBeVisible()

    // 4. Switch sessions and back: the return runs the warm session.activate
    //    whose canonical snapshot (prompts / pending_clarify, never
    //    open_requests) still lists the live clarify prompt.
    await switchAwayAndBack(page)

    // 5. THE regression assertion: the card is STILL answerable after the
    //    switch — not a disabled args-preview over a live backend prompt.
    const returned = activeClarifyCard(page)
    await returned.first().waitFor({ state: 'visible', timeout: 30_000 })
    await expect(returned).toHaveCount(1)
    const returnedYes = returned.getByRole('button', { name: /Yes/ })
    // The reconnect cycle (drop → backoff → re-dial → warm activate) can take
    // a few seconds; the card itself never left the transcript.
    await expect(returnedYes).toBeEnabled({ timeout: 30_000 })

    // 6. Submit the answer: the clarify tool completes and the turn finishes.
    await returnedYes.click()
    await expect(returned.locator('button[type="submit"]')).toBeEnabled()
    await returned.locator('button[type="submit"]').click()

    await expect(activeSurface(page).locator('[data-clarify-settled]')).toBeVisible({ timeout: 30_000 })
    await waitForTranscriptText(page, TOOL_DONE_TEXT)

    // 7. The queued follow-up drains and executes once the turn completes.
    await expect
      .poll(() => mock.receivedPrompts.filter(prompt => prompt === FOLLOWUP), { timeout: 60_000 })
      .toHaveLength(1)
  })
})

test.describe('canonical clarify timeout', () => {
  let fixture: MockBackendFixture | null = null

  test.beforeEach(async () => {
    // Boot (sandboxed gateway + Electron) can exceed Playwright's default 90s
    // ceiling on a loaded machine; extend it for the hook + test.
    test.setTimeout(300_000)
    // Shortened clarify timeout so the wait ends within the test. Generous
    // enough that a loaded machine still delivers the prompt before it fires.
    fixture = await setupMockBackend({ extraConfig: 'clarify:\n  timeout: 25\n' })
    await waitForAppReady(fixture, 120_000)
  })

  test.afterEach(async () => {
    await fixture?.cleanup()
    fixture = null
  })

  test('times out, completes the blocked tool call, and drains the queued input', async () => {
    const { mock, page } = fixture!

    await send(page, TRIGGER)

    // A slow boot can let the shortened deadline fire before the prompt is
    // even delivered — accept either the live card or the already-settled one.
    const card = activeClarifyCard(page)
    const settled = activeSurface(page).locator('[data-clarify-settled]')
    await expect(card.first().or(settled.first())).toBeVisible({ timeout: 60_000 })

    if (await card.count()) {
      // A message queued behind the blocked turn must not be lost.
      await queue(page, FOLLOWUP)
      await expect(page.getByText('1 Queued')).toBeVisible()

      // The shortened timeout ends the wait: the card settles unanswered…
      await expect(settled).toBeVisible({ timeout: 45_000 })
    } else {
      // The deadline already fired pre-delivery; queue the follow-up now —
      // the turn is still running its post-timeout continuation.
      await queue(page, FOLLOWUP)
    }

    // …the blocked tool call completes and the turn finishes…
    await waitForTranscriptText(page, TOOL_DONE_TEXT)

    // …and the queued input drains and executes.
    await expect
      .poll(() => mock.receivedPrompts.filter(prompt => prompt === FOLLOWUP), { timeout: 90_000 })
      .toHaveLength(1)
  })
})

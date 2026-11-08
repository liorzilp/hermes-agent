"""Regression coverage: abandoned PTB polling apps must be force-killed (#139201).

When ``updater.stop()`` misses its deadline (CLOSE-WAIT socket wedged on epoll, or the
Updater lifecycle lock held by an abandoned stop), every recovery path drops the app
reference and rebuilds/replaces the adapter — but the abandoned app's polling task keeps
long-polling getUpdates as an *unregistered* receiver. It wins the polling race against
its own replacement (Telegram 409 conflicts keep the replacement unhealthy) and every
update it dispatches fails closed at admission (``capture_provenance`` →
``RuntimeStoreError('not_found')``) until process restart. These tests pin the hard-kill
armed at each abandonment site.
"""

import asyncio
from types import SimpleNamespace

import pytest
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))


def _make_app(polling_task, stop_event):
    """A minimal stand-in for a PTB Application plus its Updater, exposing the PTB 22.x
    name-mangled polling internals the kill relies on (same private-API reliance as
    ``bot._request``)."""
    shutdown_calls = []

    class _PollingRequest:
        async def shutdown(self):
            shutdown_calls.append(True)

    class _GeneralRequest:
        async def shutdown(self):
            shutdown_calls.append("general")  # must never be touched by the kill

    updater = SimpleNamespace(
        _running=True,
        running=True,
        _Updater__polling_task=polling_task,
        _Updater__polling_task_stop_event=stop_event,
        # stop() itself is what hung — record calls to prove the kill bypasses it.
        stop_calls=0,
    )

    async def _hung_stop():
        updater.stop_calls += 1
        await asyncio.Event().wait()  # never completes — the production hang

    updater.stop = _hung_stop

    bot = SimpleNamespace(_request=(_PollingRequest(), _GeneralRequest()))
    app = SimpleNamespace(updater=updater, bot=bot, shutdown_calls=shutdown_calls)
    return app


async def _never_ending_task():
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_kill_abandoned_polling_app_cancels_task_and_closes_polling_client():
    adapter = _make_adapter()
    stop_event = asyncio.Event()
    polling_task = asyncio.get_running_loop().create_task(_never_ending_task())
    app = _make_app(polling_task, stop_event)

    adapter._kill_abandoned_polling_app(app, "network-recovery")
    # The kill is detached: give the loop a bounded moment to run it.
    for _ in range(50):
        await asyncio.sleep(0)
        if polling_task.done() and stop_event.is_set():
            break

    assert polling_task.cancelled(), "abandoned polling task must be cancelled"
    assert stop_event.is_set(), "private stop event must be set"
    assert app.updater._running is False, "running gate must stop new polls"
    assert app.shutdown_calls == [True], "only the getUpdates client may be closed"
    assert app.updater.stop_calls == 0, "kill must not wait on the hung stop()"


@pytest.mark.asyncio
async def test_stop_updater_or_go_fatal_arms_kill_on_stop_timeout():
    adapter = _make_adapter()
    stop_event = asyncio.Event()
    polling_task = asyncio.get_running_loop().create_task(_never_ending_task())
    app = _make_app(polling_task, stop_event)

    async def _deadline_timeout(awaitable, *args, **kwargs):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()  # do not leak the never-completing stop() coroutine
        raise TimeoutError()

    fatal_calls = []

    async def _record_fatal(message, *log_args):
        fatal_calls.append(message)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", _deadline_timeout)
    monkeypatch.setattr(adapter, "_go_fatal_network", _record_fatal)
    try:
        assert await adapter._stop_updater_or_go_fatal(app, "network-recovery") is False
        assert fatal_calls, "the stop timeout must still go fatal for the supervisor rebuild"
        for _ in range(50):
            await asyncio.sleep(0)
            if polling_task.done():
                break
        assert polling_task.cancelled(), "the fatal-rebuild path must arm the hard kill"
        assert app.updater._running is False
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_kill_abandoned_polling_app_tolerates_missing_internals():
    adapter = _make_adapter()
    app = SimpleNamespace(updater=SimpleNamespace(running=True), bot=None)
    adapter._kill_abandoned_polling_app(app, "disconnect")  # must not raise
    adapter._kill_abandoned_polling_app(None, "disconnect")  # must not raise
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_kill_abandoned_polling_app_noop_on_stopped_updater():
    adapter = _make_adapter()
    stop_event = asyncio.Event()
    done_task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    await done_task
    app = _make_app(done_task, stop_event)
    app.shutdown_calls.clear()

    adapter._kill_abandoned_polling_app(app, "disconnect")
    for _ in range(10):
        await asyncio.sleep(0)

    # A finished task is left alone, but the gate still flips and the client still closes.
    assert not done_task.cancelled()
    assert app.updater._running is False
    assert app.shutdown_calls == [True]

"""Regression tests for the photo/media-group sibling of the #137545 replay.

A later enqueue must not cancel a flush that already popped its batch and is
mid-dispatch.  Cancelling there makes ``_flush_buffered`` re-hold the
already-dispatching event, and the hold queue later replays it (duplicate
delivery / re-processing) — the same shape as the text-batch replay fixed for
#137545, via the unguarded ``prior_task.cancel()`` in:

* ``_enqueue_photo_event``      (photo bursts)
* ``_queue_media_group_event``   (albums / media groups)
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SessionSource
from gateway.platforms.event import MessageEvent, MessageType


def _make_adapter():
    """Minimal TelegramAdapter for testing the delayed photo flush paths."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.platform = Platform.TELEGRAM
    adapter.config = config
    adapter._running = True
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._drop_delayed_deliveries = False
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    adapter._media_group_events = {}
    adapter._media_group_tasks = {}
    adapter._polling_error_task = None
    adapter._polling_heartbeat_task = None
    adapter._app = None
    adapter._bot = None
    adapter._set_status_indicator = AsyncMock()
    adapter._release_platform_lock = lambda: None
    adapter._text_batch_delay_seconds = 0.1
    adapter._media_batch_delay_seconds = 0
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._held_inbound_events = []
    adapter._held_inbound_redispatch_task = None
    adapter.HELD_INBOUND_MAX = 64
    return adapter


def _make_event(text: str, chat_id: str = "12345") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
    )


def _make_photo(text: str) -> MessageEvent:
    event = _make_event(text)
    event.media_urls = [f"url-{text}"]
    event.media_types = ["image"]
    return event


BURST_KEY = "agent:main:telegram:dm:12345:photo-burst"


class TestPhotoBurstReplay:
    @pytest.mark.asyncio
    async def test_burst_supersede_does_not_cancel_a_popped_photo_flush(self):
        """A later photo in the same burst must not cancel a flush that already
        popped its batch and is mid-dispatch (else the hold queue replays it)."""
        adapter = _make_adapter()
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered: list[str] = []

        async def _handle(event):
            delivered.append(event.text or "")
            if event.text == "burst one":
                entered.set()
                await release.wait()

        adapter.handle_message = _handle

        adapter._enqueue_photo_event(BURST_KEY, _make_photo("burst one"))
        first_flush = adapter._pending_photo_batch_tasks[BURST_KEY]
        await entered.wait()  # first flush popped its batch and is dispatching

        adapter._enqueue_photo_event(BURST_KEY, _make_photo("burst two"))
        second_flush = adapter._pending_photo_batch_tasks[BURST_KEY]
        release.set()
        try:
            await first_flush
        except asyncio.CancelledError:
            pass
        await second_flush

        assert not first_flush.cancelled(), "in-flight photo flush was cancelled mid-dispatch"
        assert adapter._held_inbound_events == [], "dispatching photo batch was re-held; hold queue replays it"
        assert delivered == ["burst one", "burst two"]


class TestMediaGroupReplay:
    @pytest.mark.asyncio
    async def test_album_item_supersede_does_not_cancel_a_popped_media_group_flush(self):
        """A later album item must not cancel a flush that already popped the
        album's merged event and is mid-dispatch (else the album is replayed)."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = _make_adapter()
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered: list[str] = []

        async def _handle(event):
            delivered.append(event.text or "")
            if event.text == "album one":
                entered.set()
                await release.wait()

        adapter.handle_message = _handle

        with patch.object(TelegramAdapter, "MEDIA_GROUP_WAIT_SECONDS", 0):
            await adapter._queue_media_group_event("mg-1", _make_photo("album one"))
            first_flush = adapter._media_group_tasks["mg-1"]
            await entered.wait()  # album flush popped its event and is dispatching

            await adapter._queue_media_group_event("mg-1", _make_photo("album two"))
            second_flush = adapter._media_group_tasks["mg-1"]
            release.set()
            try:
                await first_flush
            except asyncio.CancelledError:
                pass
            await second_flush

        assert not first_flush.cancelled(), "in-flight album flush was cancelled mid-dispatch"
        assert adapter._held_inbound_events == [], "dispatching album event was re-held; hold queue replays it"
        assert delivered == ["album one", "album two"]

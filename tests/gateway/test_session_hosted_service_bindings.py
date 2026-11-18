"""``CanonicalHostedRoomService.bindings``: owned-key discovery and the empty short-circuit.

The bindings call runs on every hosted-room supervisor poll cycle for every served
authority, so its store cost must stay index-shaped and vanish entirely when the
authority owns no rooms (the common install with zero hosted rooms).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

from gateway import hosted_rooms
from gateway.session_hosted_service import _OWNER, CanonicalHostedRoomService, _owned_room_ids


def _state_db(tmp_path: Path, keys: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO state_meta(key, value) VALUES (?, 'x')", [(key,) for key in keys])
    return conn


def test_owned_room_ids_finds_owner_keys_and_ignores_neighbours(tmp_path: Path):
    conn = _state_db(tmp_path, [
        _OWNER + "room-1",
        _OWNER + "room-2",
        "gateway.hosted.owner.v2:room-3",       # sibling version prefix: not ours
        "gateway.hosted.owner.v1",              # the prefix without its trailing colon
        "unrelated",
        "GATEWAY.HOSTED.OWNER.v1:room-4",       # GLOB is case-sensitive; writer keys are exact-case
    ])
    try:
        assert _owned_room_ids(conn) == {"room-1", "room-2"}
    finally:
        conn.close()


def test_owned_room_ids_empty_when_no_owner_keys(tmp_path: Path):
    conn = _state_db(tmp_path, ["unrelated", "gateway.hosted.owner.v2:x"])
    try:
        assert _owned_room_ids(conn) == set()
    finally:
        conn.close()


@contextmanager
def _yield_conn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    yield conn


class _StubService(CanonicalHostedRoomService):
    """Just enough of the canonical service to exercise bindings() in isolation."""

    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self.authority = SimpleNamespace(db=SimpleNamespace(
            db_path=db_path, _read_ctx=lambda: _yield_conn(conn)))
        self.db_path = db_path


def test_bindings_skips_the_store_round_trip_when_nothing_owned(tmp_path: Path, monkeypatch):
    conn = _state_db(tmp_path, ["unrelated"])

    def _must_not_run(*args, **kwargs):
        raise AssertionError("list_rooms must not run when the authority owns no rooms")

    monkeypatch.setattr(hosted_rooms, "list_rooms", _must_not_run)
    service = _StubService(conn, tmp_path / "state.db")
    assert service.bindings() == ()


def test_bindings_keeps_only_owned_rooms(tmp_path: Path, monkeypatch):
    conn = _state_db(tmp_path, [_OWNER + "room-1"])
    monkeypatch.setattr(hosted_rooms, "local_authority_gateway_id", lambda: "gateway-a")
    monkeypatch.setattr(hosted_rooms, "list_rooms", lambda db_path: [
        {"room_id": "room-1", "authority_gateway_id": "gateway-a", "authority_epoch": 1},
        {"room_id": "room-2", "authority_gateway_id": "gateway-a", "authority_epoch": 1},
    ])
    service = _StubService(conn, tmp_path / "state.db")
    assert [binding.room_id for binding in service.bindings()] == ["room-1"]

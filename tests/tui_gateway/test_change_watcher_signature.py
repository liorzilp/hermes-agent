"""Sessions-table content signature (``tui_gateway.change_watcher``).

The signature must (a) change iff a sessions-table value changes, (b) stay
silent for unrelated tables sharing the file (gateway heartbeats, #98005),
and (c) tolerate legacy column subsets and separator/quote-hostile content.
The 2026-11-18 rework computes the digest SQL-side (single aggregate row);
these tests pin the semantics that rework must preserve.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tui_gateway import change_watcher


_BASE_NS = 1_600_000_000_000_000_000  # settled (2020): never inside the racy window

_SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    display_name TEXT,
    message_count INTEGER,
    model TEXT,
    extra_col TEXT
);
CREATE TABLE gateway_heartbeats (
    id INTEGER PRIMARY KEY,
    ts REAL
);
"""


def _make_db(path: Path, schema: str = _SCHEMA) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(schema)
        con.commit()
    finally:
        con.close()


def _write(path: Path, sql: str, params: tuple = ()) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _bump(path: Path, step: int) -> None:
    """Force a settled mtime change so the signature cache must re-read."""
    ns = _BASE_NS + step * 1_000_000_000
    os.utime(path, ns=(ns, ns))


def _sig(path: Path):
    return change_watcher._session_db_content_sig(path)


def _assert_real_digest(signature) -> None:
    assert signature is not None
    fields, digest = signature
    assert fields != "mtime-fallback", f"expected a real digest, got {signature!r}"
    assert isinstance(digest, (bytes, bytearray)) and len(digest) == 16


def test_signature_stable_when_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, 'one', 3, 'm', 'x')")
    _bump(db, 1)
    first = _sig(db)
    _assert_real_digest(first)
    assert _sig(db) == first  # cache hit, and a re-read would match too


def test_signature_changes_on_signature_column_update(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, 'one', 3, 'm', 'x')")
    _bump(db, 1)
    before = _sig(db)
    _assert_real_digest(before)

    _write(db, "UPDATE sessions SET message_count = 4 WHERE id = 1")
    _bump(db, 2)
    after = _sig(db)
    _assert_real_digest(after)
    assert after != before


def test_signature_ignores_non_signature_columns(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, 'one', 3, 'm', 'x')")
    _bump(db, 1)
    before = _sig(db)

    _write(db, "UPDATE sessions SET extra_col = 'not-signature-relevant' WHERE id = 1")
    _bump(db, 2)
    after = _sig(db)
    _assert_real_digest(after)
    assert after == before


def test_signature_ignores_other_tables(tmp_path: Path) -> None:
    # gateway_heartbeats shares state.db and writes every minute (#98005):
    # those commits must not look like session changes.
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, 'one', 3, 'm', 'x')")
    _bump(db, 1)
    before = _sig(db)

    _write(db, "INSERT INTO gateway_heartbeats VALUES (1, 12345.678)")
    _bump(db, 2)
    after = _sig(db)
    _assert_real_digest(after)
    assert after == before


def test_signature_changes_on_insert_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, 'one', 3, 'm', 'x')")
    _bump(db, 1)
    one_row = _sig(db)

    _write(db, "INSERT INTO sessions VALUES (2, 'two', 1, 'm', 'x')")
    _bump(db, 2)
    two_rows = _sig(db)
    assert two_rows != one_row

    _write(db, "DELETE FROM sessions WHERE id = 2")
    _bump(db, 3)
    back_to_one = _sig(db)
    assert back_to_one == one_row  # deleting the insert restores the digest


def test_signature_handles_null_vs_literal_null(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions (id, display_name, model) VALUES (1, NULL, NULL)")
    _bump(db, 1)
    with_null = _sig(db)
    _assert_real_digest(with_null)

    _write(db, "UPDATE sessions SET model = 'NULL' WHERE id = 1")
    _bump(db, 2)
    with_literal = _sig(db)
    assert with_literal != with_null


def test_signature_survives_separator_and_quote_hostile_values(tmp_path: Path) -> None:
    # The encoding emits only typeof tags, X'…' blob literals and quote-wrapped
    # short strings; content separators, quotes and commas stay inside the
    # blob hex and cannot forge a collision.
    hostile_a = tmp_path / "hostile-a.db"
    hostile_b = tmp_path / "hostile-b.db"
    row = (
        1,
        "name with 'quotes', commas, and \x1e\x1f control chars",
        7,
        "m\x1fo\x1en",
        None,
    )
    for db in (hostile_a, hostile_b):
        _make_db(db)
        _write(db, "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", row)
        _bump(db, 1)
    assert _sig(hostile_a) == _sig(hostile_b)  # identical data, identical digest

    _write(hostile_b, "INSERT INTO sessions VALUES (2, 'a', 1, 'm', NULL)")
    _bump(hostile_b, 2)
    assert _sig(hostile_b) != _sig(hostile_a)  # extra row changes it


def test_signature_row_split_disambiguation(tmp_path: Path) -> None:
    # (x, sep-in-y) vs (x-with-sep, y): different data must not collide.
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _make_db(left)
    _make_db(right)
    _write(left, "INSERT INTO sessions VALUES (1, 'a', 1, 'b', NULL)")
    _write(right, "INSERT INTO sessions VALUES (1, 'a', 1, 'b', NULL)")
    _write(right, "INSERT INTO sessions VALUES (2, ?, 1, 'c', NULL)", ("a\x1fb",))
    _bump(left, 1)
    _bump(right, 1)
    assert _sig(left) != _sig(right)


def test_signature_legacy_column_subset(tmp_path: Path) -> None:
    legacy = tmp_path / "state.db"
    con = sqlite3.connect(legacy)
    try:
        con.executescript(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY, display_name TEXT);"
        )
        con.commit()
    finally:
        con.close()
    _write(legacy, "INSERT INTO sessions VALUES (1, 'old-schema')")
    _bump(legacy, 1)
    before = _sig(legacy)
    _assert_real_digest(before)
    assert before is not None
    assert before[0] == ("id", "display_name")  # subset actually used

    _write(legacy, "UPDATE sessions SET display_name = 'renamed' WHERE id = 1")
    _bump(legacy, 2)
    assert _sig(legacy) != before


def test_signature_falls_back_when_no_sessions_table(tmp_path: Path) -> None:
    foreign = tmp_path / "state.db"
    con = sqlite3.connect(foreign)
    try:
        con.executescript("CREATE TABLE unrelated (id INTEGER PRIMARY KEY);")
        con.commit()
    finally:
        con.close()
    _bump(foreign, 1)
    signature = _sig(foreign)
    assert signature is not None
    assert signature[0] == "mtime-fallback"


def test_signature_missing_file_returns_none(tmp_path: Path) -> None:
    assert _sig(tmp_path / "absent.db") is None


def test_signature_distinguishes_embedded_nul_values(tmp_path: Path) -> None:
    # 2026-11-19 review gate: SQLite quote(TEXT) truncates at the first
    # embedded NUL, so 'a\x00x' and 'a\x00y' produced identical digests under
    # the quote()-only encoding. The blob-quoting encoding is length-based and
    # must distinguish them.
    left = tmp_path / "nul-left.db"
    right = tmp_path / "nul-right.db"
    _make_db(left)
    _make_db(right)
    _write(left, "INSERT INTO sessions VALUES (1, ?, 1, 'm', NULL)", ("a\x00x",))
    _write(right, "INSERT INTO sessions VALUES (1, ?, 1, 'm', NULL)", ("a\x00y",))
    _bump(left, 1)
    _bump(right, 1)
    lsig, rsig = _sig(left), _sig(right)
    _assert_real_digest(lsig)
    _assert_real_digest(rsig)
    assert lsig != rsig


def test_signature_detects_change_after_embedded_nul(tmp_path: Path) -> None:
    # The invisible-change class the review reproduced: an update whose only
    # difference sits after an embedded NUL must still change the digest.
    db = tmp_path / "state.db"
    _make_db(db)
    _write(db, "INSERT INTO sessions VALUES (1, ?, 1, 'm', NULL)", ("a\x00x",))
    _bump(db, 1)
    before = _sig(db)
    _assert_real_digest(before)

    _write(db, "UPDATE sessions SET display_name = ? WHERE id = 1", ("a\x00xy",))
    _bump(db, 2)
    assert _sig(db) != before


def test_signature_distinguishes_storage_type(tmp_path: Path) -> None:
    # typeof() tagging preserves the old repr(tuple(row)) distinction between
    # TEXT '42' and INTEGER 42. A declared-TEXT column converts numbers to
    # text at write time (affinity), so the distinction only persists in a
    # no-affinity column — the legacy-store shape this guards.
    schema = """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        display_name,
        message_count INTEGER,
        model TEXT,
        extra_col TEXT
    );
    CREATE TABLE gateway_heartbeats (
        id INTEGER PRIMARY KEY,
        ts REAL
    );
    """
    left = tmp_path / "typed-left.db"
    right = tmp_path / "typed-right.db"
    _make_db(left, schema)
    _make_db(right, schema)
    _write(left, "INSERT INTO sessions VALUES (1, ?, 1, 'm', NULL)", ("42",))
    _write(right, "INSERT INTO sessions VALUES (1, ?, 1, 'm', NULL)", (42,))
    _bump(left, 1)
    _bump(right, 1)
    lsig, rsig = _sig(left), _sig(right)
    _assert_real_digest(lsig)
    _assert_real_digest(rsig)
    assert lsig != rsig


@pytest.mark.parametrize("rows", [0, 1, 250])
def test_signature_scales_without_row_loop(rows: int, tmp_path: Path) -> None:
    # 250-row smoke: bulk correctness (deep group_concat) with one update
    # detected in the middle of the table.
    db = tmp_path / "state.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)",
            [(i, f"s{i}", i % 7, "m") for i in range(1, rows + 1)],
        )
        con.commit()
    finally:
        con.close()
    _bump(db, 1)
    before = _sig(db)
    _assert_real_digest(before)

    if rows:
        _write(db, "UPDATE sessions SET message_count = 999 WHERE id = ?", (rows // 2 or 1,))
        _bump(db, 2)
        assert _sig(db) != before

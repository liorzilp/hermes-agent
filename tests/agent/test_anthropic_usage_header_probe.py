"""Anthropic usage via /v1/messages response headers (the unified probe).

The OAuth usage endpoint is throttled independently of /v1/messages; the
probe reads the account's LIVE unified rate-limit headers off one tiny
real reply instead. These tests pin the probe's contract:
- a 2xx or 429 reply carrying >=1 unified window is a successful read
  (429 IS quota-exhaustion, the reported state itself);
- any other status, absent/unparseable headers, or a transport failure
  returns None so the fetcher falls back to the endpoint unchanged;
- a window is emitted only when BOTH -utilization and -reset parse;
  non-finite or negative utilization drops the window; >1.0 passes (overage);
- window labels match the endpoint path's ("Current session"/"Current week")
  so caches and alerts stay continuous across the source switch.

The header fixture is a LIVE capture (2026-11-17, owner account, HTTP 200
from POST /v1/messages with anthropic-beta: oauth-2025-04-20).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from agent import account_usage

_LIVE_HEADERS = {
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-overage-disabled-reason": "out_of_credits",
    "anthropic-ratelimit-unified-overage-status": "rejected",
    "anthropic-ratelimit-unified-reset": "1794919200",
    "anthropic-ratelimit-unified-5h-utilization": "0.85",
    "anthropic-ratelimit-unified-7d-reset": "1795147200",
    "anthropic-ratelimit-unified-5h-reset": "1794919200",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
    "anthropic-ratelimit-unified-fallback": "available",
    "anthropic-ratelimit-unified-7d-utilization": "0.26",
    "anthropic-ratelimit-unified-status": "allowed",
}


def _probe_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


class _CannedClient:
    """One canned response per client; records every POST's request shape."""

    calls: list[httpx.Response] = []
    seen: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        _CannedClient.seen.append({"url": url, "headers": headers, "json": json})
        return _CannedClient.calls.pop(0)


def _canned(status, headers=None):
    _CannedClient.calls = [httpx.Response(status, headers=headers or {}, request=_probe_request())]
    _CannedClient.seen = []
    return _CannedClient


# --- parser -----------------------------------------------------------------

def test_parser_two_windows_from_live_fixture():
    parsed = account_usage._parse_anthropic_unified_headers(_LIVE_HEADERS)
    assert parsed is not None
    windows, details = parsed
    assert [w.label for w in windows] == ["Current session", "Current week"]
    assert [w.used_percent for w in windows] == [85.0, 26.0]
    assert [w.reset_at for w in windows] == [
        datetime(2026, 11, 17, 12, 40, tzinfo=UTC),
        datetime(2026, 11, 20, 4, 0, tzinfo=UTC),
    ]
    assert all(w.scope == "account" for w in windows)
    assert details == []  # healthy status earns no detail line


def test_parser_case_insensitive_lookup():
    upper = {k.upper(): v for k, v in _LIVE_HEADERS.items()}
    assert account_usage._parse_anthropic_unified_headers(upper) is not None


def test_parser_window_missing_either_field_is_omitted():
    one = dict(_LIVE_HEADERS)
    one.pop("anthropic-ratelimit-unified-7d-utilization")
    parsed = account_usage._parse_anthropic_unified_headers(one)
    assert parsed is not None
    assert [w.label for w in parsed[0]] == ["Current session"]


def test_parser_no_windows_returns_none():
    assert account_usage._parse_anthropic_unified_headers(
        {"anthropic-ratelimit-unified-status": "allowed"}) is None


def test_parser_junk_reset_drops_that_window_only():
    junk = dict(_LIVE_HEADERS)
    junk["anthropic-ratelimit-unified-5h-reset"] = "not-a-number"
    parsed = account_usage._parse_anthropic_unified_headers(junk)
    assert parsed is not None
    assert [w.label for w in parsed[0]] == ["Current week"]


@pytest.mark.parametrize("bad", ["nan", "inf", "-0.05"])
def test_parser_nonfinite_or_negative_utilization_drops_window(bad):
    headers = dict(_LIVE_HEADERS)
    headers["anthropic-ratelimit-unified-5h-utilization"] = bad
    parsed = account_usage._parse_anthropic_unified_headers(headers)
    assert parsed is not None
    assert [w.label for w in parsed[0]] == ["Current week"]


def test_parser_overage_utilization_above_one_passes():
    headers = dict(_LIVE_HEADERS)
    headers["anthropic-ratelimit-unified-5h-utilization"] = "1.4"
    parsed = account_usage._parse_anthropic_unified_headers(headers)
    assert parsed is not None
    assert parsed[0][0].used_percent == 140.0


def test_parser_huge_finite_utilization_drops_window():
    # 1e308 is finite but overflows the x100 scaling to inf — the scaled
    # percent must be validated too, or the snapshot carries inf.
    headers = dict(_LIVE_HEADERS)
    headers["anthropic-ratelimit-unified-5h-utilization"] = "1e308"
    parsed = account_usage._parse_anthropic_unified_headers(headers)
    assert parsed is not None
    assert [w.label for w in parsed[0]] == ["Current week"]


def test_probe_request_shape_and_single_request(monkeypatch):
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(200, _LIVE_HEADERS))
    account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t")
    assert len(_CannedClient.seen) == 1  # exactly one probe request, never more
    req = _CannedClient.seen[0]
    # Literals pinned, NOT the production constants — a wrong change to the
    # URL or body in production code must turn this test red.
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["json"] == {"model": "claude-haiku-4-5", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}
    h = req["headers"]
    assert h["Authorization"] == "Bearer sk-ant-oat01-t"
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["User-Agent"] == "claude-code/2.1.0"
    assert h["Content-Type"] == "application/json"


def test_parser_degraded_status_earns_one_detail_line():
    headers = dict(_LIVE_HEADERS)
    headers["anthropic-ratelimit-unified-status"] = "rejected"
    parsed = account_usage._parse_anthropic_unified_headers(headers)
    assert parsed is not None
    assert len(parsed[1]) == 1
    assert "rejected" in parsed[1][0]
    assert "five hour" in parsed[1][0]  # representative claim decoded
    assert "fallback available" in parsed[1][0]


# --- probe -------------------------------------------------------------------

def test_probe_success_on_200_with_headers(monkeypatch):
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(200, _LIVE_HEADERS))
    parsed = account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t")
    assert parsed is not None
    assert [w.label for w in parsed[0]] == ["Current session", "Current week"]


def test_probe_success_on_429_with_headers(monkeypatch):
    # THE inversion: a 429 carrying unified headers is a successful read —
    # quota-exhaustion is the reported state itself.
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(429, _LIVE_HEADERS))
    parsed = account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t")
    assert parsed is not None


def test_probe_miss_on_other_status_with_headers(monkeypatch):
    for status in (401, 404, 503, 100):
        monkeypatch.setattr(account_usage.httpx, "Client", _canned(status, _LIVE_HEADERS))
        assert account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t") is None


def test_probe_miss_on_429_without_headers(monkeypatch):
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(429, {"retry-after": "3600"}))
    assert account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t") is None


def test_probe_miss_on_transport_failure(monkeypatch):
    class _DeadClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(account_usage.httpx, "Client", _DeadClient)
    assert account_usage._anthropic_usage_via_message_probe("sk-ant-oat01-t") is None


# --- fetcher integration -------------------------------------------------------

def _gate_exempt(monkeypatch):
    import agent.account_usage_cache as auc
    monkeypatch.setattr(auc, "gate_exempt", lambda: True)


def _oauth_token(monkeypatch):
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-t")


_ENDPOINT_PAYLOAD = {
    "five_hour": {"utilization": 0.42, "resets_at": "2026-11-17T12:40:00+00:00"},
    "seven_day": {"utilization": 0.26, "resets_at": "2026-11-20T04:00:00+00:00"},
    "seven_day_opus": {"utilization": 0.61, "resets_at": "2026-11-19T04:00:00+00:00"},
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": True, "used_credits": 1.5, "monthly_limit": 100.0, "currency": "USD"},
}


def test_fetcher_probe_wins_and_endpoint_is_not_called(monkeypatch):
    _gate_exempt(monkeypatch)
    _oauth_token(monkeypatch)
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(200, _LIVE_HEADERS))

    def _no_endpoint(*a, **k):
        raise AssertionError("endpoint must not be called when the probe wins")

    monkeypatch.setattr(account_usage, "_get_json", _no_endpoint)
    snapshot = account_usage._fetch_anthropic_account_usage()
    assert snapshot is not None
    assert snapshot.source == "messages_header_probe"
    assert snapshot.available
    assert [w.label for w in snapshot.windows] == ["Current session", "Current week"]
    assert [w.used_percent for w in snapshot.windows] == [85.0, 26.0]


def test_fetcher_probe_miss_falls_back_to_endpoint(monkeypatch):
    _gate_exempt(monkeypatch)
    _oauth_token(monkeypatch)
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(404, _LIVE_HEADERS))
    calls = {"n": 0}

    def _endpoint(url, headers, *, timeout):
        calls["n"] += 1
        return _ENDPOINT_PAYLOAD

    monkeypatch.setattr(account_usage, "_get_json", _endpoint)
    snapshot = account_usage._fetch_anthropic_account_usage()
    assert snapshot is not None
    assert calls["n"] == 1
    assert snapshot.source == "oauth_usage_api"
    labels = [w.label for w in snapshot.windows]
    assert labels == ["Current session", "Current week", "Opus week"]
    assert any("Extra usage" in d for d in snapshot.details)
    # Scale continuity across the source switch: a 0.42 utilization reads
    # as 42 on the endpoint path exactly as it would on the probe path.
    assert snapshot.windows[0].used_percent == pytest.approx(42.0)


def test_fetcher_probe_transport_failure_falls_back_to_endpoint(monkeypatch):
    _gate_exempt(monkeypatch)
    _oauth_token(monkeypatch)

    class _DeadClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(account_usage.httpx, "Client", _DeadClient)
    monkeypatch.setattr(account_usage, "_get_json",
                        lambda url, headers, *, timeout: _ENDPOINT_PAYLOAD)
    snapshot = account_usage._fetch_anthropic_account_usage()
    assert snapshot is not None
    assert snapshot.source == "oauth_usage_api"
    assert [w.label for w in snapshot.windows] == [
        "Current session", "Current week", "Opus week"]


def test_fetcher_probe_wins_on_429_when_account_capped(monkeypatch):
    _gate_exempt(monkeypatch)
    _oauth_token(monkeypatch)
    headers = dict(_LIVE_HEADERS)
    headers["anthropic-ratelimit-unified-5h-utilization"] = "1.0"
    headers["anthropic-ratelimit-unified-status"] = "rejected"
    monkeypatch.setattr(account_usage.httpx, "Client", _canned(429, headers))
    snapshot = account_usage._fetch_anthropic_account_usage()
    assert snapshot is not None
    assert snapshot.source == "messages_header_probe"
    assert snapshot.windows[0].used_percent == 100.0
    assert any("rejected" in d for d in snapshot.details)

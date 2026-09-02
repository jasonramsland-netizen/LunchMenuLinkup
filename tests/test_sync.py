from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from src.sync_menu import (
    MenuDay,
    BASE_API_ENDPOINT,
    HEALTH_E_PRO_API_ROOT,
    SyncError,
    _fold_ical_line,
    _format_dtstamp,
    _month_starts,
    _parse_date,
    _parse_json_object,
    _records_from_payload,
    _request_json,
    calculate_item_frequencies,
    extract_menu_days,
    fetch_menu_payload,
    filter_repeating_items,
    generate_ical,
    main,
    write_calendar_atomically,
)


def _days() -> list[MenuDay]:
    return [
        MenuDay(date(2026, 9, 1), ("Tacos", "1% White Milk", "Apple")),
        MenuDay(date(2026, 9, 2), ("Pizza", "1% White Milk", "Apple")),
        MenuDay(date(2026, 9, 3), ("Chicken", "1% White Milk", "Carrots")),
        MenuDay(date(2026, 9, 4), ("Pasta", "1% White Milk", "Apple")),
    ]


def test_frequency_filter_strips_items_at_or_above_70_percent() -> None:
    days = _days()

    frequencies = calculate_item_frequencies(days)
    assert frequencies["1% White Milk"] == 1.0
    assert frequencies["Apple"] == 0.75
    assert frequencies["Tacos"] == 0.25

    filtered = filter_repeating_items(days)
    assert filtered[0].items == ("Tacos",)
    assert filtered[1].items == ("Pizza",)
    assert filtered[2].items == ("Chicken", "Carrots")
    assert filtered[3].items == ("Pasta",)


def test_exact_blacklist_removes_item_below_frequency_threshold() -> None:
    days = [
        MenuDay(date(2026, 9, 1), ("Tacos", "Condiment Packet")),
        MenuDay(date(2026, 9, 2), ("Pizza",)),
        MenuDay(date(2026, 9, 3), ("Chicken",)),
    ]

    filtered = filter_repeating_items(days)
    assert filtered[0].items == ("Tacos",)


def test_empty_frequency_input_and_blank_blacklist_are_safe() -> None:
    assert calculate_item_frequencies([]) == {}
    assert filter_repeating_items([], blacklist=[None, "  "]) == []


def test_extract_menu_days_skips_categories_days_off_and_past_days() -> None:
    payload = {
        "data": [
            {
                "day": "2026-08-31",
                "setting": '{"current_display":[{"type":"recipe","name":"Past"}]}',
            },
            {
                "day": "2026-09-01",
                "setting": '{"current_display":['
                '{"type":"category","name":"Lunch Entree"},'
                '{"type":"recipe","name":"Tacos"},'
                '{"type":"recipe","recipe_name":"Apple"}'
                '],"days_off":[]}',
            },
            {
                "day": "2026-09-02",
                "setting": '{"current_display":[],"days_off":{"status":1,"description":"Holiday"}}',
            },
        ]
    }

    days = extract_menu_days(payload, start_date=date(2026, 9, 1))
    assert days == [MenuDay(date(2026, 9, 1), ("Tacos", "Apple"))]


def test_extract_menu_days_supports_nested_and_older_shapes() -> None:
    payload = {
        "data": {
            "days": [
                {
                    "date": date(2026, 9, 1),
                    "current_display": [
                        "Fresh Fruit",
                        {"recipe_name": "Tacos"},
                        {"type": "item", "title": "Apple"},
                        {"type": "category", "name": "Fruit"},
                        {"name": "Not a recipe"},
                        {"type": "recipe"},
                        {"type": "recipe", "name": "Tacos"},
                        object(),
                    ],
                },
                {
                    "day": "not-a-date",
                    "current_display": [{"type": "recipe", "name": "Skip"}],
                },
                {
                    "day": "2026-09-02",
                    "setting": {
                        "current_display": [{"type": "recipe", "name": "Skip"}],
                        "days_off": {"description": "Teacher workday"},
                    },
                },
            ]
        }
    }

    days = extract_menu_days(payload)
    assert days == [
        MenuDay(date(2026, 9, 1), ("Fresh Fruit", "Tacos", "Apple"))
    ]


def test_parsing_helpers_reject_malformed_values() -> None:
    assert _parse_date(datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)) == date(
        2026, 9, 1
    )
    assert _parse_date(date(2026, 9, 1)) == date(2026, 9, 1)
    assert _parse_date("not-a-date") is None
    assert _parse_date(1234) is None
    assert _parse_json_object({"current_display": []}, field_name="setting") == {
        "current_display": []
    }

    with pytest.raises(SyncError, match="Invalid JSON"):
        _parse_json_object("{", field_name="setting")
    with pytest.raises(SyncError, match="no usable"):
        _parse_json_object([], field_name="setting")

    with pytest.raises(SyncError, match="no setting"):
        extract_menu_days(
            {"data": [{"day": "2026-09-01", "other": "value"}]}
        )
    assert extract_menu_days(
        {"data": [{"day": "2026-09-03", "setting": {}}]}
    ) == []


def test_payload_helpers_reject_empty_or_wrong_shapes() -> None:
    with pytest.raises(SyncError, match="does not contain a menu day list"):
        _records_from_payload({"data": {"not_days": []}})
    with pytest.raises(SyncError, match="no menu day records"):
        _records_from_payload({"data": [None, "not a record"]})
    assert _records_from_payload({"data": {"entries": [{"day": "2026-09-01"}]}})

    assert _month_starts(
        ["not-a-date", "2025-08-01", "2026-09-15", "2026-09-01"],
        start_date=date(2026, 9, 2),
    ) == [date(2026, 9, 1)]


def test_ical_helpers_handle_naive_datetimes_unicode_and_empty_events() -> None:
    assert _format_dtstamp(datetime(2026, 9, 1, 12, 0)) == "20260901T120000Z"
    folded = _fold_ical_line("XXXXXXXX" + "é" * 100)
    assert all(len(line.encode("utf-8")) <= 75 for line in folded)
    assert all(line.startswith(" ") for line in folded[1:])

    calendar = generate_ical(
        [MenuDay(date(2026, 9, 1), ("Only staple",))],
        blacklist=["Only staple"],
        generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert "BEGIN:VEVENT" not in calendar


def test_ical_has_valid_all_day_structure_and_escaped_text() -> None:
    days = [
        MenuDay(
            date(2026, 9, 1),
            ("Mac & Cheese, Toast", "1% White Milk", "Fresh Fruit"),
        ),
        MenuDay(date(2026, 9, 2), ("Chicken Tenders",)),
    ]

    calendar = generate_ical(
        days,
        threshold=1.1,
        blacklist=[],
        generated_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert calendar.startswith("BEGIN:VCALENDAR\r\n")
    assert calendar.endswith("END:VCALENDAR\r\n")
    assert "VERSION:2.0\r\n" in calendar
    assert "PRODID:-//Cozyla School Lunch Menu Sync//EN\r\n" in calendar
    assert "BEGIN:VEVENT\r\n" in calendar
    assert "DTSTART;VALUE=DATE:20260901\r\n" in calendar
    assert "DTEND;VALUE=DATE:20260902\r\n" in calendar
    assert "DTSTAMP:20260901T120000Z\r\n" in calendar
    assert "SUMMARY:Lunch: Mac & Cheese\\, Toast\r\n" in calendar
    assert "DESCRIPTION:Mac & Cheese\\, Toast\\n1% White Milk\\nFresh Fruit\r\n" in calendar
    assert calendar.count("BEGIN:VEVENT") == 2


def test_long_ical_lines_are_folded() -> None:
    long_item = "A very long lunch item " * 8
    calendar = generate_ical(
        [MenuDay(date(2026, 9, 1), (long_item,))],
        threshold=1.1,
        generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    for line in calendar.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75

    assert "\r\n " in calendar


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append(url)
        return self.responses[url]


class _InvalidJsonResponse(_FakeResponse):
    def json(self) -> object:
        raise ValueError("not JSON")


def test_request_json_handles_http_and_decode_errors() -> None:
    url = "https://example.test/menu"
    with pytest.raises(SyncError, match="HTTP 500"):
        _request_json(_FakeSession({url: _FakeResponse(500, {})}), url)
    with pytest.raises(SyncError, match="invalid JSON"):
        _request_json(_FakeSession({url: _InvalidJsonResponse(200, {})}), url)
    with pytest.raises(SyncError, match="non-object"):
        _request_json(_FakeSession({url: _FakeResponse(200, [])}), url)


def test_legacy_payload_is_used_when_available() -> None:
    record = {
        "day": "2026-09-01",
        "setting": '{"current_display":[{"type":"recipe","name":"Tacos"}]}',
    }
    session = _FakeSession(
        {BASE_API_ENDPOINT: _FakeResponse(200, {"data": [record]})}
    )

    payload = fetch_menu_payload(session=session, start_date=date(2026, 9, 1))

    assert payload["data"] == [record]
    assert session.calls == [BASE_API_ENDPOINT]


def test_current_api_fallback_fetches_published_months() -> None:
    metadata_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895"
    )
    september_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895/"
        "year/2026/month/09/date_overwrites"
    )
    october_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895/"
        "year/2026/month/10/date_overwrites"
    )
    record = {
        "day": "2026-09-01",
        "setting": '{"current_display":[{"type":"recipe","name":"Tacos"}]}',
    }
    session = _FakeSession(
        {
            BASE_API_ENDPOINT: _FakeResponse(
                200,
                {"data": {"not_day_records": []}},
            ),
            metadata_url: _FakeResponse(
                200,
                {"data": {"published_months": ["2025-08-01", "2026-10-01", "2026-09-01"]}},
            ),
            september_url: _FakeResponse(200, {"data": [record]}),
            october_url: _FakeResponse(200, {"data": [record]}),
        }
    )

    payload = fetch_menu_payload(
        session=session,
        start_date=date(2026, 9, 1),
    )

    assert payload["source"] == "modern-health-e-pro-api"
    assert len(payload["data"]) == 2
    assert session.calls == [
        BASE_API_ENDPOINT,
        metadata_url,
        september_url,
        october_url,
    ]


def test_modern_api_fallback_uses_current_month_when_metadata_is_empty() -> None:
    metadata_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895"
    )
    september_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895/"
        "year/2026/month/09/date_overwrites"
    )
    session = _FakeSession(
        {
            BASE_API_ENDPOINT: _FakeResponse(404, {}),
            metadata_url: _FakeResponse(
                200,
                {"data": {"published_months": []}},
            ),
            september_url: _FakeResponse(
                200,
                {"data": [{"day": "2026-09-01", "setting": "{}"}]},
            ),
        }
    )

    payload = fetch_menu_payload(
        session=session,
        start_date=date(2026, 9, 9),
    )

    assert payload["data"]
    assert session.calls == [BASE_API_ENDPOINT, metadata_url, september_url]


def test_modern_api_rejects_bad_metadata() -> None:
    metadata_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/3660/menus/127895"
    )
    session = _FakeSession(
        {
            BASE_API_ENDPOINT: _FakeResponse(404, {}),
            metadata_url: _FakeResponse(200, {"data": None}),
        }
    )
    with pytest.raises(SyncError, match="no menu metadata"):
        fetch_menu_payload(session=session, start_date=date(2026, 9, 1))

    session = _FakeSession(
        {
            BASE_API_ENDPOINT: _FakeResponse(404, {}),
            metadata_url: _FakeResponse(
                200,
                {"data": {"published_months": "not a list"}},
            ),
        }
    )
    with pytest.raises(SyncError, match="no published months"):
        fetch_menu_payload(session=session, start_date=date(2026, 9, 1))


def test_main_writes_a_successful_calendar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        "data": [
            {
                "day": "2026-09-01",
                "setting": '{"current_display":[{"type":"recipe","name":"Tacos"}]}',
            }
        ]
    }
    monkeypatch.setattr(
        "src.sync_menu.fetch_menu_payload",
        lambda **kwargs: payload,
    )
    output = tmp_path / "lunch_menu.ics"

    result = main(
        [],
        session=_FakeSession({}),
        output_path=output,
        start_date=date(2026, 9, 1),
        generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert result == 0
    assert output.read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")


def test_main_rejects_a_success_response_with_no_valid_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "src.sync_menu.fetch_menu_payload",
        lambda **kwargs: {
            "data": [
                {
                    "day": "2026-09-01",
                    "setting": '{"current_display":[]}',
                }
            ]
        },
    )
    output = tmp_path / "lunch_menu.ics"
    original = b"existing feed"
    output.write_bytes(original)

    result = main(
        [],
        session=_FakeSession({}),
        output_path=output,
        start_date=date(2026, 9, 1),
    )

    assert result == 1
    assert output.read_bytes() == original


def test_atomic_writer_cleans_up_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "lunch_menu.ics"

    def fail_replace(*args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("src.sync_menu.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_calendar_atomically("new feed", output)
    assert not list(tmp_path.glob(".lunch_menu.ics.*.tmp"))


def test_atomic_writer_ignores_missing_temp_file_during_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "lunch_menu.ics"

    def fail_replace(*args: object) -> None:
        raise OSError("replace failed")

    def missing_unlink(*args: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("src.sync_menu.os.replace", fail_replace)
    monkeypatch.setattr("src.sync_menu.os.unlink", missing_unlink)
    with pytest.raises(OSError, match="replace failed"):
        write_calendar_atomically("new feed", output)


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("timed out"),
        requests.HTTPError("server error"),
    ],
)
def test_network_failure_does_not_overwrite_existing_calendar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    output = tmp_path / "lunch_menu.ics"
    original = "BEGIN:VCALENDAR\r\nold feed\r\nEND:VCALENDAR\r\n"
    output.write_text(original, encoding="utf-8")

    def fail_get(*args: object, **kwargs: object) -> None:
        if isinstance(failure, requests.HTTPError):
            raise requests.RequestException(str(failure))
        raise failure

    monkeypatch.setattr(requests.Session, "get", fail_get)

    result = main([], output_path=output, start_date=date(2026, 9, 1))

    assert result == 1
    assert output.read_bytes() == original.encode("utf-8")

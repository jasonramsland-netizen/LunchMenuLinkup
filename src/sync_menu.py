"""Fetch a Health-e Pro school lunch menu and publish an iCalendar feed.

The service is deliberately dependency-light.  It understands both the legacy
My School Menus public endpoint supplied for this project and the current
Health-e Pro API shape.  The latter is used as a fallback because the legacy
endpoint currently redirects to the new host and may return 404 for otherwise
valid menus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISTRICT_ID = 3660
SITE_ID = 18352
MENU_ID = 127895

# Keep the endpoint from the original project requirements.  fetch_menu_payload
# falls back to the modern endpoints below when this legacy endpoint returns
# 404, which is currently how this menu is exposed by Health-e Pro.
BASE_API_ENDPOINT = (
    "https://www.myschoolmenus.com/api/v1/public/menu/127895"
)
HEALTH_E_PRO_API_ROOT = "https://menus.healthepro.com/api"

REQUEST_HEADERS = {
    "x-district": str(DISTRICT_ID),
    "User-Agent": "Mozilla/5.0",
}
REQUEST_TIMEOUT_SECONDS = 30
REPEATING_ITEM_THRESHOLD = 0.70

# Exact matches are intentionally easy to edit.  Names are trimmed and
# internal whitespace is normalized before comparison, but matching remains
# case-sensitive so an accidental broad blacklist is less likely.
EXPLICIT_BLACKLIST = [
    "1% White Milk",
    "Fat Free Chocolate Milk",
    "Condiment Packet",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "lunch_menu.ics"


class SyncError(RuntimeError):
    """A recoverable synchronization failure.

    The command catches this error before writing the output file.  That makes
    a transient API outage unable to replace a previously working feed.
    """


@dataclass(frozen=True)
class MenuDay:
    """The date and unique recipe names shown for one scheduled lunch day."""

    day: date
    items: tuple[str, ...]


def _clean_item_name(value: Any) -> str:
    """Return a display name with harmless whitespace normalized."""

    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\xa0", " ").split())


def _parse_date(value: Any) -> date | None:
    """Parse the ISO date/datetime values used by Health-e Pro."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _parse_json_object(value: Any, *, field_name: str) -> Mapping[str, Any]:
    """Decode a setting that may be a JSON string or an object."""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SyncError(f"Invalid JSON in {field_name}: {exc}") from exc
        if isinstance(parsed, Mapping):
            return parsed
    raise SyncError(f"Menu record has no usable {field_name} object")


def _record_setting(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Get the menu setting from current or older API field names."""

    for key in ("setting", "settings", "menu_setting"):
        if key in record and record[key] not in (None, ""):
            return _parse_json_object(record[key], field_name=key)

    # Some test fixtures and older API responses expose display data directly.
    if any(key in record for key in ("current_display", "items", "recipes")):
        return record

    raise SyncError("Menu record has no setting field")


def _display_items(
    setting: Mapping[str, Any], record: Mapping[str, Any]
) -> Sequence[Any]:
    """Find the ordered display list in a menu record."""

    for source in (setting, record):
        for key in ("current_display", "items", "menu_items", "recipes"):
            candidate = source.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _item_name(item: Any) -> str:
    """Extract a recipe name from a current-display entry."""

    if isinstance(item, str):
        return _clean_item_name(item)
    if not isinstance(item, Mapping):
        return ""

    for key in ("name", "recipe_name", "title", "label"):
        name = _clean_item_name(item.get(key))
        if name:
            return name
    return ""


def _recipe_names(
    setting: Mapping[str, Any], record: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return unique recipe names in their on-screen order.

    Category headers such as ``Fruit`` and ``Milk`` are intentionally not
    included.  A few old responses omit ``type`` but expose ``recipe_name``;
    those entries are accepted for compatibility.
    """

    names: list[str] = []
    seen: set[str] = set()
    for item in _display_items(setting, record):
        if isinstance(item, Mapping):
            item_type = str(item.get("type", "")).casefold()
            if item_type and item_type not in {
                "recipe",
                "menu_item",
                "food",
                "item",
            }:
                continue
            if not item_type and not any(
                key in item for key in ("recipe_name", "recipe", "food")
            ):
                continue

        name = _item_name(item)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def _is_day_off(setting: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Return whether a record represents a scheduled day off."""

    value: Any = setting.get("days_off", record.get("days_off"))
    if isinstance(value, Mapping):
        if "status" in value:
            return bool(value["status"])
        return bool(value.get("description"))
    return bool(value)


def _records_from_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Normalize the list-shaped menu payload used by both APIs."""

    raw_data: Any = payload.get("data", payload)
    if isinstance(raw_data, Mapping):
        for key in ("days", "entries", "menu_days", "records"):
            candidate = raw_data.get(key)
            if isinstance(candidate, list):
                raw_data = candidate
                break

    if not isinstance(raw_data, list):
        raise SyncError("API response does not contain a menu day list")

    records = [record for record in raw_data if isinstance(record, Mapping)]
    if not records:
        raise SyncError("API response contains no menu day records")
    return records


def extract_menu_days(
    payload: Mapping[str, Any],
    *,
    start_date: date | None = None,
) -> list[MenuDay]:
    """Extract valid, scheduled menu days from an API response.

    A valid day has a parseable date, is not marked as a day off, and has at
    least one recipe.  Past days can be excluded with ``start_date`` before
    calculating item frequencies.
    """

    days_by_date: dict[date, list[str]] = {}
    for record in _records_from_payload(payload):
        menu_day = _parse_date(
            record.get("day", record.get("date", record.get("menu_date")))
        )
        if menu_day is None or (start_date is not None and menu_day < start_date):
            continue

        setting = _record_setting(record)
        if _is_day_off(setting, record):
            continue

        items = _recipe_names(setting, record)
        if not items:
            continue

        # Merge duplicate records for a date while retaining first-seen order.
        existing = days_by_date.setdefault(menu_day, [])
        for item in items:
            if item not in existing:
                existing.append(item)

    return [
        MenuDay(day=menu_day, items=tuple(days_by_date[menu_day]))
        for menu_day in sorted(days_by_date)
    ]


def calculate_item_frequencies(days: Iterable[MenuDay]) -> dict[str, float]:
    """Calculate the fraction of valid menu days containing each item.

    An item is counted at most once per day, even if a malformed payload lists
    it twice.  This is the frequency used by the 70% repeating-staple filter.
    """

    day_list = list(days)
    if not day_list:
        return {}

    occurrences = Counter(
        item
        for menu_day in day_list
        for item in set(menu_day.items)
    )
    denominator = len(day_list)
    return {
        item: occurrences[item] / denominator
        for item in occurrences
    }


def find_repeating_items(
    days: Iterable[MenuDay],
    *,
    threshold: float = REPEATING_ITEM_THRESHOLD,
    blacklist: Iterable[str] = EXPLICIT_BLACKLIST,
) -> set[str]:
    """Return automatically repeating and explicitly blacklisted item names."""

    frequencies = calculate_item_frequencies(days)
    repeating = {
        item for item, frequency in frequencies.items() if frequency >= threshold
    }
    repeating.update(
        cleaned
        for item in blacklist
        if (cleaned := _clean_item_name(item))
    )
    return repeating


def filter_repeating_items(
    days: Iterable[MenuDay],
    *,
    threshold: float = REPEATING_ITEM_THRESHOLD,
    blacklist: Iterable[str] = EXPLICIT_BLACKLIST,
) -> list[MenuDay]:
    """Remove repeating staples while preserving dates and item order."""

    day_list = list(days)
    excluded = find_repeating_items(
        day_list,
        threshold=threshold,
        blacklist=blacklist,
    )
    return [
        MenuDay(
            day=menu_day.day,
            items=tuple(item for item in menu_day.items if item not in excluded),
        )
        for menu_day in day_list
    ]


def _escape_ical_text(value: str) -> str:
    """Escape a TEXT property value according to RFC 5545 section 3.3.11."""

    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _fold_ical_line(line: str) -> list[str]:
    """Fold a content line at 75 UTF-8 octets as required by RFC 5545."""

    if len(line.encode("utf-8")) <= 75:
        return [line]

    folded: list[str] = []
    remaining = line
    first_line = True
    while remaining:
        # Continuation lines include one leading space, leaving 74 octets for
        # the content portion.
        content_limit = 75 if first_line else 74
        encoded = remaining.encode("utf-8")
        if len(encoded) <= content_limit:
            chunk = remaining
            remaining = ""
        else:
            boundary = content_limit
            while boundary > 0 and (encoded[boundary] & 0xC0) == 0x80:
                boundary -= 1
            chunk = encoded[:boundary].decode("utf-8")
            remaining = encoded[boundary:].decode("utf-8")

        folded.append(chunk if first_line else f" {chunk}")
        first_line = False
    return folded


def _uid_for_day(menu_day: date) -> str:
    """Build a deterministic UID so calendar clients can update events."""

    seed = f"{DISTRICT_ID}:{SITE_ID}:{MENU_ID}:{menu_day.isoformat()}".encode()
    digest = hashlib.sha1(seed).hexdigest()
    return f"{digest}@cozyla-lunch-menu"


def _format_dtstamp(value: datetime | None) -> str:
    """Format a UTC DTSTAMP, using the current time when not supplied."""

    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_ical(
    days: Iterable[MenuDay],
    *,
    threshold: float = REPEATING_ITEM_THRESHOLD,
    blacklist: Iterable[str] = EXPLICIT_BLACKLIST,
    generated_at: datetime | None = None,
) -> str:
    """Generate an RFC 5545 iCalendar string containing all-day events."""

    day_list = list(days)
    filtered_days = filter_repeating_items(
        day_list,
        threshold=threshold,
        blacklist=blacklist,
    )
    dtstamp = _format_dtstamp(generated_at)

    content_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Cozyla School Lunch Menu Sync//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:School Lunch Menu",
        "X-WR-CALDESC:Synced school lunch menu",
    ]

    for menu_day in filtered_days:
        if not menu_day.items:
            # A day with only repeating staples is not useful as a calendar
            # event, and omitting it avoids an empty "Lunch:" entry.
            continue

        next_day = menu_day.day + timedelta(days=1)
        description = "\n".join(menu_day.items)
        content_lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{_uid_for_day(menu_day.day)}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{menu_day.day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{next_day:%Y%m%d}",
                f"SUMMARY:{_escape_ical_text('Lunch: ' + menu_day.items[0])}",
                f"DESCRIPTION:{_escape_ical_text(description)}",
                "END:VEVENT",
            ]
        )

    content_lines.append("END:VCALENDAR")
    folded_lines = [
        folded
        for content_line in content_lines
        for folded in _fold_ical_line(content_line)
    ]
    # RFC 5545 content lines use CRLF and the final line is also terminated.
    return "\r\n".join(folded_lines) + "\r\n"


def _request_json(
    session: requests.Session,
    url: str,
    *,
    allow_not_found: bool = False,
) -> Mapping[str, Any] | None:
    """GET and decode one JSON API response with useful error messages."""

    try:
        response = session.get(
            url,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SyncError(f"Unable to reach menu API: {exc}") from exc

    if response.status_code == 404 and allow_not_found:
        return None
    if response.status_code != 200:
        raise SyncError(
            f"Menu API returned HTTP {response.status_code} for {url}"
        )

    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as exc:
        raise SyncError(f"Menu API returned invalid JSON for {url}") from exc
    if not isinstance(payload, Mapping):
        raise SyncError(f"Menu API returned a non-object response for {url}")
    return payload


def _month_starts(
    published_months: Iterable[Any],
    *,
    start_date: date,
) -> list[date]:
    """Select published months that can contain current/upcoming days."""

    starts: set[date] = set()
    for value in published_months:
        parsed = _parse_date(value)
        if parsed is None:
            continue
        month_start = parsed.replace(day=1)
        if (month_start.year, month_start.month) >= (
            start_date.year,
            start_date.month,
        ):
            starts.add(month_start)
    return sorted(starts)


def _fetch_modern_menu_payload(
    session: requests.Session,
    *,
    start_date: date,
) -> Mapping[str, Any]:
    """Fetch current and future monthly records from the current API."""

    metadata_url = (
        f"{HEALTH_E_PRO_API_ROOT}/organizations/{DISTRICT_ID}/menus/{MENU_ID}"
    )
    metadata_response = _request_json(session, metadata_url)
    assert metadata_response is not None
    metadata = metadata_response.get("data")
    if not isinstance(metadata, Mapping):
        raise SyncError("Modern menu API returned no menu metadata")

    published_months = metadata.get("published_months", [])
    if not isinstance(published_months, list):
        raise SyncError("Modern menu metadata has no published months")

    month_starts = _month_starts(published_months, start_date=start_date)
    if not month_starts:
        # Keep the service useful if Health-e Pro temporarily omits metadata
        # while a current month is still accessible.
        month_starts = [start_date.replace(day=1)]

    records: list[Mapping[str, Any]] = []
    for month_start in month_starts:
        month_url = (
            f"{HEALTH_E_PRO_API_ROOT}/organizations/{DISTRICT_ID}/menus/"
            f"{MENU_ID}/year/{month_start.year}/month/{month_start.month:02d}/"
            "date_overwrites"
        )
        month_response = _request_json(session, month_url)
        assert month_response is not None
        month_records = _records_from_payload(month_response)
        records.extend(month_records)

    return {
        "data": records,
        "metadata": metadata,
        "site_id": SITE_ID,
        "source": "modern-health-e-pro-api",
    }


def fetch_menu_payload(
    *,
    session: requests.Session | None = None,
    start_date: date | None = None,
) -> Mapping[str, Any]:
    """Fetch the supplied public endpoint, falling back to current API routes."""

    client = session or requests.Session()
    first_day = start_date or date.today()
    legacy_response = _request_json(
        client,
        BASE_API_ENDPOINT,
        allow_not_found=True,
    )

    if legacy_response is not None:
        try:
            legacy_records = _records_from_payload(legacy_response)
        except SyncError:
            legacy_records = []
        if legacy_records:
            return legacy_response

    return _fetch_modern_menu_payload(client, start_date=first_day)


def write_calendar_atomically(content: str, output_path: Path = OUTPUT_PATH) -> None:
    """Write a generated feed atomically so failures preserve the old feed."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync the school lunch menu to lunch_menu.ics."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Output path (default: lunch_menu.ics in the repository root).",
    )
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        default=None,
        help="Optional ISO date used as the first included menu day.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session: requests.Session | None = None,
    output_path: Path | None = None,
    start_date: date | None = None,
    generated_at: datetime | None = None,
) -> int:
    """Run one synchronization; return 0 on success and 1 on failure."""

    args = _build_argument_parser().parse_args(argv)
    destination = Path(output_path or args.output)
    first_day = start_date or args.from_date or date.today()
    client = session or requests.Session()

    try:
        payload = fetch_menu_payload(session=client, start_date=first_day)
        days = extract_menu_days(payload, start_date=first_day)
        if not days:
            raise SyncError("API returned no valid current or upcoming menu days")
        calendar = generate_ical(days, generated_at=generated_at)
        write_calendar_atomically(calendar, destination)
    except (SyncError, OSError, requests.RequestException) as exc:
        print(f"Lunch menu sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if session is None:
            client.close()

    print(f"Wrote {destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

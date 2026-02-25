#!/usr/bin/env python3
"""
Google Calendar API wrapper using service account.
Usage:
  gcal.py list [--days N]           List upcoming events
  gcal.py create SUMMARY START END  Create event (ISO datetime or "tomorrow 3pm")
  gcal.py delete EVENT_ID           Delete event
  gcal.py test                      Test connection
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pathlib import Path

CONFIG_DIR = os.path.expanduser("~/.config/google-calendar")
SERVICE_ACCOUNT_FILE = os.path.join(CONFIG_DIR, "service-account.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

if not Path(CONFIG_FILE).exists():
    print(f"ERROR: Config file not found: {CONFIG_FILE}", file=sys.stderr)
    print(
        'Please create config.json with: {"calendar_id": "...", "timezone": "..."}',
        file=sys.stderr,
    )
    sys.exit(1)

try:
    with open(CONFIG_FILE) as f:
        config = json.load(f)
except (json.JSONDecodeError, OSError) as e:
    print(f"ERROR: Failed to load config file: {e}", file=sys.stderr)
    sys.exit(1)

CALENDAR_ID = config.get("calendar_id")
if not CALENDAR_ID:
    print("ERROR: config.json missing required 'calendar_id' field", file=sys.stderr)
    sys.exit(1)

TIMEZONE = config.get("timezone", "Europe/Zurich")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_service():
    """Get authenticated Calendar service."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def list_upcoming_events(days=7, max_results=50):
    """List upcoming events."""
    service = get_service()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    time_max = now + timedelta(days=days)

    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    return events_result.get("items", [])


def create_event(summary, start, end, description=None, location=None):
    """Create a calendar event."""
    service = get_service()

    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute()


def create_all_day_event(summary, date, description=None):
    """Create an all-day event.

    Args:
        summary: Event title
        date: Either a datetime object, date object, or YYYY-MM-DD string.
              Datetime objects are converted to the configured timezone.
        description: Optional event description
    """
    service = get_service()

    tz = ZoneInfo(TIMEZONE)

    if isinstance(date, datetime):
        local_date = date.astimezone(tz).date()
    elif hasattr(date, "strftime"):
        local_date = date
    else:
        try:
            local_date = datetime.strptime(date, "%Y-%m-%d").date()
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid date format '{date}': expected YYYY-MM-DD. {e}")

    date_str = local_date.strftime("%Y-%m-%d")

    event = {
        "summary": summary,
        "start": {"date": date_str, "timeZone": TIMEZONE},
        "end": {"date": date_str, "timeZone": TIMEZONE},
    }
    if description:
        event["description"] = description

    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute()


def delete_event(event_id):
    """Delete a calendar event."""
    service = get_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()


def update_event(event_id, **kwargs):
    """Update a calendar event."""
    service = get_service()
    event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    if "summary" in kwargs:
        event["summary"] = kwargs["summary"]
    if "description" in kwargs:
        event["description"] = kwargs["description"]
    if "start" in kwargs:
        event["start"] = {"dateTime": kwargs["start"].isoformat(), "timeZone": TIMEZONE}
    if "end" in kwargs:
        event["end"] = {"dateTime": kwargs["end"].isoformat(), "timeZone": TIMEZONE}

    return (
        service.events()
        .update(calendarId=CALENDAR_ID, eventId=event_id, body=event)
        .execute()
    )


def format_event(event):
    """Format event for display."""
    start = event["start"].get("dateTime", event["start"].get("date"))
    if "T" in start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            start_str = dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError) as e:
            start_str = f"(parse error: {e})"
    else:
        start_str = start + " (all day)"
    return f"{start_str}: {event['summary']} [{event['id'][:8]}...]"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Calendar CLI")
    parser.add_argument(
        "command", choices=["list", "test", "create", "update", "delete"]
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "test":
        print("Testing calendar connection...")
        events = list_upcoming_events(days=30)
        print(f"✓ Connected! Found {len(events)} upcoming events.")

    elif args.command == "list":
        events = list_upcoming_events(days=args.days)
        if args.json:
            print(json.dumps(events, indent=2, default=str))
        elif events:
            for event in events:
                print(format_event(event))
        else:
            print(f"No events in the next {args.days} days.")

    elif args.command == "delete":
        if not args.args:
            print("Usage: gcal.py delete EVENT_ID")
            sys.exit(1)
        delete_event(args.args[0])
        print(f"✓ Deleted event {args.args[0]}")

    elif args.command == "update":
        if len(args.args) < 2:
            print(
                "Usage: gcal.py update EVENT_ID [--summary TEXT] [--description TEXT]"
            )
            sys.exit(1)
        event_id = args.args[0]
        kwargs = {}
        rest = args.args[1:]
        for i in range(0, len(rest) - 1, 2):
            key = rest[i].lstrip("-").replace("-", "_")
            if key in ("summary", "description"):
                kwargs[key] = rest[i + 1]
        if not kwargs:
            print(
                "Usage: gcal.py update EVENT_ID [--summary TEXT] [--description TEXT]"
            )
            sys.exit(1)
        updated = update_event(event_id, **kwargs)
        print(f"✓ Updated event {event_id}: {updated.get('summary', '')}")

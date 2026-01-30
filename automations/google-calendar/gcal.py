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

CONFIG_DIR = os.path.expanduser('~/.config/google-calendar')
SERVICE_ACCOUNT_FILE = os.path.join(CONFIG_DIR, 'service-account.json')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

with open(CONFIG_FILE) as f:
    config = json.load(f)

CALENDAR_ID = config['calendar_id']
TIMEZONE = config.get('timezone', 'Europe/Zurich')
SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_service():
    """Get authenticated Calendar service."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds)

def list_upcoming_events(days=7, max_results=50):
    """List upcoming events."""
    service = get_service()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    time_max = now + timedelta(days=days)
    
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=time_max.isoformat(),
        maxResults=max_results,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    return events_result.get('items', [])

def create_event(summary, start, end, description=None, location=None):
    """Create a calendar event."""
    service = get_service()
    
    event = {
        'summary': summary,
        'start': {'dateTime': start.isoformat(), 'timeZone': TIMEZONE},
        'end': {'dateTime': end.isoformat(), 'timeZone': TIMEZONE},
    }
    if description:
        event['description'] = description
    if location:
        event['location'] = location
    
    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

def create_all_day_event(summary, date, description=None):
    """Create an all-day event."""
    service = get_service()
    
    event = {
        'summary': summary,
        'start': {'date': date},
        'end': {'date': date},
    }
    if description:
        event['description'] = description
    
    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

def delete_event(event_id):
    """Delete a calendar event."""
    service = get_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()

def update_event(event_id, **kwargs):
    """Update a calendar event."""
    service = get_service()
    event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    
    if 'summary' in kwargs:
        event['summary'] = kwargs['summary']
    if 'description' in kwargs:
        event['description'] = kwargs['description']
    if 'start' in kwargs:
        event['start'] = {'dateTime': kwargs['start'].isoformat(), 'timeZone': TIMEZONE}
    if 'end' in kwargs:
        event['end'] = {'dateTime': kwargs['end'].isoformat(), 'timeZone': TIMEZONE}
    
    return service.events().update(calendarId=CALENDAR_ID, eventId=event_id, body=event).execute()

def format_event(event):
    """Format event for display."""
    start = event['start'].get('dateTime', event['start'].get('date'))
    if 'T' in start:
        dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        start_str = dt.astimezone(ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d %H:%M')
    else:
        start_str = start + ' (all day)'
    return f"{start_str}: {event['summary']} [{event['id'][:8]}...]"

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Google Calendar CLI')
    parser.add_argument('command', choices=['list', 'test', 'create', 'delete'])
    parser.add_argument('args', nargs='*')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    
    if args.command == 'test':
        print("Testing calendar connection...")
        events = list_upcoming_events(days=30)
        print(f"✓ Connected! Found {len(events)} upcoming events.")
        
    elif args.command == 'list':
        events = list_upcoming_events(days=args.days)
        if args.json:
            print(json.dumps(events, indent=2, default=str))
        elif events:
            for event in events:
                print(format_event(event))
        else:
            print(f"No events in the next {args.days} days.")
            
    elif args.command == 'delete':
        if not args.args:
            print("Usage: gcal.py delete EVENT_ID")
            sys.exit(1)
        delete_event(args.args[0])
        print(f"✓ Deleted event {args.args[0]}")

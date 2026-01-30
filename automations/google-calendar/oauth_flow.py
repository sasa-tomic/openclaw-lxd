#!/usr/bin/env python3
"""
Google Calendar OAuth flow.
Run once to authorize, then tokens are saved for future use.
"""
import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = os.path.expanduser('~/.config/google-calendar/credentials.json')
TOKEN_FILE = os.path.expanduser('~/.config/google-calendar/token.json')

def main():
    creds = None
    
    # Load existing token if available
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    # If no valid credentials, run the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            print("Starting OAuth flow...")
            print("=" * 60)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            
            # Generate auth URL for manual flow (no local server needed)
            auth_url, _ = flow.authorization_url(prompt='consent')
            print(f"\n1. Open this URL in your browser:\n\n{auth_url}\n")
            print("2. Authorize the app and copy the authorization code.")
            print("3. Paste the code below:\n")
            
            code = input("Authorization code: ").strip()
            flow.fetch_token(code=code)
            creds = flow.credentials
        
        # Save the credentials
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
        print(f"\nToken saved to {TOKEN_FILE}")
    
    print("\n✓ Authorization successful!")
    return creds

if __name__ == '__main__':
    main()

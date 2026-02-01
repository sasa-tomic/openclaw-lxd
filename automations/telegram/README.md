# Telegram to Obsidian Sync

Syncs Telegram messages to Obsidian markdown files using the Telethon library.

## Features

- **DMs** → `/projects/Notes/Telegram/DMs/<contact_name>.md`
- **Groups/Channels** → `/projects/Notes/Telegram/Groups/<group_name>.md`
- Live sync with automatic reconnection
- Tracks last synced message ID per chat (no duplicates)
- Initial sync: last 100 messages per chat
- Rate limiting to avoid Telegram API limits

## Setup

### 1. Install dependencies

```bash
cd /projects/automations/telegram
uv venv
uv pip install telethon python-dotenv
```

### 2. Configure credentials

The `.env` file should already exist with your API credentials:

```bash
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

Get these from https://my.telegram.org/apps

### 3. First run (interactive auth)

**Important:** The first run requires interactive authentication:

```bash
cd /projects/automations/telegram
source .venv/bin/activate
python telegram-sync.py
```

You will be prompted for:
1. Your phone number (international format, e.g., +1234567890)
2. The verification code sent to your Telegram app
3. (Optional) 2FA password if enabled

The session is saved to `.session/` directory.

### 4. Verify it works

After auth, the script will:
1. Show your logged-in account
2. Perform initial sync (last 100 messages per chat)
3. Listen for new messages

Check the Obsidian folder:
```bash
ls -la /projects/Notes/Telegram/DMs/
ls -la /projects/Notes/Telegram/Groups/
```

### 5. Install systemd service

```bash
# Copy service file
mkdir -p ~/.config/systemd/user/
cp telegram-obsidian-sync.service ~/.config/systemd/user/

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable telegram-obsidian-sync
systemctl --user start telegram-obsidian-sync

# Check status
systemctl --user status telegram-obsidian-sync
```

## Usage

### Manual run
```bash
cd /projects/automations/telegram
source .venv/bin/activate
python telegram-sync.py
```

### Service management
```bash
# Start/stop/restart
systemctl --user start telegram-obsidian-sync
systemctl --user stop telegram-obsidian-sync
systemctl --user restart telegram-obsidian-sync

# View logs
tail -f /projects/automations/telegram/sync.log
journalctl --user -u telegram-obsidian-sync -f
```

## Message Format

Messages are formatted as:
```markdown
# Contact Name

_Telegram chat - live sync via telethon_

---

[2026-01-31 12:34:56] Sender Name: Message text
[2026-01-31 12:35:00] Me: My reply
[2026-01-31 12:35:10] Sender Name: [Photo] Caption here
```

## Files

- `telegram-sync.py` - Main sync script
- `.env` - Credentials (gitignored)
- `.session/` - Telegram session files (gitignored)
- `.state/` - Sync state tracking (gitignored)
- `sync.log` - Runtime logs
- `telegram-obsidian-sync.service` - Systemd unit

## Troubleshooting

### "Session expired" or auth errors
Delete the session and re-authenticate:
```bash
rm -rf .session/
python telegram-sync.py
```

### Rate limiting
The script has built-in delays. If you hit limits:
- Wait a few minutes
- Reduce `INITIAL_SYNC_LIMIT` in the script

### Missing messages
Check `.state/sync-state.json` - it tracks per-chat sync state.
To force re-sync a chat, remove its entry from the state file.

### Connection drops
The systemd service auto-restarts. For manual runs, just restart the script.

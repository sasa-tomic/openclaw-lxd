# Automations

Message sync scripts for Obsidian notes.

## Signal Sync

Live sync of Signal messages to Obsidian.

**Requirements:**
- `signal-cli` (native build) authenticated and linked
- Python 3.10+

**Files:**
- `signal/sync_to_obsidian.py` - Main sync script
- `signal/run_sync.sh` - Service wrapper

**Service:**
```bash
# Status
systemctl --user status signal-obsidian-sync

# Restart
systemctl --user restart signal-obsidian-sync

# Logs
tail -f ~/.signal-cli/obsidian-sync.log
```

**Configuration (env vars):**
| Variable | Default | Description |
|----------|---------|-------------|
| `OBSIDIAN_SIGNAL_DIR` | `~/clawd/notes/Signal` | Output directory |
| `SIGNAL_STATE_FILE` | `~/.signal-cli/obsidian-sync-state.json` | Sync state |
| `SIGNAL_CONTACTS_FILE` | `~/.signal-cli/contacts.json` | Contacts cache |
| `SIGNAL_CLI` | `/home/moltbot/homebrew/bin/signal-cli` | signal-cli path |
| `SIGNAL_PHONE` | `+41798471964` | Account phone number |

## WhatsApp Sync

Batch sync of WhatsApp messages via wacli.

**Requirements:**
- `wacli` authenticated and syncing
- Python 3.10+

**Files:**
- `whatsapp/sync_to_obsidian.py` - Main sync script

**Cron:**
```bash
*/15 * * * * /projects/automations/whatsapp/sync_to_obsidian.py
```

**Configuration (env vars):**
| Variable | Default | Description |
|----------|---------|-------------|
| `OBSIDIAN_WHATSAPP_DIR` | `~/clawd/notes/WhatsApp` | Output directory |
| `WHATSAPP_STATE_FILE` | `~/.wacli/obsidian-sync-state.json` | Sync state |
| `WACLI_PATH` | `~/homebrew/bin/wacli` | wacli binary |

## Output Format

Both sync to the same markdown format:
```
[YYYY-MM-DD HH:MM:SS] Sender: Message text
```

Compatible with signal-export and searchable in Obsidian.

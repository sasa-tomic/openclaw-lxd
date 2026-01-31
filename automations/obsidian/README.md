# Obsidian Note Watcher

Monitors `/projects/Notes` for markdown file changes and wakes the OpenClaw main session to review them.

## Features

- **Debouncing:** Waits 2 seconds after the last change before triggering (batches rapid edits)
- **Smart filtering:** Skips `.obsidian/`, `.trash/`, temp files, sync-conflict files
- **Loop prevention:** 60-second cooldown per file to prevent wake → edit → wake loops

## Installation

```bash
# Create/activate virtual environment (already done)
cd /projects/automations/obsidian
uv venv .venv
source .venv/bin/activate
uv pip install watchdog

# Make executable
chmod +x obsidian-watcher.py
```

## Manual Testing

```bash
cd /projects/automations/obsidian
source .venv/bin/activate
python obsidian-watcher.py
```

Then edit any `.md` file in `/projects/Notes` (not in `.obsidian/` or `.trash/`).

## Systemd Service (User)

```bash
# Copy service file to user systemd directory
mkdir -p ~/.config/systemd/user
cp obsidian-watcher.service ~/.config/systemd/user/

# Reload systemd and enable
systemctl --user daemon-reload
systemctl --user enable obsidian-watcher.service
systemctl --user start obsidian-watcher.service

# Check status
systemctl --user status obsidian-watcher.service

# View logs
journalctl --user -u obsidian-watcher.service -f
```

## Configuration

Edit `obsidian-watcher.py` to change:

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCH_PATH` | `/projects/Notes` | Directory to monitor |
| `DEBOUNCE_SECONDS` | `2.0` | Wait time before triggering |
| `COOLDOWN_SECONDS` | `60.0` | Per-file cooldown to prevent loops |
| `SKIP_DIRS` | `.obsidian`, `.trash`, etc. | Directories to ignore |
| `SKIP_PATTERNS` | `sync-conflict`, `.tmp`, etc. | Filename patterns to ignore |

## How It Works

1. Uses `watchdog` library to monitor filesystem events
2. Filters events based on path patterns and cooldown
3. Debounces rapid changes (waits for quiet period)
4. Wakes OpenClaw main session via `openclaw agent` CLI
5. Main session reviews the change and updates memory/notes as needed

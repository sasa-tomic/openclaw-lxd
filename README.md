# Openclaw

Run [Clawdbot](https://docs.molt.bot) in an LXD container with host browser control.

## Setup

```bash
./openclaw --projects ~/projects/myapp ~/projects/other
```

## Container Management

```bash
./openclaw shell      # enter container
./openclaw list       # status
./openclaw stop       # stop
./openclaw start      # start
./openclaw destroy    # remove
```

## Usage

**TUI (interactive chat):**
```bash
clawdbot tui
clawdbot tui --session myproject   # resume session
```

**Browser control** (launch Chrome on host first):
```bash
# Host:
google-chrome --remote-debugging-port=9222

# Container:
clawdbot browser open https://example.com
clawdbot browser snapshot              # get page state
clawdbot browser click e12             # click element ref
clawdbot browser type e5 "hello"       # type into element
clawdbot browser screenshot            # capture
```

**Send message to agent:**
```bash
clawdbot agent "fix the bug in main.rs"
clawdbot agent --session myproject "continue"
```

**Agents:**
```bash
clawdbot agents list
clawdbot agents add myagent    # interactive setup
clawdbot agents delete <id>
```

**Gateway:**
```bash
clawdbot gateway status
# Dashboard: http://127.0.0.1:18789/
```

## Architecture

- LXD container runs Clawdbot + Playwright
- Browser runs on host with CDP (port 9222)
- Container connects to host browser via LXD bridge
- Projects mounted at `/projects/<name>`

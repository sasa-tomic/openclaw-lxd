# How Tasks Are Added to Todoist

This document explains how the Obsidian Watcher automatically extracts and adds tasks to Todoist.

## Overview

The Obsidian Watcher monitors `/projects/Notes` for ALL changes including:
- Regular notes
- Chat messages (Signal, WhatsApp, Telegram)
- Daily notes

**Tasks go to Todoist, NOT TODO.md!**

## What Gets Extracted

The watcher is AGGRESSIVE. It extracts:

### From Notes
- `- [ ]` unchecked checkboxes
- `TODO:`, `FIXME:`, `ACTION:`, `FOLLOW-UP:` lines
- Commitments: "I'll...", "I need to...", "I should...", "I must..."
- Questions that need answers
- Meeting/action items

### From Chats
- Requests: "Can you...", "Could you...", "Please..."
- Promises: "I'll send you...", "Let me know..."
- Follow-ups: "We should...", "Don't forget..."
- Meeting mentions: "call with X", "meeting with Y"
- Questions addressed to you

## Project Detection

Tasks are automatically assigned to projects based on keywords:

| Keywords | Todoist Project |
|----------|-----------------|
| axiom, Axiom | #Axiom GmbH |
| voKI, voki, voxtral | #VoKI |
| decent cloud | #Decent Cloud |
| personal, family, kids | #Personal |
| (default) | #Inbox |

## Deduplication

Before adding a task, the watcher uses **LLM-powered intelligent deduplication**:

1. **LLM Analysis**: A single LLM call compares the new task against all existing tasks
2. **Objective Evaluation**: The LLM provides objective reasoning about whether tasks are duplicates
3. **Smart Handling**:
   - If **duplicate found**: Skips adding the new task, keeps existing tasks
   - If **unique**: Adds as new task

This prevents duplicate tasks from:
- Multiple chat messages about the same action
- Repeated mentions across different notes
- Task rewording that means the same thing

The LLM evaluates task equivalence objectively, not just text similarity, so:
- "Call John" and "Phone John" → **Duplicate** (same action)
- "Call John" and "Email John" → **Not duplicate** (different actions)

## Priority Assignment

| Source | Priority |
|--------|----------|
| "must", "urgent", "don't forget" | 1-2 (High) |
| "need to", "have to", "will" | 3 (Medium) |
| "should", "want to", "maybe" | 4 (Low) |

## Manual Task Addition

### Via CLI
```bash
todoist add "Task description" -N "#Project Name" -p 2 -d "tomorrow"
```

### Via Obsidian
Just write naturally in any note:
```
I need to follow up with John about the proposal.
TODO: Review the contract by Friday
- [ ] Send invoice to client
```

The watcher will pick it up within seconds.

## Configuration

- **Watcher script**: `/projects/automations/obsidian/obsidian-watcher.py`
- **Cooldown**: 2 minutes per file (prevents spam from edits)
- **Todoist CLI**: `/home/openclaw/.local/bin/todoist`

## Troubleshooting

### Check if watcher is running
```bash
systemctl --user status obsidian-watcher
```

### View recent logs
```bash
journalctl --user -u obsidian-watcher -f
```

### Manual Todoist sync
```bash
todoist sync
todoist list
```

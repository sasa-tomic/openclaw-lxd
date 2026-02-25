#!/bin/bash
# Pickle Obsidian Maintenance Service Wrapper
# Runs the opencode-based vault maintenance service
#
# Usage:
#   ./pickle-obsidian-maintenance.sh [--mode scan|apply] [--rule-id <id>]
#
# Modes:
#   scan  - Read-only analysis, no changes
#   apply - Apply improvements (default)
#
# Rule IDs:
#   daily_maintenance    - Full daily pass (default)
#   quick_scan           - Fast hourly check
#   knowledge_extraction - Extract from daily notes
#   organization         - Reorganize vault structure
#   link_maintenance     - Fix and add wikilinks
#   archive_cleanup      - Move stale content to archive

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBSIDIAN_DIR="/projects/automations/obsidian"
PYTHON_SCRIPT="$OBSIDIAN_DIR/pickle_obsidian_maintenance.py"
RULES_FILE="$OBSIDIAN_DIR/vault-rules.json"
LOG_DIR="/var/log/pickle"

mkdir -p "$LOG_DIR" 2>/dev/null || true

MODE="${1:-apply}"
RULE_ID="${2:-daily_maintenance}"
TIMEOUT="${TIMEOUT:-600}"

if [[ "$1" == "--mode" ]]; then
    MODE="$2"
    shift 2
fi

if [[ "$1" == "--rule-id" ]]; then
    RULE_ID="$2"
    shift 2
fi

if [[ ! -f "$PYTHON_SCRIPT" ]]; then
    echo "ERROR: Python script not found: $PYTHON_SCRIPT" >&2
    exit 1
fi

TIMESTAMP=$(date -Iseconds)
LOG_FILE="$LOG_DIR/obsidian-maintenance-$(date +%Y%m%d).log"

{
    echo "=== PICKLE OBSIDIAN MAINTENANCE ==="
    echo "Timestamp: $TIMESTAMP"
    echo "Mode: $MODE"
    echo "Rule: $RULE_ID"
    echo ""
    
    python3 "$PYTHON_SCRIPT" \
        --mode "$MODE" \
        --rules "$RULES_FILE" \
        --rule-id "$RULE_ID" \
        --timeout "$TIMEOUT" \
        2>&1
    
    EXIT_CODE=$?
    
    echo ""
    echo "Exit code: $EXIT_CODE"
    echo "Completed: $(date -Iseconds)"
    
    exit $EXIT_CODE
} 2>&1 | tee -a "$LOG_FILE"

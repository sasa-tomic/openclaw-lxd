#!/bin/bash
# Watch Signal/WhatsApp Obsidian notes for new incoming messages
# Triggers Clawdbot to draft replies in near real-time

NOTES_DIR="/projects/Notes"
STATE_FILE="/home/openclaw/clawd/memory/message-watcher-state.json"
SIGNAL_DIR="$NOTES_DIR/Signal"
WHATSAPP_DIR="$NOTES_DIR/WhatsApp"
CONTACTS_FILE="$HOME/.signal-cli/contacts.json"

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
    echo '{}' > "$STATE_FILE"
fi

# Function to get last line count for a file
get_last_lines() {
    local file="$1"
    local key=$(echo "$file" | md5sum | cut -d' ' -f1)
    jq -r ".[\"$key\"] // 0" "$STATE_FILE" 2>/dev/null || echo 0
}

# Function to update last line count
set_last_lines() {
    local file="$1"
    local count="$2"
    local key=$(echo "$file" | md5sum | cut -d' ' -f1)
    local tmp=$(mktemp)
    jq ".[\"$key\"] = $count" "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
}

# Extract contact name from file path
get_contact_name() {
    local file="$1"
    basename "$file" .md
}

# Look up phone number from contacts
lookup_phone() {
    local name="$1"
    jq -r ".[] | select(.name == \"$name\") | .number" "$CONTACTS_FILE" 2>/dev/null | head -1
}

# Determine platform from file path
get_platform() {
    local file="$1"
    if [[ "$file" == *"/Signal/"* ]]; then
        echo "signal"
    elif [[ "$file" == *"/WhatsApp/"* ]]; then
        echo "whatsapp"
    else
        echo "unknown"
    fi
}

# Function to check for new incoming messages
check_file() {
    local file="$1"
    local current_lines=$(wc -l < "$file" 2>/dev/null || echo 0)
    local last_lines=$(get_last_lines "$file")
    
    if [ "$current_lines" -gt "$last_lines" ]; then
        # Get new lines
        local new_content=$(tail -n +$((last_lines + 1)) "$file")
        
        # Check for incoming messages (not from "Me:")
        local incoming=$(echo "$new_content" | grep '^\[' | grep -v '\] Me:' | grep -v '^\[.*\] (- ' | tail -5)
        
        if [ -n "$incoming" ]; then
            local contact=$(get_contact_name "$file")
            local platform=$(get_platform "$file")
            local phone=$(lookup_phone "$contact")
            
            echo "[$(date -Iseconds)] New messages in: $file (contact: $contact, platform: $platform)"
            
            # Trigger agent to draft a reply
            clawdbot agent --channel telegram --to 5996479639 --deliver --message "📨 New incoming message - draft a reply if appropriate

Platform: $platform
Contact: $contact
Phone: $phone
File: $file

Recent messages:
$incoming

If this needs a reply, draft one and send it to me on Telegram with [Send] [Skip] buttons. If it's just noise (ok, thanks, reactions), reply NO_REPLY."
        fi
        
        set_last_lines "$file" "$current_lines"
    fi
}

echo "[$(date -Iseconds)] Message watcher starting..."
echo "[$(date -Iseconds)] Watching: $SIGNAL_DIR, $WHATSAPP_DIR"
echo "[$(date -Iseconds)] Using inotifywait (real-time)"

# Use inotifywait to watch for file changes - near instant response
inotifywait -m -r -e modify,create --format '%w%f' "$SIGNAL_DIR" "$WHATSAPP_DIR" 2>/dev/null | while IFS= read -r file; do
    # Only process .md files
    if [[ "$file" == *.md ]]; then
        # Small delay to let file finish writing
        sleep 0.5
        check_file "$file"
    fi
done

#!/bin/bash
# Look up phone number by name from Signal contacts
# Usage: contacts-lookup.sh "Name"

NAME="$1"
CONTACTS_FILE="$HOME/.signal-cli/contacts.json"

if [ -z "$NAME" ]; then
    echo "Usage: contacts-lookup.sh <name>"
    exit 1
fi

jq -r ".[] | select(.name | test(\"$NAME\"; \"i\")) | .number" "$CONTACTS_FILE" 2>/dev/null | head -1

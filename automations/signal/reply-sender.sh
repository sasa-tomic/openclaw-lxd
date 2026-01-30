#!/bin/bash
# Send a reply via Signal or WhatsApp
# Usage: reply-sender.sh <platform> <recipient> <message>

PLATFORM="$1"
RECIPIENT="$2"
MESSAGE="$3"

if [ -z "$PLATFORM" ] || [ -z "$RECIPIENT" ] || [ -z "$MESSAGE" ]; then
    echo "Usage: reply-sender.sh <signal|whatsapp> <recipient> <message>"
    exit 1
fi

case "$PLATFORM" in
    signal)
        ~/homebrew/bin/signal-cli -a +41798471964 send -u "$RECIPIENT" -m "$MESSAGE"
        ;;
    whatsapp)
        wacli send text --to "$RECIPIENT" --message "$MESSAGE"
        ;;
    *)
        echo "Unknown platform: $PLATFORM"
        exit 1
        ;;
esac

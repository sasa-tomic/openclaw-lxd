#!/usr/bin/env python3
"""Cron Wrapper - Runs heartbeat automation scripts and sends results to Telegram.

Usage: python cron_wrapper.py <script_name>

Features:
- Proper exit code propagation (exits with script's exit code)
- Telegram notifications with chunking for long messages
- Never silently swallows failures
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)
from lib.config import TELEGRAM_TARGET
from lib.telegram_utils import send_telegram, chunk_message

MAX_CHUNK = 3500


def format_message(script_name: str, content: str) -> str:
    """Format the result content based on its type."""
    if content.startswith("OBSERVATIONS:"):
        body = content[len("OBSERVATIONS:") :]
        return f"📝 **Obsidian Note Review**\n{body}"
    elif content.startswith("IMPORTANT_EMAILS:"):
        body = content[len("IMPORTANT_EMAILS:") :]
        return f"📧 **Important Emails**\n{body}"
    elif content.startswith("MAINTENANCE:"):
        body = content[len("MAINTENANCE:") :]
        return f"🗂️ **Obsidian Maintenance**\n{body}"
    elif content.startswith("TWITTER_MORNING:"):
        body = content[len("TWITTER_MORNING:") :]
        return f"🐦 **Twitter Morning Research**\n{body}\n\nReady to post? Approve with A/B/C or edit."
    elif content.startswith("TWITTER_ENGAGEMENT:"):
        body = content[len("TWITTER_ENGAGEMENT:") :]
        return f"💬 **Twitter Engagement Check**\n{body}"
    else:
        return f"**{script_name}**\n{content}"


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    if len(sys.argv) < 2:
        logger.error("Usage: cron_wrapper.py <script_name>")
        sys.exit(1)

    script_name = sys.argv[1]
    script_dir = Path(__file__).parent
    temp_results = Path(f"/tmp/{script_name}-results.txt")

    logger.info(f"=== CRON WRAPPER: {script_name} ===")

    # Run the Python script (prefer .py) or shell script (.sh)
    # Handle both hyphen and underscore naming conventions
    py_script = script_dir / f"{script_name.replace('-', '_')}.py"
    sh_script = script_dir / f"{script_name}.sh"

    if py_script.exists():
        script_path = py_script
    elif sh_script.exists():
        script_path = sh_script
    else:
        logger.error(f"No script found for {script_name} (.py or .sh)")
        sys.exit(127)

    # Run the script and capture exit code
    result = subprocess.run(
        [str(script_path)],
        cwd=str(script_dir),
        capture_output=False,  # Let output go to journald
        env=os.environ.copy(),
    )

    exit_code = result.returncode

    if exit_code == 0:
        logger.info("Script completed successfully")
    else:
        logger.error(f"Script failed with exit code {exit_code}")

    # Check if there are results to send
    if temp_results.exists():
        try:
            content = temp_results.read_text().strip()
            if content:
                message = format_message(script_name, content)
                chunks = chunk_message(message)

                for i, chunk in enumerate(chunks, 1):
                    if len(chunks) > 1:
                        chunk = f"(part {i}/{len(chunks)})\n{chunk}"
                    if not send_telegram(chunk):
                        logger.error(f"Failed to send chunk {i}/{len(chunks)}")
            else:
                logger.info("Empty results file, nothing to send")
        except Exception as e:
            logger.error(f"Error reading results file: {e}")
        finally:
            temp_results.unlink(missing_ok=True)
    else:
        logger.info("No results to send")

    logger.info("Cron wrapper complete.")

    # CRITICAL: Exit with the script's exit code so systemd knows about failures
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

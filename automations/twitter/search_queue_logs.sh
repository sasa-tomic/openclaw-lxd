#!/bin/sh

RUN_ID=$(uv run prefect flow-run ls --flow-name twitter-search-queue --state-type COMPLETED --limit 1 -o json | sed -n 's/^[[:space:]]*"id": "\([^"]*\)",$/\1/p' | head -n1) && uv run prefect flow-run logs -t -n 300 "$RUN_ID"

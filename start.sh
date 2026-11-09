#!/bin/zsh
# Run ALI AI locally (on-device image generation). Keeps the Mac awake while it runs.
# Stop the Railway/cloud copy first: Telegram allows only one running instance per bot token.
cd "$(dirname "$0")"
exec caffeinate -i venv/bin/python bot.py

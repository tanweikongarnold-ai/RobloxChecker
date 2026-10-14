#!/usr/bin/env bash
# Double-click this file on a Mac (or run "bash start.command" on Linux) to start the bot.
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo
  echo "Python 3.10 or newer is needed and wasn't found."
  echo "Download it from https://www.python.org/downloads/ , install it, then double-click start.command again."
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time. This takes a minute or two..."
  "$PY" -m venv .venv || { read -r -p "Setup failed. Press Enter to close."; exit 1; }
fi
echo "Checking everything is installed..."
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
  || { read -r -p "Setup failed. Check your internet and try again. Press Enter to close."; exit 1; }

.venv/bin/python -m bot
echo
read -r -p "The bot has stopped. Press Enter to close."

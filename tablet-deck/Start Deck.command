#!/bin/bash
# Double-click this on the Mac to start Mizan Deck.
cd "$(dirname "$0")" || exit 1
if ! /usr/bin/python3 --version >/dev/null 2>&1; then
  echo "Python isn't set up yet. A window should appear offering to install the"
  echo "Command Line Tools: click Install, wait for it to finish, then double-click"
  echo "Start Deck again."
  read -r -p "Press Enter to close."
  exit 1
fi
/usr/bin/python3 deck.py
read -r -p "Mizan Deck stopped. Press Enter to close."

#!/bin/bash
# Roster installer for macOS. Safe to run again to update.
# Also upgrades an older install made under the previous name (Agent Huddle).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.roster"
OLD="$HOME/.agent-huddle"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/com.roster.widget.plist"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Run:  xcode-select --install   then run this script again."
  exit 1
fi

mkdir -p "$DEST/sessions"
echo "Building the widget..."
if ! swiftc -O "$SRC/main.swift" "$SRC/hotkeys.swift" -o "$DEST/Roster" 2>"$DEST/build.log"; then
  echo "Keyboard shortcuts could not be built (details in ~/.roster/build.log); building without them..."
  swiftc -O "$SRC/main.swift" "$SRC/hotkeys_stub.swift" -o "$DEST/Roster"
fi
cp "$SRC/hook.py" "$SRC/widget.html" "$DEST/"

# Upgrade from Agent Huddle: keep settings, and leave a forwarder so sessions that are
# already open (they still call the old path) keep working and keep showing up.
if [ -d "$OLD" ]; then
  [ -f "$OLD/config.json" ] && [ ! -f "$DEST/config.json" ] && cp -p "$OLD/config.json" "$DEST/config.json"
  cat > "$OLD/hook.py" <<'PY'
import os, sys
new = os.path.join(os.path.expanduser("~"), ".roster", "hook.py")
if os.path.exists(new):
    os.execv("/usr/bin/python3", ["python3", new] + sys.argv[1:])
sys.exit(0)
PY
  rm -f "$AGENTS/com.agenthuddle.widget.plist"
  echo "Upgraded from Agent Huddle. Once you have restarted your Claude Code sessions you can delete ~/.agent-huddle"
fi

echo "Adding hooks to Claude Code..."
/usr/bin/python3 "$SRC/setup_hooks.py" install

# Start at login.
mkdir -p "$AGENTS"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.roster.widget</string>
  <key>ProgramArguments</key><array><string>$DEST/Roster</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL

pkill -x AgentHuddle 2>/dev/null || true
pkill -x Roster 2>/dev/null || true
nohup "$DEST/Roster" >/dev/null 2>&1 &

echo
echo "Done. Roster is in the top-right corner."
echo "Sessions appear when you start a NEW Claude Code session (already-open ones need a restart)."
echo "Show/hide it from the people icon in the menu bar, or with Ctrl+Option+H."
echo "Ctrl+Option+A allows the permission request that is waiting."
echo "Phone alerts (Telegram): see the README, then run"
echo "  python3 ~/.roster/hook.py phone setup <bot token>"

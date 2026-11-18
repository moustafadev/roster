#!/bin/bash
# Removes Roster: the hooks, the login item and the widget.
SRC="$(cd "$(dirname "$0")" && pwd)"
pkill -x Roster 2>/dev/null || true
pkill -x AgentHuddle 2>/dev/null || true
/usr/bin/python3 "$SRC/setup_hooks.py" uninstall
rm -f "$HOME/Library/LaunchAgents/com.roster.widget.plist" "$HOME/Library/LaunchAgents/com.agenthuddle.widget.plist"

# Sessions that are still open keep calling the hook file until they are restarted,
# so replace it with one that does nothing instead of deleting it.
for DIR in "$HOME/.roster" "$HOME/.agent-huddle"; do
  if [ -d "$DIR" ]; then
    printf 'import sys\nsys.exit(0)\n' > "$DIR/hook.py"
    rm -f "$DIR/Roster" "$DIR/AgentHuddle" "$DIR/widget.html" "$DIR/build.log"
  fi
done
echo "Roster removed. After restarting your Claude Code sessions you can delete ~/.roster"

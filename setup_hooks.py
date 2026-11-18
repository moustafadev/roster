#!/usr/bin/python3
"""Add (or remove) the Roster hooks in ~/.claude/settings.json.

Usage: setup_hooks.py install | uninstall
Existing settings and other hooks are kept; a backup is written first.
"""
import json
import os
import shutil
import sys

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MARKS = (".roster/hook.py", ".agent-huddle/hook.py")  # the second is the old name, removed on upgrade
# "|| true": if the hook file is ever missing, sessions that are already open carry on normally.
COMMAND = '/usr/bin/python3 "$HOME/.roster/hook.py" 2>/dev/null || true'
EVENTS = {  # event -> run in the background?
    "SessionStart": False, "UserPromptSubmit": False, "PreToolUse": True,
    "PostToolUse": False, "Notification": False, "Stop": False, "SessionEnd": False,
    "PermissionRequest": False,
}
TIMEOUTS = {"PermissionRequest": 620}  # stays open while you decide in the widget


def strip(hooks):
    for event in list(hooks.keys()):
        groups = []
        for group in hooks.get(event) or []:
            if isinstance(group, dict):
                group["hooks"] = [h for h in group.get("hooks") or []
                                  if not any(m in str(h.get("command", "")) for m in MARKS)]
                if not group["hooks"]:
                    continue
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "install"
    settings = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            settings = json.load(f)  # stop here if the file is not valid JSON
        backup = SETTINGS + ".roster-backup"
        if not os.path.exists(backup):  # keep the oldest copy
            shutil.copy2(SETTINGS, backup)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    strip(hooks)
    if mode == "install":
        for event, background in EVENTS.items():
            entry = {"type": "command", "command": COMMAND, "timeout": TIMEOUTS.get(event, 10)}
            if background:
                entry["async"] = True
            hooks.setdefault(event, []).append({"hooks": [entry]})
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    print("hooks %sed in %s" % (mode, SETTINGS))


if __name__ == "__main__":
    main()

# Roster

A small floating widget for macOS that shows every running Claude Code session:
the **project and branch**, what you asked, what it is doing right now, how long
it has been at it, tokens and estimated cost, files changed, and how far through
its task list it is. It chimes when a session needs you, and you can answer
permission prompts and send replies straight from the widget.

## Install or update

    cd roster
    ./install.sh

Needs the Xcode command line tools (`xcode-select --install`).
After an update, restart open Claude Code sessions so they load the new hooks.

## What you can do

| Where | What |
| --- | --- |
| Card | Click to jump to that session. |
| **Allow / Always / Deny** | Answer a permission prompt. *Always* also saves the rule Claude Code suggests, so it stops asking for that kind of command. It only appears when Claude Code offers such a rule. |
| Speech-bubble button | Quick reply. On a permission prompt it means "do this instead". While the session is working, the message is handed over at its next step. When the session is idle: typed in directly for Terminal and iTerm2; for editors (Cursor, VS Code) it is copied and the session is brought forward so you can paste it. |
| Clock button | Today's finished work: time, project, branch, how long it took, lines changed. |
| Menu bar icon | Session count (`!` when one needs you), show/hide, quit. |
| `Ctrl+Option+A` | Allow the permission request that has waited longest. |
| `Ctrl+Option+H` | Show / hide the widget. |

Other behaviour:

- Sessions of the same project are grouped under one heading.
- Each project has its own "needs you" chime; a pop means a session finished.
- A card turns amber with "Quiet for 6m" when a working session has shown no activity for 5 minutes.
- Cost is an **estimate at API list prices**. On a subscription you are not billed per token.
  Prices live at the top of `hook.py`; override them with a `"prices"` object in `~/.roster/config.json`.
- Lines changed count the session's own edits (Edit/Write), not changes made by shell commands.

## Phone alerts (Telegram)

Alerts go to your own Telegram account through a bot only you control, and only
when you have been away from the Mac for 2 minutes (no mouse or keyboard).

1. In Telegram, open **@BotFather**, send `/newbot`, follow the steps, copy the token.
2. Open your new bot and send it any message.
3. Run: `python3 ~/.roster/hook.py phone setup <token>`

You get a test message. Later: `hook.py phone off`, `phone on`, `phone test`.
The alert contains the project name and the prompt title or the tool being asked about.
The token is stored in `~/.roster/config.json` (readable only by you).
Change the away time with `"away_seconds"` in that file.

## How it works

| File | Job |
| --- | --- |
| `hook.py` | Claude Code runs it on every event. It writes one JSON file per session to `~/.roster/sessions/`, answers permission prompts for the widget, and sends phone alerts. |
| `main.swift`, `hotkeys.swift` | The floating window, menu bar icon and global shortcuts. |
| `widget.html` | The look of the widget. Open it in a browser with `?demo` to preview while you restyle it. |
| `setup_hooks.py` | Adds/removes the hooks in `~/.claude/settings.json` (a backup is kept as `settings.json.roster-backup`). |

Remove everything with `./uninstall.sh`.

Roster was previously called Agent Huddle; `./install.sh` upgrades an old install in place.

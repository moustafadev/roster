#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Roster hook.

Claude Code runs this on every lifecycle event and passes the event as JSON on
stdin. We keep one small state file per session in ~/.roster/sessions/,
which the floating widget reads once a second.

It is also the widget's helper:
    hook.py focus|close|allow|always|deny <session_id>
    hook.py reply <session_id> <text>
    hook.py phone setup <bot token> | on | off | test

Permission prompts: on PermissionRequest the hook publishes the request to the
widget and waits. If you answer in the widget, the hook answers Claude Code. If
you answer in Claude Code itself, the hook notices and quietly exits.

Rules: as a hook it prints nothing except a deliberate answer to Claude Code,
never fails loudly, and always exits 0 -- it must not be able to disturb the
session it is watching.
"""
import difflib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), ".roster")
SESSIONS = os.path.join(BASE, "sessions")
CONFIG = os.path.join(BASE, "config.json")
HISTORY = os.path.join(SESSIONS, "_today.json")

WAIT_SECONDS = 600       # how long a permission request stays answerable from the widget
PENDING_GUARD = 1.5      # ignore tool events this soon after a request (parallel tool calls)
NO_BUTTON_TOOLS = ("AskUserQuestion", "ExitPlanMode")  # need a real answer, not just Allow
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
WAITING_TYPES = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")

TERM_BUNDLES = {
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "vscode": "com.microsoft.VSCode",
    "WarpTerminal": "dev.warp.Warp-Stable",
    "ghostty": "com.mitchellh.ghostty",
}
# Editors that focus the right window when asked to "open" the project folder.
EDITOR_HINTS = ("vscode", "cursor", "todesktop", "windsurf", "antigravity", "zed", "vscodium")

# API list prices in USD per million tokens: (input, output). First match wins.
# Cache writes are billed at 1.25x input and cache reads at 0.1x input.
# This is only an estimate; on a subscription plan you are not billed per token.
# Override or extend with a "prices" object in config.json.
PRICES = [
    ("fable-5-2", 8, 40), ("mythos-5-2", 8, 40), ("fable-5", 10, 50), ("mythos-5", 10, 50),
    ("mythos", 10, 50), ("opus-5-5", 4, 20), ("opus-5", 5, 25), ("opus-4", 5, 25),
    ("sonnet-5", 2, 10), ("sonnet-4", 3, 15), ("haiku-5", 0.10, 0.50), ("haiku-4", 1, 5),
]


# -------------------------------------------------------------------- helpers
def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def safe_id(session_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))[:120]


def state_path(session_id):
    return os.path.join(SESSIONS, safe_id(session_id) + ".json")


def decision_path(session_id):
    return os.path.join(SESSIONS, safe_id(session_id) + ".decision")


def inbox_path(session_id):
    return os.path.join(SESSIONS, safe_id(session_id) + ".inbox")


def remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class locked(object):
    """Exclusive lock around a read-modify-write of one state file."""

    def __init__(self, path):
        self.path = path + ".lock"

    def __enter__(self):
        self.f = open(self.path, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()
        return False


def config():
    cfg = read_json(CONFIG, {})
    return cfg if isinstance(cfg, dict) else {}


def set_status(state, status, activity, now):
    if state.get("status") != status:
        state["since"] = now
    state["status"] = status
    if activity is not None:
        state["activity"] = activity


# ---------------------------------------------------------------- process info
def ps(pid, field):
    try:
        out = subprocess.run(["/bin/ps", "-o", field + "=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
        return out.strip()
    except Exception:
        return ""


def find_claude_pid():
    """Walk up from this hook to the nearest ancestor that is Claude Code."""
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        cmd = ps(pid, "command")
        if "claude" in cmd.lower() and "hook.py" not in cmd:
            return pid
        try:
            pid = int(ps(pid, "ppid") or 0)
        except ValueError:
            break
    return 0


def project_name(data):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    root = root.rstrip("/")
    return root, (os.path.basename(root) or root or "unknown")


def git_branch(start):
    """Current branch of the repo containing `start`, read straight from .git (no subprocess)."""
    d = str(start or "")
    for _ in range(15):
        if not d:
            break
        dot = os.path.join(d, ".git")
        head = ""
        if os.path.isdir(dot):
            head = os.path.join(dot, "HEAD")
        elif os.path.isfile(dot):  # worktree / submodule: ".git" is a pointer file
            try:
                with open(dot) as f:
                    line = f.read().strip()
                if line.startswith("gitdir:"):
                    target = line[7:].strip()
                    head = os.path.join(target if os.path.isabs(target) else os.path.join(d, target), "HEAD")
            except Exception:
                pass
        if head:
            try:
                with open(head) as f:
                    ref = f.read().strip()
            except Exception:
                return ""
            if ref.startswith("ref: refs/heads/"):
                return ref[16:]
            return ref[:7]  # detached HEAD
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


# ------------------------------------------------------------------- activity
def describe(tool, inp):
    inp = inp if isinstance(inp, dict) else {}
    base = lambda p: os.path.basename(str(p or "").rstrip("/")) or "file"
    if tool == "Read":
        return "Reading " + base(inp.get("file_path"))
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing " + base(inp.get("file_path") or inp.get("notebook_path"))
    if tool == "Write":
        return "Writing " + base(inp.get("file_path"))
    if tool == "Bash":
        return clip(inp.get("description") or ("Running: " + str(inp.get("command", ""))), 60)
    if tool == "Grep":
        return clip("Searching “%s”" % inp.get("pattern", ""), 60)
    if tool == "Glob":
        return clip("Finding files %s" % inp.get("pattern", ""), 60)
    if tool in ("Task", "Agent"):
        return clip("Subagent: " + str(inp.get("description") or "working"), 60)
    if tool == "WebSearch":
        return clip("Searching the web: " + str(inp.get("query", "")), 60)
    if tool == "WebFetch":
        host = re.sub(r"^https?://([^/]+).*$", r"\1", str(inp.get("url", "")))
        return clip("Fetching " + host, 60)
    if tool in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"):
        return "Planning the steps"
    if tool == "Skill":
        return clip("Using skill " + str(inp.get("skill", "")), 60)
    if tool == "AskUserQuestion":
        return "asks a question"
    if tool == "ExitPlanMode":
        return "Plan ready for review"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return clip("%s: %s" % (parts[1], parts[-1].replace("_", " ")), 60)
    return clip(tool, 60)


def detail_of(tool, inp):
    """The exact thing being approved, shown above the Allow button."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "Bash":
        return clip(inp.get("command"), 400)
    for key in ("file_path", "notebook_path", "url", "query"):
        if inp.get(key):
            return clip(inp[key], 400)
    try:
        return clip(json.dumps(inp), 400)
    except Exception:
        return ""


# ---------------------------------------------------------------------- tasks
def find_id(obj):
    """Best effort: dig the new task's id out of a TaskCreate response."""
    if isinstance(obj, dict):
        for key in ("id", "taskId", "task_id"):
            if obj.get(key) not in (None, ""):
                return str(obj[key])
        for value in obj.values():
            found = find_id(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_id(value)
            if found:
                return found
    elif isinstance(obj, str):
        m = re.search(r"#?(\d+)", obj)
        if m:
            return m.group(1)
    return ""


def update_tasks(state, tool, inp, resp):
    inp = inp if isinstance(inp, dict) else {}
    tasks = state.get("tasks") or []
    if tool == "TodoWrite":
        tasks = []
        for i, t in enumerate(inp.get("todos") or []):
            if isinstance(t, dict):
                tasks.append({"id": str(i + 1),
                              "text": clip(t.get("content"), 60),
                              "active": clip(t.get("activeForm") or t.get("content"), 60),
                              "status": t.get("status") or "pending"})
    elif tool == "TaskCreate":
        known = set(t["id"] for t in tasks)
        tid = find_id(resp)
        if not tid or tid in known:
            tid = str(max([int(t["id"]) for t in tasks if t["id"].isdigit()] or [0]) + 1)
        tasks.append({"id": tid,
                      "text": clip(inp.get("subject"), 60),
                      "active": clip(inp.get("activeForm") or inp.get("subject"), 60),
                      "status": "pending"})
    elif tool == "TaskUpdate":
        tid = str(inp.get("taskId") or inp.get("id") or "")
        status = inp.get("status")
        if status == "deleted":
            tasks = [t for t in tasks if t["id"] != tid]
        else:
            for t in tasks:
                if t["id"] == tid:
                    if status:
                        t["status"] = status
                    if inp.get("subject"):
                        t["text"] = clip(inp["subject"], 60)
                    if inp.get("activeForm"):
                        t["active"] = clip(inp["activeForm"], 60)
    else:
        return
    state["tasks"] = tasks


def summarize(state):
    tasks = state.get("tasks") or []
    state["total"] = len(tasks)
    state["done"] = len([t for t in tasks if t.get("status") == "completed"])
    current = [t for t in tasks if t.get("status") == "in_progress"]
    state["current"] = current[0].get("active") or current[0].get("text") if current else ""
    files = state.get("files") or {}
    state["files_n"] = len(files)
    state["added"] = sum(v[0] for v in files.values())
    state["removed"] = sum(v[1] for v in files.values())


# ------------------------------------------------------------ files and tokens
def count_lines(text):
    text = str(text or "")
    return len(text.splitlines()) if text else 0


def diff_counts(old, new):
    added = removed = 0
    for line in difflib.ndiff(str(old or "").splitlines(), str(new or "").splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def track_edit(state, tool, inp, resp):
    """Count files touched and lines added/removed by this session's own edits."""
    if tool not in EDIT_TOOLS or not isinstance(inp, dict):
        return
    path = str(inp.get("file_path") or inp.get("notebook_path") or "")
    if not path:
        return
    added = removed = 0
    patch = resp.get("structuredPatch") if isinstance(resp, dict) else None
    if isinstance(patch, list) and patch:
        for hunk in patch:
            for line in (hunk.get("lines") if isinstance(hunk, dict) else None) or []:
                if str(line).startswith("+"):
                    added += 1
                elif str(line).startswith("-"):
                    removed += 1
    elif tool == "Edit":
        added, removed = diff_counts(inp.get("old_string"), inp.get("new_string"))
    elif tool == "MultiEdit":
        for edit in inp.get("edits") or []:
            if isinstance(edit, dict):
                a, r = diff_counts(edit.get("old_string"), edit.get("new_string"))
                added, removed = added + a, removed + r
    elif tool == "Write":
        added = count_lines(inp.get("content"))
    files = state.get("files") or {}
    if path in files or len(files) < 300:
        current = files.get(path) or [0, 0]
        files[path] = [current[0] + added, current[1] + removed]
    state["files"] = files


def price_for(model):
    model = str(model or "").lower()
    custom = config().get("prices")
    if isinstance(custom, dict):
        for key, pair in custom.items():
            try:
                if str(key).lower() in model:
                    return float(pair[0]), float(pair[1])
            except Exception:
                pass
    for key, inp, out in PRICES:
        if key in model:
            return float(inp), float(out)
    return None


def cost_of(model, vec):
    price = price_for(model)
    if not price:
        return 0.0
    inp, out = price
    return (vec[0] * inp + vec[1] * out + vec[2] * inp * 1.25 + vec[3] * inp * 0.1) / 1e6


def read_usage(state, transcript):
    """Add up token usage from the part of the transcript we have not read yet."""
    if not transcript or not os.path.isfile(transcript):
        return
    offset = int(state.get("t_offset") or 0)
    if os.path.getsize(transcript) < offset or state.get("t_path") != transcript:
        offset = 0
        state["tokens"], state["cost"], state["t_last_id"], state["t_last"] = [0, 0, 0, 0], 0.0, "", None
    state["t_path"] = transcript
    with open(transcript, "rb") as f:
        f.seek(offset)
        chunk = f.read(8 * 1024 * 1024)
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    state["t_offset"] = offset + end + 1
    tokens = list(state.get("tokens") or [0, 0, 0, 0])
    cost = float(state.get("cost") or 0)
    last_id, last = state.get("t_last_id") or "", state.get("t_last")
    for line in chunk[:end].split(b"\n"):
        if b'"usage"' not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        usage = msg["usage"]
        vec = [int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                "cache_creation_input_tokens", "cache_read_input_tokens")]
        c = cost_of(msg.get("model"), vec)
        mid = str(msg.get("id") or "")
        # One reply is written as several lines carrying the same usage: count it once.
        if mid and mid == last_id and last:
            tokens = [t - o for t, o in zip(tokens, last[:4])]
            cost -= last[4]
        tokens = [t + v for t, v in zip(tokens, vec)]
        cost += c
        last_id, last = mid, vec + [c]
    state["tokens"], state["cost"] = tokens, round(max(cost, 0.0), 4)
    state["t_last_id"], state["t_last"] = last_id, last


# ---------------------------------------------------------- history and phone
def log_history(state, now):
    """Append the turn that just finished to today's log (shown in the widget)."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with locked(HISTORY):
        log = read_json(HISTORY, {})
        if not isinstance(log, dict) or log.get("day") != day:
            log = {"kind": "history", "day": day, "items": []}
        started = float(state.get("turn_started") or now)
        log["items"].append({
            "t": now, "project": state.get("project"), "branch": state.get("branch") or "",
            "title": state.get("title") or "", "secs": int(max(0, now - started)),
            "files": state.get("files_n") or 0, "added": state.get("added") or 0,
            "removed": state.get("removed") or 0, "cost": state.get("cost") or 0,
        })
        log["items"] = log["items"][-80:]
        write_json(HISTORY, log)


def idle_seconds():
    """Seconds since the last keyboard/mouse input on this Mac (large if unknown)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=3).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 1e9
    except Exception:
        return 1e9


def curl(url, fields=None, wait=True):
    """Call a URL with curl. The URL (it holds the bot token) goes in on stdin, not on the command line."""
    esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    lines = ['url = "%s"' % esc(url)]
    for key, value in (fields or {}).items():
        lines.append('data-urlencode = "%s=%s"' % (key, esc(value)))
    cmd = [os.environ.get("ROSTER_CURL") or "/usr/bin/curl", "-s", "-m", "10", "-K", "-"]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    if wait:
        out = subprocess.run(cmd, input=body, capture_output=True, timeout=15).stdout
        try:
            return json.loads(out.decode("utf-8", "replace"))
        except Exception:
            return {}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(body)
    p.stdin.close()
    return {}


def telegram(cfg, method):
    return "https://api.telegram.org/bot%s/%s" % (cfg.get("telegram_token"), method)


def push(text, force=False, wait=False):
    """Telegram message to your own chat, only when enabled and you are away from the Mac."""
    cfg = config()
    if not cfg.get("telegram_token") or not cfg.get("telegram_chat_id"):
        return {}
    if not force and not cfg.get("phone_alerts"):
        return {}
    if not force and idle_seconds() < float(cfg.get("away_seconds") or 120):
        return {}
    try:
        return curl(telegram(cfg, "sendMessage"),
                    {"chat_id": cfg["telegram_chat_id"], "text": text}, wait=wait)
    except Exception:
        return {}


def take_inbox(session_id):
    """Messages typed in the widget while the session was busy."""
    path = inbox_path(session_id)
    with locked(path):
        items = read_json(path, [])
        remove(path)
    return [str(i) for i in items if i] if isinstance(items, list) else []


# ---------------------------------------------------------------------- hooks
def handle(data):
    """Update the session's state. Returns (wait_info, answer_for_claude_code)."""
    session_id = data.get("session_id")
    event = data.get("hook_event_name") or ""
    if not session_id or not event:
        return None, None
    os.makedirs(SESSIONS, exist_ok=True)
    path = state_path(session_id)

    if event == "SessionEnd":
        remove(path, path + ".lock", decision_path(session_id),
               inbox_path(session_id), inbox_path(session_id) + ".lock")
        return None, None

    wait, answer, notify = None, None, ""
    with locked(path):
        state = read_json(path, {}) or {}
        now = time.time()
        if not state:
            root, name = project_name(data)
            state = {"session_id": str(session_id), "project": name, "project_dir": root,
                     "title": "", "activity": "Ready", "status": "idle", "since": now,
                     "tasks": [], "started": now, "tools": 0}
        if not state.get("pid"):
            pid = find_claude_pid()
            state["pid"] = pid
            state["tty"] = ps(pid, "tty") if pid else ""
            state["bundle"] = os.environ.get("__CFBundleIdentifier") or \
                TERM_BUNDLES.get(os.environ.get("TERM_PROGRAM", ""), "")
        state["cwd"] = data.get("cwd") or state.get("cwd") or ""
        state["branch"] = git_branch(state["cwd"] or state.get("project_dir"))
        is_sub = bool(data.get("agent_id"))
        tool = data.get("tool_name") or ""
        inp = data.get("tool_input")
        pending = state.get("pending") or {}
        settled = pending and now - float(pending.get("created") or 0) > PENDING_GUARD

        if event == "SessionStart":
            if state.get("status") != "working":
                set_status(state, "idle", "Ready", now)
        elif event == "UserPromptSubmit":
            prompt = clip(data.get("prompt"), 70)
            if prompt:
                state["title"] = prompt
            tasks = state.get("tasks") or []
            if tasks and all(t.get("status") == "completed" for t in tasks):
                state["tasks"] = []
            state["turn_started"] = now
            set_status(state, "working", "Thinking…", now)
            state.pop("pending", None)
        elif event == "PreToolUse":
            state["tools"] = int(state.get("tools") or 0) + 1
            if settled:
                state.pop("pending", None)
            if not state.get("pending"):
                set_status(state, "waiting" if tool == "AskUserQuestion" else "working",
                           describe(tool, inp), now)
        elif event == "PostToolUse":
            if not is_sub:
                update_tasks(state, tool, inp, data.get("tool_response"))
                track_edit(state, tool, inp, data.get("tool_response"))
                read_usage(state, data.get("transcript_path"))
                notes = take_inbox(session_id)
                if notes:
                    state["queued"] = 0
                    answer = {"hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "The user sent this while you were working "
                                             "(via the Roster widget): " + " / ".join(notes)}}
            if settled:
                state.pop("pending", None)
            if not state.get("pending"):
                set_status(state, "working", None, now)
        elif event == "PermissionRequest":
            question = tool == "AskUserQuestion"
            set_status(state, "waiting", "asks a question" if question
                       else clip("Allow? " + describe(tool, inp), 60), now)
            notify = "❓ %s needs you: %s" % (state.get("project"), state["activity"])
            if tool not in NO_BUTTON_TOOLS:
                suggestions = data.get("permission_suggestions")
                suggestions = suggestions if isinstance(suggestions, list) else []
                wait = {"id": "%d-%d" % (int(now * 1000), os.getpid()), "suggestions": suggestions}
                state["pending"] = {"id": wait["id"], "tool": tool, "summary": describe(tool, inp),
                                    "detail": detail_of(tool, inp), "created": now, "beat": now,
                                    "can_always": bool(suggestions)}
                remove(decision_path(session_id))
        elif event == "Notification":
            kind = data.get("notification_type") or ""
            message = str(data.get("message") or "")
            if state.get("pending"):
                pass
            elif kind in WAITING_TYPES or (not kind and "permission" in message.lower()):
                set_status(state, "waiting", "asks a question" if "question" in message.lower()
                           else clip(message or "Needs your permission", 60), now)
            elif kind == "idle_prompt" or (not kind and "waiting for your input" in message.lower()):
                if state.get("status") == "working":
                    set_status(state, "done", None, now)
                if state.get("status") != "waiting":
                    state["activity"] = "Waiting for you"
        elif event == "Stop":
            state.pop("pending", None)
            read_usage(state, data.get("transcript_path"))
            notes = take_inbox(session_id)
            if notes:
                state["queued"] = 0
                set_status(state, "working", "Reading your reply…", now)
                answer = {"decision": "block",
                          "reason": "The user sent this from the Roster widget: " + " / ".join(notes)}
            else:
                was_working = state.get("status") == "working"
                set_status(state, "done", "Finished · waiting for you", now)
                summarize(state)
                if was_working:
                    try:
                        log_history(state, now)
                    except Exception:
                        pass
                    notify = "✅ %s finished: %s" % (state.get("project"), state.get("title") or "")

        summarize(state)
        state["updated"] = now
        write_json(path, state)

    if notify:
        push(notify)
    return wait, answer


# -------------------------------------------------------- permission from widget
def touch_pending(session_id, wait_id, resolve=False):
    """Heartbeat (or clear) our pending request. False if it is no longer ours."""
    path = state_path(session_id)
    with locked(path):
        state = read_json(path)
        if not state or (state.get("pending") or {}).get("id") != wait_id:
            return False
        if resolve:
            state.pop("pending", None)
            set_status(state, "working", "Continuing…", time.time())
        else:
            state["pending"]["beat"] = time.time()
        write_json(path, state)
        return True


def wait_for_decision(session_id, wait):
    wait_id = wait["id"]
    dpath = decision_path(session_id)
    deadline = time.time() + WAIT_SECONDS
    ticks = 0
    while time.time() < deadline:
        time.sleep(0.2)
        ticks += 1
        decision = read_json(dpath)
        if isinstance(decision, dict) and decision.get("id") == wait_id:
            remove(dpath)
            touch_pending(session_id, wait_id, resolve=True)
            behavior = decision.get("behavior")
            if behavior == "always" and wait.get("suggestions"):
                out = {"behavior": "allow", "updatedPermissions": wait["suggestions"]}
            elif behavior in ("allow", "always"):
                out = {"behavior": "allow"}
            else:
                out = {"behavior": "deny",
                       "message": decision.get("message") or "Denied by the user from Roster."}
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}
        if ticks % 5 == 0:
            # Claude Code went away, or the prompt was answered there: step aside.
            if os.getppid() <= 1 or not touch_pending(session_id, wait_id):
                return None
    touch_pending(session_id, wait_id, resolve=True)
    return None


def live_pending(state):
    pending = state.get("pending") or {}
    if pending.get("id") and time.time() - float(pending.get("beat") or 0) < 4:
        return pending
    return None


def decide(session_id, behavior, message=""):
    pending = live_pending(read_json(state_path(session_id), {}) or {})
    if not pending:
        return False
    write_json(decision_path(session_id), {"id": pending["id"], "behavior": behavior, "message": message})
    return True


# -------------------------------------------------------------- widget actions
def osa(script):
    return subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10)


def tty_dev(state):
    tty = state.get("tty") or ""
    return "/dev/" + tty if tty and not tty.startswith("?") else ""


def focus(state):
    bundle = state.get("bundle") or ""
    dev = tty_dev(state)
    folder = state.get("project_dir") or state.get("cwd") or ""
    if bundle == "com.apple.Terminal" and dev:
        osa('''tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "%s" then
        set selected of t to true
        set index of w to 1
      end if
    end repeat
  end repeat
end tell''' % dev)
    elif bundle == "com.googlecode.iterm2" and dev:
        osa('''tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "%s" then
          select w
          select t
          select s
        end if
      end repeat
    end repeat
  end repeat
end tell''' % dev)
    elif bundle and folder and any(h in bundle.lower() for h in EDITOR_HINTS):
        subprocess.run(["/usr/bin/open", "-b", bundle, folder], capture_output=True, timeout=10)
    elif bundle:
        subprocess.run(["/usr/bin/open", "-b", bundle], capture_output=True, timeout=10)


def type_into_terminal(state, text):
    """Type a reply straight into the session's terminal tab. True if it worked."""
    bundle = state.get("bundle") or ""
    dev = tty_dev(state)
    if not dev:
        return False
    quoted = text.replace("\\", "\\\\").replace('"', '\\"')
    if bundle == "com.apple.Terminal":
        script = '''tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "%s" then
        do script "%s" in t
        return "ok"
      end if
    end repeat
  end repeat
end tell''' % (dev, quoted)
    elif bundle == "com.googlecode.iterm2":
        script = '''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "%s" then
          tell s to write text "%s"
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell''' % (dev, quoted)
    else:
        return False
    try:
        return osa(script).stdout.strip() == "ok"
    except Exception:
        return False


def note(session_id, text):
    path = state_path(session_id)
    with locked(path):
        state = read_json(path)
        if state:
            state["activity"] = text
            state["updated"] = time.time()
            write_json(path, state)


def reply(session_id, text):
    """Send a message to a session, by whichever route works for its current state."""
    text = " ".join(str(text or "").split())
    if not text:
        return
    path = state_path(session_id)
    state = read_json(path, {}) or {}
    if decide(session_id, "deny", text):       # answering a permission prompt: "do this instead"
        return
    if state.get("status") == "working":       # busy: hand it over at the next step
        ipath = inbox_path(session_id)
        with locked(ipath):
            items = read_json(ipath, [])
            items = items if isinstance(items, list) else []
            items.append(text)
            write_json(ipath, items)
        with locked(path):
            state = read_json(path)
            if state:
                state["queued"] = len(items)
                write_json(path, state)
        return
    if type_into_terminal(state, text):         # idle in Terminal/iTerm: type it in
        return
    try:                                        # anything else: clipboard + bring the session forward
        subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), timeout=5)
    except Exception:
        pass
    note(session_id, "Reply copied · paste it with ⌘V")
    focus(state)


def action(name, session_id):
    path = state_path(session_id)
    state = read_json(path, {}) or {}
    if name == "focus":
        focus(state)
    elif name == "close":
        pid = int(state.get("pid") or 0)
        if pid > 1:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        remove(path, path + ".lock", decision_path(session_id),
               inbox_path(session_id), inbox_path(session_id) + ".lock")


def save_config(cfg):
    os.makedirs(BASE, exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG, 0o600)  # holds the bot token


def phone(mode, token=""):
    """hook.py phone setup <bot token> | on | off | test  -- alerts through your own Telegram bot."""
    cfg = config()
    if mode == "setup":
        token = token.strip()
        if not token:
            print("Usage: hook.py phone setup <bot token from @BotFather>")
            return
        cfg["telegram_token"] = token
        me = curl(telegram(cfg, "getMe"))
        if not me.get("ok"):
            print("Telegram did not accept that token. Copy it again from @BotFather.")
            return
        bot = (me.get("result") or {}).get("username") or "your bot"
        updates = curl(telegram(cfg, "getUpdates")).get("result") or []
        chats = [u["message"]["chat"] for u in updates
                 if isinstance(u, dict) and isinstance(u.get("message"), dict)
                 and (u["message"].get("chat") or {}).get("type") == "private"]
        if not chats:
            print("Bot @%s is fine, but it has no message from you yet." % bot)
            print("Open Telegram on your phone, send any message to @%s, then run this command again." % bot)
            return
        chat = chats[-1]
        cfg["telegram_chat_id"] = chat.get("id")
        cfg["phone_alerts"] = True
        save_config(cfg)
        print("Linked to the Telegram account: %s" % (chat.get("first_name") or chat.get("username") or chat.get("id")))
        sent = push("Roster is connected to this phone.", force=True, wait=True)
        print("Test message sent." if sent.get("ok") else "Linked, but the test message failed.")
        return
    if not cfg.get("telegram_chat_id"):
        print("Not linked yet. Run:  python3 ~/.roster/hook.py phone setup <bot token>")
        return
    if mode in ("on", "off"):
        cfg["phone_alerts"] = mode == "on"
        save_config(cfg)
    print("Phone alerts: %s" % ("ON" if cfg.get("phone_alerts") else "OFF"))
    if mode == "test":
        sent = push("Test from Roster.", force=True, wait=True)
        print("Test message sent." if sent.get("ok") else "The test message failed.")


def main():
    try:
        args = sys.argv[1:]
        if len(args) >= 2 and args[0] == "phone" and args[1] in ("setup", "on", "off", "test"):
            phone(args[1], args[2] if len(args) > 2 else "")
        elif len(args) >= 2 and args[0] in ("allow", "always", "deny"):
            decide(args[1], args[0])
        elif len(args) >= 3 and args[0] == "reply":
            reply(args[1], " ".join(args[2:]))
        elif len(args) >= 2 and args[0] in ("focus", "close"):
            action(args[0], args[1])
        elif not args:
            data = json.load(sys.stdin)
            wait, answer = handle(data)
            if wait:
                answer = wait_for_decision(data.get("session_id"), wait)
            if answer:
                sys.stdout.write(json.dumps(answer))
                sys.stdout.flush()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()

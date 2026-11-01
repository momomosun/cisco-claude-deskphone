#!/usr/bin/env python3
"""desk-relay (Mac Studio): lets the desk phone see and talk to the sessions open in the Claude
desktop app. phone-hub on the Mini calls it, signed like the hooks (hubauth), from the Mini only:

  /sessions  desktop sessions with a live process, newest first: title, busy/idle, ids
             (Claude Code's registry, ~/.claude/sessions/<pid>.json)
  /view      the last few things said in one session (its transcript under ~/.claude/projects)
  /send      put the text in the session's prompt box and press Send (desk_ax.js, through the
             macOS accessibility API: no keystrokes, no focus change, and only after checking a
             window shows that session; it opens the session's claude:// link only when no
             window does). A window shows a session only by its title, so the title must name
             one session: no other running session and none of the desktop app's own records
             (~/Library/Application Support/Claude/claude-code-sessions, archived ones too) may
             share it. Refuses a busy session, and a send that waited LOCK_WAIT seconds behind
             another; the script refuses an unsent draft. When
             the script was cut off and may have pressed Send, the words count as sent once they
             show up in the transcript. The element paths a send used are kept per session and
             tried first next time.
  /focus     bring the session running a transcript (session_id) to the front: GO on the phone
             after that session finished its turn. desk_focus.js opens its claude:// link, which
             raises the desktop app and shows the session; not while the screen is locked (the link
             would switch the main window behind the lock screen)
  /reply     whether the sent text landed, what the session said since, and whether its turn
             has ended (the Stop hook summary that closes every turn)

The desktop app has no API for any of this. The registry and transcripts are Claude Code's own
files, and the text goes in through the app's own prompt box, so the session takes it as the
user's own message. Answers are signed for the request they answer (hubauth).
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hubauth

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8091
CLIENTS = {"192.168.1.10", "127.0.0.1"}  # phone-hub on the Mini
TOKEN_FILE = os.path.expanduser("~/.claude/phone-hub.token")
REGISTRY = os.path.expanduser("~/.claude/sessions")
PROJECTS = os.path.expanduser("~/.claude/projects")
APP_SESSIONS = os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions")
AX_SCRIPT = os.path.join(HERE, "desk_ax.js")
FOCUS_SCRIPT = os.path.join(HERE, "desk_focus.js")
FOCUS_TIMEOUT = 10
MAX_BODY = 64 * 1024
MAX_TEXT = 2000
VIEW_TURNS = 6
TAIL_BYTES = 2_000_000
AX_TIMEOUT = 45
LAND_WAIT = 2  # seconds to look for the words in the transcript after an uncertain send
LOCK_WAIT = 10  # seconds a send may wait behind another; later it would arrive unexpectedly late
RECORD_RETRY = 0.1  # the app may be writing a session record just then: read it again after this
END_OF_TURN = {"stop_hook_summary", "turn_duration"}
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def live_sessions(registry=REGISTRY, alive=pid_alive):
    """Desktop sessions whose process is running, newest first."""
    found = {}
    for path in glob.glob(os.path.join(registry, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue  # gone or half written
        host = d.get("hostSessionId")
        if (d.get("entrypoint") != "claude-desktop" or not isinstance(host, str) or not host.startswith("local_")
                or not alive(d.get("pid"))):
            continue
        entry = {"id": host, "session_id": str(d.get("sessionId") or ""), "title": str(d.get("name") or ""),
                 "status": str(d.get("status") or ""), "cwd": str(d.get("cwd") or ""),
                 "updated": int(d.get("updatedAt") or 0)}
        if host not in found or entry["updated"] > found[host]["updated"]:
            found[host] = entry
    return sorted(found.values(), key=lambda e: -e["updated"])


def app_titles(root=APP_SESSIONS):
    """(id, title) of every session the desktop app has a record of, running or not. Raises
    OSError or ValueError when the records cannot all be read: then no title is known unique."""
    paths = glob.glob(os.path.join(root, "*", "*", "local_*.json"))
    if not paths:
        raise OSError(f"no session records under {root}")
    found = []
    for path in paths:
        for attempt in range(2):
            try:
                with open(path) as f:
                    d = json.load(f)
                break
            except FileNotFoundError:
                d = None  # deleted since the listing: no window can show it
                break
            except ValueError:
                if attempt:
                    raise
                time.sleep(RECORD_RETRY)  # the app may be writing it
        if d is not None:
            found.append((str(d.get("sessionId") or ""), str(d.get("title") or "").strip()))
    return found


def transcript(session_id, projects=PROJECTS):
    if not SESSION_ID.fullmatch(session_id or ""):
        return None  # only ever a session id, never a path
    hits = glob.glob(os.path.join(projects, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def parse(data):
    records = []
    for line in data.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # the line the app is still writing
    return records


def read_records(path, offset=0):
    with open(path, "rb") as f:
        f.seek(offset)
        return parse(f.read())


def tail_records(path, nbytes=TAIL_BYTES):
    start = max(0, os.path.getsize(path) - nbytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    return parse(data.split(b"\n", 1)[-1] if start else data)


def text_of(record):
    """What a person or Claude said in a record; "" for tool traffic and app-made wrappers."""
    content = (record.get("message") or {}).get("content")
    if record.get("type") == "user":
        if record.get("isMeta"):
            return ""
        if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
            content = "".join(b.get("text") or "" for b in content)
        if not isinstance(content, str):
            return ""  # tool results
        content = content.strip()
        return "" if content.startswith("<") else content  # slash commands, system wrappers
    if record.get("type") == "assistant" and isinstance(content, list):
        return "".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def recent_turns(path, limit=VIEW_TURNS):
    turns = []
    for record in tail_records(path):
        text = text_of(record)
        if text:
            turns.append({"role": record["type"], "text": text})
    return turns[-limit:]


def reply_since(path, offset, sent):
    """After a send: did our text land, what did the session say since, and is its turn over."""
    landed, texts, ended = False, [], False
    for record in read_records(path, offset):
        kind = record.get("type")
        if not landed:
            landed = kind == "user" and text_of(record) == sent.strip()
        elif kind == "assistant":
            text = text_of(record)
            if text:
                texts.append(text)
        elif (kind == "system" and record.get("subtype") in END_OF_TURN) or (kind == "user" and text_of(record)):
            ended = True  # the turn's Stop summary, or someone wrote again after us: the rest is not ours
            break
    return {"landed": landed, "texts": texts, "ended": ended}


def run_ax(host_id, title, text, hints):
    """(outcome, hints): desk_ax.js prints SENT or "ABORT: <reason>" (having sent nothing), then
    the element paths it used when it sent."""
    try:
        out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", AX_SCRIPT, title, text, host_id,
                              json.dumps(hints or {})], capture_output=True, text=True, timeout=AX_TIMEOUT)
    except subprocess.TimeoutExpired:  # killed at any point, perhaps after pressing Send
        return "UNKNOWN: the desktop app did not answer in time", None
    lines = out.stdout.strip().splitlines()
    if not lines:
        return "ABORT: " + (out.stderr.strip()[-200:] or "no output"), None
    try:
        found = json.loads(lines[1]) if len(lines) > 1 else None
    except ValueError:
        found = None
    return lines[0], found if isinstance(found, dict) else None


def run_focus(host_id):
    """desk_focus.js prints OPENED, or LOCKED having done nothing."""
    try:
        out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", FOCUS_SCRIPT, host_id],
                             capture_output=True, text=True, timeout=FOCUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "the desktop app did not answer in time"
    return out.stdout.strip() or out.stderr.strip()[-200:] or "no output"


def settle(path, offset, text, outcome):
    """After an uncertain send: SENT if the words show up in the transcript within LAND_WAIT."""
    deadline = time.time() + LAND_WAIT
    while not reply_since(path, offset, text)["landed"]:
        if time.time() >= deadline:
            return outcome
        time.sleep(0.2)
    log(f"the words landed despite {outcome!r}")
    return "SENT"


class Relay:
    def __init__(self, registry=REGISTRY, projects=PROJECTS, ax=run_ax, app_sessions=APP_SESSIONS, focus=run_focus):
        self.registry, self.projects, self.ax, self.app_sessions = registry, projects, ax, app_sessions
        self.focus_run = focus
        self.hints = {}  # session id -> element paths of its last send
        self.lock = threading.Lock()  # one send at a time: the app has one prompt box per window

    def sessions(self):
        return live_sessions(self.registry)

    def find(self, host_id):
        return next((s for s in self.sessions() if s["id"] == host_id), None)

    def view(self, host_id):
        entry = self.find(host_id)
        path = entry and transcript(entry["session_id"], self.projects)
        if not path:
            return {"error": "no such session"}
        return {"title": entry["title"], "status": entry["status"], "turns": recent_turns(path)}

    def send(self, host_id, text):
        text = str(text or "").strip()
        if not self.lock.acquire(timeout=LOCK_WAIT):
            return {"ok": False, "error": "busy"}
        try:  # checked under the lock: a send waiting here may find the session busy by now
            sessions = self.sessions()
            entry = next((s for s in sessions if s["id"] == host_id), None)
            path = entry and transcript(entry["session_id"], self.projects)
            title = entry["title"].strip() if entry else ""
            try:
                records = app_titles(self.app_sessions)
            except (OSError, ValueError) as exc:
                log(f"session records: {exc}")
                records = None
            others = ([s["title"].strip() for s in sessions if s["id"] != host_id]
                      + [t for sid, t in records or () if sid != host_id])
            error = ("no such session" if not path
                     else "titles unknown" if records is None or host_id not in {sid for sid, _ in records}
                     else "unclear title" if not title or title in others
                     else "busy" if entry["status"] == "busy"
                     else "empty" if not text else "too long" if len(text) > MAX_TEXT else "")
            if error:
                return {"ok": False, "error": error}
            offset = os.path.getsize(path)
            outcome, hints = self.ax(host_id, entry["title"], text, self.hints.get(host_id))
            if hints:
                self.hints[host_id] = hints
            if outcome.startswith("UNKNOWN"):
                outcome = settle(path, offset, text, outcome)
        finally:
            self.lock.release()
        ok = outcome == "SENT"
        log(f"send to {entry['title']!r}: {outcome}")
        return {"ok": ok, "error": "" if ok else outcome, "offset": offset, "session_id": entry["session_id"],
                "title": entry["title"]}

    def focus(self, session_id):
        entry = session_id and next((s for s in self.sessions() if s["session_id"] == session_id), None)
        if not entry:
            return {"ok": False, "error": "no such session"}
        outcome = self.focus_run(entry["id"])
        log(f"focus {entry['title']!r}: {outcome}")
        if outcome == "OPENED":
            return {"ok": True, "error": ""}
        return {"ok": False, "error": "locked" if outcome == "LOCKED" else outcome}

    def reply(self, host_id, offset, sent):
        entry = self.find(host_id)
        path = entry and transcript(entry["session_id"], self.projects)
        if not path:
            return {"error": "no such session"}
        if not isinstance(offset, int) or not 0 <= offset <= os.path.getsize(path):
            return {"error": "bad offset"}
        return dict(reply_since(path, offset, str(sent or "")), status=entry["status"])


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(relay, key):
    nonces = hubauth.NonceCache()
    routes = {
        "/sessions": lambda p: {"sessions": relay.sessions()},
        "/view": lambda p: relay.view(str(p.get("id") or "")),
        "/send": lambda p: relay.send(str(p.get("id") or ""), p.get("text")),
        "/focus": lambda p: relay.focus(str(p.get("session_id") or "")),
        "/reply": lambda p: relay.reply(str(p.get("id") or ""), p.get("offset"), p.get("sent")),
    }

    class Handler(BaseHTTPRequestHandler):
        timeout = 15

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload, nonce=None):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if nonce:
                self.send_header("X-Hub-Answer", hubauth.sign(key, nonce, data))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.client_address[0] not in CLIENTS:
                return self._send(403, {"error": "forbidden"})
            path = urllib.parse.urlparse(self.path).path
            if path not in routes:
                return self._send(404, {"error": "not found"})
            if not all(self.headers.get(h) for h in ("X-Hub-Time", "X-Hub-Nonce", "X-Hub-Signature")):
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self._send(413, {"error": "bad size"})
            body = self.rfile.read(length)
            if not hubauth.verify(key, self.headers, path, body, nonces):
                return self._send(401, {"error": "unauthorized"})
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return self._send(400, {"error": "bad json"})
            return self._send(200, routes[path](payload), nonce=self.headers.get("X-Hub-Nonce"))

    return Handler


def main():
    with open(TOKEN_FILE) as f:
        key = f.read().strip()
    server = RelayServer(("0.0.0.0", PORT), make_handler(Relay(), key))
    log(f"desk-relay on :{PORT} for {sorted(CLIENTS)}")
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

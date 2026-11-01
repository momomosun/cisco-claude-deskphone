#!/usr/bin/env python3
"""phone-hub: shows Claude Code on the Cisco 7970 and lets the phone approve permission prompts.

Claude Code hooks (claude_hooks/phone_hook.py, any Mac on the LAN) POST here:
  /hook/permission  PermissionRequest payload; in "away" mode the request is pushed to the
                    phone and this call waits up to WAIT_SECONDS for Allow/Deny. Returns
                    {"decision": "allow" | "deny" | null, "sig": ...}; null means "ask on the
                    computer".
  /hook/event       Notification / Stop payloads; they update the status panel. Where the desk relay
                    is set up, a Stop (a session finished its turn) also comes up on the phone like a
                    warning (ringing only in AWAY mode), with GO: the Studio brings that session to
                    the front. Not for the session mo is looking at (on_screen) or a phone call.
Any program on the LAN that holds the same key may post a message (phone_notify.py):
  /notify           {"level": "warning" | "info", "title", "text"} -> {"ok", "id"}. A warning comes
                    up on the phone at once, rings, and stays up (in the panel's place) until
                    OK is pressed, for WARNING_HOLD at most; an info message waits under Messages in the
                    services menu. The last MAX_MESSAGES are kept (in memory only).
A permission request marked phone_call comes from a Claude that mo is talking to over the phone
(voice_agent.py, dial 300): it waits for the phone whatever the mode, and voice_agent on this
machine may answer it from the keypad through two loopback-only routes:
  /local/pending    {"session_id"} -> that call's open requests, to read out (Bash with the
                    exact command, so the agent can tell whether it can read it in full)
  /local/decide     {"id", "session_id", "decision"} -> {"ok": bool}; first answer wins.
                    Only requests marked phone_call: the keypad never answers a desk session.
The services menu also lists the sessions open in the Claude desktop app on the Mac Studio
(desk_gateway -> desk_relay.py there): the phone shows one, and a call to 300 then talks to it;
voice_agent reaches it through /local/desk/{target,send,reply,end}, and while a call talks to a
desk session its permission requests are held for the phone like phone_call ones.
Hook requests and permission answers are HMAC-signed with a key both ends hold (the key never
goes over the wire), with a timestamp and a one-time nonce, so someone on the LAN can delay or
drop them but not forge a request or an "allow". The local routes are signed the same way.
The phone (and only the phone's IP) GETs /phone/... pages: Cisco XML objects whose text is
drawn as PNG by phone_render, because the 7970 font has no Chinese. Each push to the phone uses
a fresh one-time password, so a password sniffed from the phone's auth callback is already spent.
Requests whose full content cannot be shown on the phone are never sent there.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

import desk_gateway
import ha_door
import phone_render
import render

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("HUB_PORT", "8090"))
BASE_URL = os.environ.get("HUB_BASE_URL", f"http://192.168.1.10:{PORT}")
PHONE_IP = os.environ.get("PHONE_IP", "192.168.1.101")
HOOK_NET = ipaddress.ip_network(os.environ.get("HOOK_NET", "192.168.1.0/24"))
LOOPBACK = ipaddress.ip_network("127.0.0.0/8")  # hooks and voice_agent on this machine
WAIT_SECONDS = float(os.environ.get("WAIT_SECONDS", "600"))  # a request on the phone; the computer asks meanwhile
PUSH_USER = "claude"
MAX_PENDING = 20
MAX_BODY = 1 << 20
MAX_SHOT = 1 << 20  # a 7970 screenshot is a few hundred KB
MAX_CONNECTIONS = 48
SOCKET_TIMEOUT = 15
REQUEST_DEADLINE = 20  # whole request line + headers + body, against trickling clients
PUSH_DEADLINE = 8
BACK_DELAY = 5  # seconds the "Answered" page stays up before the phone goes back to what it showed before
BACK_RECENT = 60  # a page loaded this recently is taken to be still on screen (the panel reloads every 20 s)
RING_FILE = "ClaudeRing.raw"  # in tftpboot (make_ring.py)
DOOR_CHIME = "DoorChime.raw"
CHIME_WINDOW = 30  # seconds after a door alert in which the picture reaching the phone chimes
RING_DELAY = 1.0  # seconds after the approval picture is served: a ring that reaches the phone while
                  # it is still loading the page stops the page from showing ("File Not Found")
MAX_MESSAGES = 30
MESSAGE_TITLE = 60  # characters kept of a posted message's title and text
MESSAGE_TEXT = 1000
WARNING_HOLD = 3600  # an unacknowledged warning takes the panel's place this long (the idle panel would cover it)
RING_WINDOW = 30  # a request rings only in its first seconds, then waits quietly on the screen
HOOK_POLL = 1.0  # how often a waiting request checks that its hook is still there
SIG_WINDOW = 120
SESSION_LIMIT = 6
# Tools whose effect is in content the phone cannot show (file bodies, diffs): computer only.
COMPUTER_ONLY_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Bash fields the phone shows or that do not change what runs; anything else (e.g.
# dangerouslyDisableSandbox, run_in_background) sends the request to the computer.
BASH_FIELDS = {"command", "description", "timeout"}
REQUEST_FOOTER = "Allow 批准 / Deny 拒绝"
# The phone's Exit goes back to the last page that had not expired when it was loaded, from cache:
# a request's pages come already expired, so an answered question never comes back on Exit.
OFF_HISTORY = ("Expires", "Thu, 01 Jan 1970 00:00:00 GMT")
DESK_ID = re.compile(r"local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
GO_ERRORS = {"locked": "电脑锁屏了，没打开", "no such session": "这个会话已经关了"}
STATUS = {
    "permission": "等你批准",
    "permission_prompt": "等你批准",
    "idle_prompt": "等你回复",
    "agent_needs_input": "等你回复",
    "elicitation_dialog": "等你回复",
    "stop": "完成",
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def sign(key, *parts):
    """HMAC-SHA256 over newline-joined parts (str or bytes)."""
    mac = hmac.new(key.encode(), digestmod=hashlib.sha256)
    for i, part in enumerate(parts):
        if i:
            mac.update(b"\n")
        mac.update(part if isinstance(part, bytes) else str(part).encode())
    return mac.hexdigest()


def canonical(answers):
    """A question's answers as the signature on them covers them (claude_hooks/phone_hook.py)."""
    return json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def summarize_tool(tool_name, tool_input, truncated=False):
    """(text for the phone, complete). complete=False means the phone cannot show everything
    that would be approved, so the request must go to the computer instead."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in COMPUTER_ONLY_TOOLS or truncated:
        return "", False
    if tool_name == "Bash":
        if any(v for k, v in tool_input.items() if k not in BASH_FIELDS):
            return "", False
        command = f"$ {tool_input.get('command', '')}"
        desc = str(tool_input.get("description") or "")
        for text in (f"{command}\n{desc}" if desc else command, command):
            if phone_render.fits(f"{tool_name}\n{text}", REQUEST_FOOTER):
                return text, True
        return command, False
    text = json.dumps(tool_input, ensure_ascii=False)
    return text, phone_render.fits(f"{tool_name}\n{text}", REQUEST_FOOTER)


ASK_TOOL = "AskUserQuestion"


def ask_questions(tool_input):
    """AskUserQuestion's questions as the phone asks them, [{question, options, several}], or None
    when it cannot: only 1-4 plain choice questions of 2-4 options (no text or number ones)."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) and "answers" not in tool_input else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        return None
    questions = []
    for q in raw:
        options = q.get("options") if isinstance(q, dict) and q.get("kind", "choice") == "choice" else None
        if not isinstance(options, list) or not 2 <= len(options) <= 4 or not isinstance(q.get("question"), str):
            return None
        labels = [o.get("label") if isinstance(o, dict) else None for o in options]
        if not all(isinstance(label, str) and label for label in labels) or not q["question"]:
            return None
        questions.append({"question": q["question"], "options": labels, "several": q.get("multiSelect") is True})
    return questions


class Request:
    def __init__(self, session, tool_name, text, phone_call=False, command=None, description=None,
                 questions=None):
        self.id = secrets.token_hex(4)
        self.token = secrets.token_urlsafe(16)
        self.session = session
        self.tool_name = tool_name
        self.text = text
        self.phone_call = phone_call
        self.command = command  # Bash only, for voice_agent to read out
        self.description = description
        self.created = time.time()
        self.decision = None
        self.closed = False
        self.done = threading.Event()
        self.questions = questions  # AskUserQuestion: asked one at a time, answers kept here
        self.answers, self.step, self.chosen = {}, 0, set()
        self.back = None  # where the phone goes once it is answered (Hub.go_back_soon)


class NonceCache:
    """Remembers nonces seen inside the signature window, so a captured request cannot be replayed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {}

    def add(self, nonce):
        now = time.time()
        with self.lock:
            self.seen = {n: t for n, t in self.seen.items() if now - t < 2 * SIG_WINDOW}
            if nonce in self.seen:
                return False
            self.seen[nonce] = now
            return True


class Hub:
    def __init__(self, push, wait_seconds=WAIT_SECONDS, state_path=None, desk=None, ring=None, door=None,
                 chime=None):
        self.push = push
        self.ring = ring  # () -> played; None: no ringing
        self.desk = desk  # desk_gateway.DeskGateway, or None
        self.door = door  # ha_door.DoorWatch, or None
        self.chime = chime  # () -> played: the door chime
        self.chime_at = 0.0
        self.wait_seconds = wait_seconds
        self.state_path = state_path
        self.lock = threading.Lock()
        self.pending = {}
        self.sessions = {}
        self.mode = self._load_mode()
        self.screen = None  # the page the phone loaded last, for what its PgUp/PgDn line keys do
        self.loads = 0  # pages the phone has loaded, to tell whether it moved on
        self.last_page = None  # (url, when) of the last page that can be shown again as it is
        self.messages = []  # posted messages, newest first (notify)

    # --- state -----------------------------------------------------------------
    def _load_mode(self):
        try:
            with open(self.state_path) as f:
                return json.load(f).get("mode", "away")
        except (OSError, TypeError, ValueError):
            return "away"

    def set_mode(self, mode):
        if mode not in ("away", "desk"):
            raise ValueError(mode)
        with self.lock:
            self.mode = mode
        if self.state_path:
            with open(self.state_path, "w") as f:
                json.dump({"mode": mode}, f)
        log(f"mode -> {mode}")

    def _touch_session(self, payload, status, message=None):
        sid = str(payload.get("session_id") or "?")
        cwd = str(payload.get("cwd") or "")
        with self.lock:
            entry = self.sessions.get(sid, {"project": os.path.basename(cwd.rstrip("/")) or "?"})
            entry.update(status=status, updated=time.time())
            if message is not None:
                entry["message"] = message
            self.sessions[sid] = entry
            for old in sorted(self.sessions, key=lambda k: self.sessions[k]["updated"])[:-SESSION_LIMIT]:
                del self.sessions[old]
        return sid

    def record_event(self, payload):
        event = payload.get("hook_event_name")
        if event == "Stop":
            msg = str(payload.get("last_assistant_message") or "")
            self._touch_session(payload, STATUS["stop"], msg[:400])
            self.session_done(payload, msg)
        elif event == "Notification":
            ntype = payload.get("notification_type", "")
            self._touch_session(payload, STATUS.get(ntype, "通知"), str(payload.get("message") or "")[:200])

    # --- permission flow ----------------------------------------------------------
    def request_permission(self, payload):
        """Return "allow"/"deny" from the phone, or None to fall back to the computer's dialog."""
        return self.ask(payload)[0]

    def ask(self, payload, gone=None):
        """(decision, answers): "allow"/"deny"/None as request_permission, and for an
        AskUserQuestion answered on the phone its answers (question -> label, or labels).
        gone() true means the hook has hung up: the phone stops asking."""
        sid = self._touch_session(payload, STATUS["permission"])
        tool_name = str(payload.get("tool_name") or "?")
        in_call = payload.get("phone_call") is True or bool(self.desk and self.desk.in_call(sid))
        questions = None
        if tool_name == ASK_TOOL:  # a call cannot show options: the computer asks
            questions = None if in_call or payload.get("truncated") else ask_questions(payload.get("tool_input"))
            text, complete = (questions[0]["question"], True) if questions else ("", False)
        else:
            text, complete = summarize_tool(tool_name, payload.get("tool_input"), bool(payload.get("truncated")))
        if self.mode != "away" and not in_call:
            return None, None
        if not complete:
            log(f"{tool_name}: cannot be shown in full on the phone; computer decides")
            return None, None
        with self.lock:
            if len(self.pending) >= MAX_PENDING:
                log("too many pending requests; falling back")
                return None, None
            tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
            bash = tool_name == "Bash"
            req = Request(sid, tool_name, text, phone_call=in_call,
                          command=str(tool_input.get("command") or "") if bash else None,
                          description=str(tool_input.get("description") or "") or None if bash else None,
                          questions=questions)
            self.pending[req.id] = req
            # back to a desk session if one was up, else to a page loaded just now (the panel, say), else
            # close the services page: the phone's own screen
            if self.screen and self.screen[0] == "view":
                req.back = f"{BASE_URL}/phone/desk/view/{self.screen[1]}"
            elif self.last_page and time.time() - self.last_page[1] <= BACK_RECENT:
                req.back = self.last_page[0]
            else:
                req.back = "Init:Services"
        pushed = False
        try:
            # a phone that answers the push very slowly must not hold the request open
            outcome = {}
            worker = threading.Thread(target=lambda: outcome.update(
                ok=self.push(f"{BASE_URL}/phone/req/{req.id}?t={req.token}")), daemon=True)
            worker.start()
            worker.join(PUSH_DEADLINE)
            pushed = outcome.get("ok") is True
            # in a call the keypad can answer even if the push failed
            hung_up = (pushed or in_call) and self._wait(req, gone)
        finally:
            with self.lock:
                self.pending.pop(req.id, None)
                req.closed = True
                decision = req.decision if pushed or in_call else None
        log(f"request {req.id} {tool_name}: {decision or ('hook hung up' if hung_up else 'no answer')}")
        return decision, (dict(req.answers) if questions and decision == "allow" else None)

    def _wait(self, req, gone):
        """Until req is answered or wait_seconds pass; True if gone() said the hook hung up first
        (it does once the session's transcript shows the call answered on the computer)."""
        deadline = time.monotonic() + self.wait_seconds
        while not req.done.wait(max(0.0, min(HOOK_POLL, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                return False
            if gone and gone():
                return True
        return False

    def redirect_next_step(self, payload):
        """Whether a question in a session's next-step box should be put to the phone instead,
        as an AskUserQuestion: in AWAY mode, outside a call (a call cannot show options)."""
        in_call = payload.get("phone_call") is True or bool(
            self.desk and self.desk.in_call(str(payload.get("session_id") or "?")))
        redirect = self.mode == "away" and not in_call
        if redirect:
            log("next-step question: asked on the phone instead")
        return redirect

    def go_back_soon(self, req):
        """BACK_DELAY after req was answered on the phone, show what was on screen before it came,
        unless the phone has loaded another page since."""
        loads = self.loads

        def later():
            time.sleep(BACK_DELAY)
            if self.loads != loads:
                log(f"request {req.id}: phone moved on, not going back")
            elif req.back:
                log(f"request {req.id}: back to {req.back}: {'ok' if self.push(req.back) else 'failed'}")

        threading.Thread(target=later, daemon=True).start()

    def ring_soon(self, req):
        """Ring once, RING_DELAY after the approval page's picture went to the phone, if req still
        waits on it: so each load (the push, then the page's own refresh every 10 s) in its first
        RING_WINDOW seconds rings once and an answered request stops ringing. A call reads its
        requests out instead."""
        if not self.ring or req.phone_call or req.step or req.chosen:  # someone is answering it
            return
        if time.time() - req.created > RING_WINDOW:
            return

        def later():
            time.sleep(RING_DELAY)
            if not req.closed and not req.done.is_set():
                self.ring()

        threading.Thread(target=later, daemon=True).start()

    def door_alert(self):
        """Someone at the front door (ha_door.DoorWatch): their picture to the phone, and a chime once
        the phone has it (chime_soon)."""
        self.chime_at = time.time()
        self.push(f"{BASE_URL}/phone/door?{time.time():.0f}")

    def chime_soon(self):
        """Ding-dong RING_DELAY after the door picture went to the phone, for an alert of the last
        CHIME_WINDOW seconds, once: looking again, or later from the menu, is quiet."""
        if not self.chime or time.time() - self.chime_at > CHIME_WINDOW:
            return
        self.chime_at = 0.0

        def later():
            time.sleep(RING_DELAY)
            self.chime()

        threading.Thread(target=later, daemon=True).start()

    # --- messages from other programs ----------------------------------------------
    def notify(self, payload):
        """A message posted to /notify: kept for the Messages page; a warning also comes up on the
        phone now, rings once when it is there (message_seen) and stays up until its OK."""
        level = "warning" if payload.get("level") == "warning" else "info"
        title = " ".join(str(payload.get("title") or "").split())[:MESSAGE_TITLE] or "消息"
        text = str(payload.get("text") or "").strip()[:MESSAGE_TEXT]
        msg = {"id": secrets.token_hex(4), "level": level, "title": title, "text": text,
               "at": time.time(), "rung": level != "warning", "acked": level != "warning"}
        with self.lock:
            self.messages = [msg, *self.messages][:MAX_MESSAGES]
        log(f"message {msg['id']} {level}: {title}")
        if level == "warning":
            self.push(f"{BASE_URL}/phone/msg/{msg['id']}")
        return msg["id"]

    def session_done(self, payload, text):
        """A desktop session finished its turn: up on the phone with GO (Handler._message_page),
        replacing a notice of that session not yet seen. An approval on screen is not covered: the
        panel shows the notice once nothing waits."""
        sid = str(payload.get("session_id") or "")
        if not self.desk or not sid or payload.get("on_screen") is True or payload.get("phone_call") is True:
            return None
        title = " ".join(str(payload.get("title") or "").split())
        title = title or os.path.basename(str(payload.get("cwd") or "").rstrip("/")) or "?"
        msg = {"id": secrets.token_hex(4), "level": "done", "title": f"完成：{title}"[:MESSAGE_TITLE],
               "text": re.sub(r"\*\*|`", "", text).strip()[:MESSAGE_TEXT], "session": sid, "at": time.time(),
               "rung": self.mode != "away", "acked": False}
        with self.lock:
            kept = [m for m in self.messages if not (m.get("session") == sid and not m["acked"])]
            self.messages = [msg, *kept][:MAX_MESSAGES]
            waiting = bool(self.pending)
        log(f"message {msg['id']} done: {title}")
        if not waiting:
            self.push(f"{BASE_URL}/phone/msg/{msg['id']}")
        return msg["id"]

    def message(self, msg_id):
        """(message, index, count) newest first, or None."""
        with self.lock:
            for i, msg in enumerate(self.messages):
                if msg["id"] == msg_id:
                    return msg, i, len(self.messages)
        return None

    def unacked_warning(self):
        """The newest warning (or finished session) still waiting for its OK, if it came in the last
        WARNING_HOLD."""
        with self.lock:
            return next((m for m in self.messages if not m["acked"] and time.time() - m["at"] <= WARNING_HOLD), None)

    def acknowledge(self, msg_id):
        with self.lock:
            for msg in self.messages:
                if msg["id"] == msg_id:
                    msg["acked"] = True

    def message_seen(self, msg):
        """A warning's picture went to the phone: ring the first time, RING_DELAY later."""
        with self.lock:
            if msg["rung"]:
                return
            msg["rung"] = True
        if self.ring and time.time() - msg["at"] <= RING_WINDOW:
            def ring():
                log(f"message {msg['id']}: ring {'ok' if self.ring() else 'failed'}")
            threading.Timer(RING_DELAY, ring).start()

    def get(self, req_id, token):
        with self.lock:
            req = self.pending.get(req_id)
        if req and hmac.compare_digest(req.token, token or ""):
            return req
        return None

    def _answer(self, req_id, behavior, may_answer):
        """Record the first answer only; later presses or a closed request change nothing."""
        if behavior not in ("allow", "deny"):
            return False
        with self.lock:
            req = self.pending.get(req_id)
            if not req or req.closed or req.decision is not None or not may_answer(req):
                return False
            req.decision = behavior
        req.done.set()
        return True

    def decide(self, req_id, token, behavior):
        """A softkey on the phone screen; the token shows the page belongs to this request.
        A question is answered by its options, never by Allow/Deny."""
        return self._answer(req_id, behavior,
                            lambda req: req.questions is None and hmac.compare_digest(req.token, token or ""))

    def choose(self, req_id, token, option=None):
        """On the question on screen: pick option (toggle it where several may be chosen), or with
        option None, take the ones chosen. The last question answered completes the request."""
        with self.lock:
            req = self.pending.get(req_id)
            if (not req or req.closed or req.decision is not None or req.questions is None
                    or not hmac.compare_digest(req.token, token or "")):
                return None
            q = req.questions[req.step]
            if option is not None and not 0 <= option < len(q["options"]):
                return req
            if q["several"] and option is not None:
                req.chosen ^= {option}
                return req
            if q["several"]:
                if not req.chosen:
                    return req
                req.answers[q["question"]] = [q["options"][i] for i in sorted(req.chosen)]
            else:
                req.answers[q["question"]] = q["options"][option]
            req.step, req.chosen = req.step + 1, set()
            if req.step == len(req.questions):
                req.decision = "allow"
        if req.decision:
            req.done.set()
        return req

    def decide_in_call(self, req_id, session, behavior):
        """A key pressed during a call to Claude: only for that call's own session."""
        return self._answer(req_id, behavior, lambda req: req.phone_call and req.session == session != "?")

    def pending_for(self, session):
        """A call's open requests, oldest first, for voice_agent to read out."""
        if session == "?":
            return []
        with self.lock:
            reqs = sorted(self.pending.values(), key=lambda r: r.created)
            return [{"id": r.id, "tool_name": r.tool_name, "text": r.text, "command": r.command,
                     "description": r.description} for r in reqs
                    if r.phone_call and r.session == session and not r.closed and r.decision is None]

    def panel_text(self):
        with self.lock:
            sessions = sorted(self.sessions.values(), key=lambda s: -s["updated"])[:3]
            pending = len(self.pending)
            mode = self.mode
        title = "Claude Code | " + ("离开：话机审批" if mode == "away" else "在座位：电脑审批")
        if not sessions:
            body = "还没有会话活动。"
        else:
            body = "\n".join(
                f"● {s['project']} {s['status']} {time.strftime('%H:%M', time.localtime(s['updated']))}"
                + (f"\n  {s['message'][:60]}" if s.get("message") else "")
                for s in sessions)
        footer = f"待批准 {pending} | {time.strftime('%H:%M:%S')}"
        return title, body, footer


# --- Cisco XML -------------------------------------------------------------------
def softkeys(keys):
    return "".join(
        f"<SoftKeyItem><Name>{escape(name)}</Name><URL>{escape(url)}</URL><Position>{i}</Position></SoftKeyItem>"
        for i, (name, url) in enumerate(keys, 1))


def image_page(title, png_url, keys, wide=False):
    mode = ' WindowMode="Wide"' if wide else ""
    return (f"<CiscoIPPhoneImageFile{mode}><Title>{escape(title)}</Title><Prompt></Prompt>"
            f"<LocationX>-1</LocationX><LocationY>-1</LocationY><URL>{escape(png_url)}</URL>"
            f"{softkeys(keys)}</CiscoIPPhoneImageFile>")


def touch_page(title, png_url, areas, keys, wide=False):
    """A full-screen picture whose rectangles (x1, y1, x2, y2 on the 298x168 services area,
    320x168 when wide) open URLs when touched: the 7970 has a touchscreen."""
    items = "".join(f'<MenuItem><Name>{escape(name)}</Name><URL>{escape(url)}</URL>'
                    f'<TouchArea X1="{x1}" Y1="{y1}" X2="{x2}" Y2="{y2}"/></MenuItem>'
                    for name, url, (x1, y1, x2, y2) in areas)
    mode = ' WindowMode="Wide"' if wide else ""
    return (f"<CiscoIPPhoneGraphicFileMenu{mode}><Title>{escape(title)}</Title><Prompt></Prompt>"
            f"<LocationX>0</LocationX><LocationY>0</LocationY><URL>{escape(png_url)}</URL>"
            f"{items}{softkeys(keys)}</CiscoIPPhoneGraphicFileMenu>")


def arrow_areas(up_url, down_url):
    """Touch areas over the arrows a wide picture draws beside line keys 2 and 3."""
    return [(name, url, (phone_render.WIDTH, phone_render.KEY_Y[key] - 10,
                         phone_render.WIDE_WIDTH - 1, phone_render.KEY_Y[key] + 10))
            for key, name, url in ((2, "Page up", up_url), (3, "Page down", down_url))]


def icon_menu(title, items):
    """A menu with Claude's icon in its title bar in place of the services globe (the status
    bar's globe is the phone's own and stays)."""
    body = "".join(f"<MenuItem><Name>{escape(n)}</Name><URL>{escape(u)}</URL></MenuItem>" for n, u in items)
    return (f'<CiscoIPPhoneIconFileMenu><Title IconIndex="1">{escape(title)}</Title><Prompt>Select</Prompt>{body}'
            f'<IconItem><Index>1</Index><URL>{escape(BASE_URL)}/phone/icon/claude.png</URL></IconItem>'
            f'</CiscoIPPhoneIconFileMenu>')


def text_page(title, text, keys=(("Exit", "SoftKey:Exit"),)):
    return (f"<CiscoIPPhoneText><Title>{escape(title)}</Title><Text>{escape(text)}</Text>"
            f"{softkeys(keys)}</CiscoIPPhoneText>")


class PushAuth:
    """One-time passwords for /CGI/Execute: each push gets a fresh one, good for one auth
    callback within PUSH_AUTH_TTL seconds."""

    PUSH_AUTH_TTL = 30

    def __init__(self):
        self.lock = threading.Lock()
        self.issued = {}

    def _purge(self):
        now = time.time()
        self.issued = {pw: exp for pw, exp in self.issued.items() if exp > now}

    def issue(self):
        password = secrets.token_urlsafe(18)
        with self.lock:
            self._purge()
            self.issued[password] = time.time() + self.PUSH_AUTH_TTL
        return password

    def check(self, password):
        with self.lock:
            self._purge()
            for pw in list(self.issued):
                if hmac.compare_digest(pw, password or ""):
                    del self.issued[pw]
                    return True
            return False


def execute_via_cgi(push_auth, url, timeout=5):
    """Make the phone execute url right now: a page to open, or an internal URI such as Play:."""
    password = push_auth.issue()
    auth = base64.b64encode(f"{PUSH_USER}:{password}".encode()).decode()
    xml = f'<CiscoIPPhoneExecute><ExecuteItem Priority="0" URL={quoteattr(url)}/></CiscoIPPhoneExecute>'
    req = urllib.request.Request(
        f"http://{PHONE_IP}/CGI/Execute", data=urllib.parse.urlencode({"XML": xml}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
    except Exception as exc:  # any network or protocol failure means "not done"
        log(f"push failed: {exc}")
        return False
    ok = 'Status="0"' in body
    if not ok:
        log(f"push rejected by phone: {body[:200]}")
    return ok


def push_via_cgi(push_auth):
    """Return a push(url) function that makes the phone open url right now."""
    return lambda url: execute_via_cgi(push_auth, url)


def ring_via_cgi(push_auth, name=RING_FILE):
    """Return a ring() function: the phone fetches name from the Mini's TFTP server and plays
    it once through its speaker, flashing its message light (the request returns when it has
    played, about 2 s)."""
    return lambda: execute_via_cgi(push_auth, f"Play:{name}", timeout=10)


def screenshot_via_cgi(push_auth):
    """Return a shot() function: the phone's screen as (content type, bytes), or None. The phone
    checks the one-time password through authenticationURL, as for pushes."""

    def shot():
        password = push_auth.issue()
        auth = base64.b64encode(f"{PUSH_USER}:{password}".encode()).decode()
        req = urllib.request.Request(f"http://{PHONE_IP}/CGI/Screenshot", headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.headers.get("Content-Type") or "", resp.read(MAX_SHOT)
        except Exception as exc:
            log(f"screenshot failed: {exc}")
            return None

    return shot


# --- HTTP ------------------------------------------------------------------------
class HubServer(ThreadingHTTPServer):
    """Threaded server with a cap on simultaneous connections."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections=MAX_CONNECTIONS):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(hub, hook_key, push_auth, shot=None):
    shot = shot or screenshot_via_cgi(push_auth)
    nonces = NonceCache()

    class Handler(BaseHTTPRequestHandler):
        server_version = "phone-hub"
        timeout = SOCKET_TIMEOUT

        def log_message(self, fmt, *args):
            pass

        # Each connection gets REQUEST_DEADLINE seconds to deliver its whole request; the idle
        # timeout alone would let a client trickle one byte at a time and hold the slot forever.
        def setup(self):
            super().setup()
            self._deadline = threading.Timer(REQUEST_DEADLINE, self._cut_off)
            self._deadline.daemon = True
            self._deadline.start()

        def _cut_off(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self._deadline.cancel()
            super().finish()

        def _send(self, status, body, ctype="text/xml; charset=utf-8", headers=()):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _client(self):
            return self.client_address[0]

        def _hook_gone(self):
            """True once the hook waiting on this connection has hung up."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
            except OSError:
                return True

        # hooks and voice_agent ----------------------------------------------------
        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/hook/permission", "/hook/event", "/hook/next-step", "/notify"):
                allowed = (HOOK_NET, LOOPBACK)
            elif path in ("/local/pending", "/local/decide") or path in DESK_ROUTES or path in DEBUG_ROUTES:
                allowed = (LOOPBACK,)
            else:
                return self._send(404, "not found", "text/plain")
            client = ipaddress.ip_address(self._client())
            if not any(client in net for net in allowed):
                return self._send(403, "forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "bad length", "text/plain")
            if length < 0 or length > MAX_BODY:
                return self._send(413, "bad size", "text/plain")
            ts, nonce = self.headers.get("X-Hub-Time", ""), self.headers.get("X-Hub-Nonce", "")
            given = self.headers.get("X-Hub-Signature", "")
            try:
                fresh = abs(time.time() - int(ts)) <= SIG_WINDOW
            except ValueError:
                fresh = False
            if not (fresh and nonce and given):  # refuse before reading a body we would discard
                return self._send(401, "unauthorized", "text/plain")
            body = self.rfile.read(length)
            if not hmac.compare_digest(given, sign(hook_key, ts, nonce, path, body)):
                return self._send(401, "unauthorized", "text/plain")
            self._deadline.cancel()  # authenticated and fully read; a permission may now wait
            if not nonces.add(nonce):
                return self._send(401, "replayed", "text/plain")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, "bad json", "text/plain")
            if not isinstance(payload, dict):
                return self._send(400, "bad json", "text/plain")
            if path == "/hook/next-step":
                redirect = hub.redirect_next_step(payload)
                answer = {"redirect": redirect, "sig": sign(hook_key, nonce, "redirect" if redirect else "keep")}
                return self._send(200, json.dumps(answer), "application/json")
            if path == "/hook/permission":
                decision, answers = hub.ask(payload, gone=self._hook_gone)
                answer = {"decision": decision, "sig": sign(hook_key, nonce, str(decision))}
                if answers is not None:  # the hook takes them only if the signature covers them
                    answer.update(answers=answers, sig=sign(hook_key, nonce, str(decision), canonical(answers)))
                return self._send(200, json.dumps(answer), "application/json")
            if path == "/notify":
                return self._send(200, json.dumps({"ok": True, "id": hub.notify(payload)}), "application/json")
            if path in DESK_ROUTES:
                return self._send(200, json.dumps(desk_local(path, payload), ensure_ascii=False), "application/json")
            if path == "/local/screenshot":
                got = shot()
                answer = {"type": got[0], "image": base64.b64encode(got[1]).decode()} if got else {"error": "no screenshot"}
                return self._send(200, json.dumps(answer), "application/json")
            if path == "/local/ring":
                return self._send(200, json.dumps({"ok": bool(hub.ring and hub.ring())}), "application/json")
            if path == "/local/door":  # try the door alert: the doorbell's picture now, and the chime
                ok = False
                if hub.door:
                    try:
                        hub.door.show(0)
                        hub.door_alert()
                        ok = True
                    except Exception as exc:
                        log(f"door: {exc}")
                return self._send(200, json.dumps({"ok": ok}), "application/json")
            if path == "/local/show":  # only this hub's own phone pages, or home: never Dial: and the like
                url = str(payload.get("url") or "")
                ok = (url.startswith(f"{BASE_URL}/phone/") or url == "Init:Services") and hub.push(url)
                return self._send(200, json.dumps({"ok": bool(ok)}), "application/json")
            session = str(payload.get("session_id") or "?")
            if path == "/local/pending":
                return self._send(200, json.dumps({"requests": hub.pending_for(session)}), "application/json")
            if path == "/local/decide":
                ok = hub.decide_in_call(str(payload.get("id") or ""), session, payload.get("decision"))
                return self._send(200, json.dumps({"ok": ok}), "application/json")
            hub.record_event(payload)
            return self._send(200, json.dumps({"ok": True}), "application/json")

        # phone -------------------------------------------------------------------
        def do_GET(self):
            self._deadline.cancel()  # the request is in; GET answers are immediate
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if self._client() != PHONE_IP:
                return self._send(403, "forbidden", "text/plain")
            parts = [p for p in url.path.split("/") if p]
            if parts[:1] != ["phone"]:
                return self._send(404, "not found", "text/plain")
            route = parts[1:]
            shown = None
            if not (route and route[-1].endswith(".png") or route == ["auth"]):
                # a page, not a picture on one: what PgUp/PgDn do is up to it, by default page the desk list
                shown, hub.screen = hub.screen, None
                hub.loads += 1
                if route in ([], ["menu"], ["panel"], ["pending"], ["door"], ["desk"]) or route[:2] == ["desk", "view"]:
                    hub.last_page = (BASE_URL + url.path + (f"?{url.query}" if url.query else ""), time.time())
            if route in (["desk", "pageup"], ["desk", "pagedown"]) and shown:
                return self._page_key(shown, route[1])

            if route[:1] == ["desk"]:
                return self._desk_page(route[1:], query)
            if route in (["door"], ["door.png"]):
                return self._door_page(route, query)
            if route[:1] in (["msgs"], ["msg"]):
                return self._message_page(route)
            if route == ["auth"]:
                ok = (query.get("UserID", [""])[0] == PUSH_USER
                      and push_auth.check(query.get("Password", [""])[0]))
                return self._send(200, "AUTHORIZED" if ok else "UN-AUTHORIZED", "text/plain")
            if route in ([], ["menu"]):
                return self._send(200, self._menu())
            if route == ["icon", "claude.png"]:
                return self._send(200, phone_render.claude_icon(), "image/png")
            if route == ["panel"]:
                return self._panel()
            if route == ["panel.png"]:
                return self._send(200, phone_render.render_screen(*hub.panel_text(), arrows=True), "image/png")
            if route == ["pending"]:
                return self._send(200, self._pending_menu())
            if route[:1] == ["mode"] and route[1:2] in (["away"], ["desk"]):
                return self._mode_page(route[1], route[2:] == ["yes"])
            if len(route) >= 2 and route[0] == "req":
                return self._request_page(route[1:], query.get("t", [""])[0])
            return self._send(404, "not found", "text/plain")

        def _page_key(self, shown, key):
            """PgUp/PgDn (line keys 2 and 3) where the phone does not show the desk list: on a
            session's page they page its conversation; an approval or question they leave on screen."""
            if shown[0] == "view":
                _, host_id, up, down = shown
                return self._desk_page(["view", host_id], {"at": [str(up if key == "pageup" else down)]})
            _, rid, token = shown
            if hub.get(rid, token):
                return self._request_page([rid], token)
            return self._desk_page([key], {})

        def _panel(self):
            # The panel auto-refreshes and is the idle screen, so it would cover a pushed
            # approval page within seconds: while anything is waiting, show that instead.
            with hub.lock:
                waiting = min(hub.pending.values(), key=lambda r: r.created, default=None)
            if waiting:
                return self._request_page([waiting.id], waiting.token)
            warning = hub.unacked_warning()
            if warning:
                return self._message_page(["msg", warning["id"]])
            return self._send(200, image_page("Claude Code", f"{BASE_URL}/phone/panel.png?{time.time():.0f}", [
                ("Refresh", f"{BASE_URL}/phone/panel"),
                ("Mode", f"{BASE_URL}/phone/mode/{'desk' if hub.mode == 'away' else 'away'}"),
                ("Exit", "SoftKey:Exit")], wide=True), headers=[("Refresh", f"20; url={BASE_URL}/phone/panel")])

        def _mode_page(self, mode, confirmed):
            """Switching modes asks first, Switch in the third softkey's place: Mode is the panel's
            second, where an answer page has an option, so a press meant for that page cannot switch."""
            if confirmed:
                hub.set_mode(mode)
                return self._panel()
            what = "AWAY: the phone approves" if mode == "away" else "DESK: the computer approves"
            return self._send(200, text_page("Claude Code", f"Switch to {what}?", [
                ("Cancel", f"{BASE_URL}/phone/panel"), ("Exit", "SoftKey:Exit"),
                ("Switch", f"{BASE_URL}/phone/mode/{mode}/yes")]), headers=[OFF_HISTORY])

        def _menu(self):
            with hub.lock:
                n = len(hub.pending)
            items = [("Status panel", f"{BASE_URL}/phone/panel"),
                     (f"Pending approvals ({n})", f"{BASE_URL}/phone/pending"),
                     *([("Desk sessions", f"{BASE_URL}/phone/desk")] if hub.desk else []),
                     (f"Messages ({len(hub.messages)})", f"{BASE_URL}/phone/msgs"),
                     *([("Front door", f"{BASE_URL}/phone/door?cam=0")] if hub.door else []),
                     ("Mode: AWAY - phone approves" if hub.mode == "away" else "Mode: DESK - computer approves",
                      f"{BASE_URL}/phone/mode/{'desk' if hub.mode == 'away' else 'away'}")]
            return icon_menu("Claude Code", items)

        def _pending_menu(self):
            with hub.lock:
                reqs = sorted(hub.pending.values(), key=lambda r: r.created)
            if not reqs:
                return text_page("Claude Code", "No pending approvals.")
            return icon_menu("Pending", [(f"{r.tool_name} {r.id}", f"{BASE_URL}/phone/req/{r.id}?t={r.token}")
                                         for r in reqs])

        # messages from other programs (/notify) ------------------------------------------
        def _message_page(self, route):
            """One message on screen, newest first: msgs opens the newest, Older/Newer step through.
            A warning's OK acknowledges it (the panel stops showing it) and opens the panel."""
            if route == ["msgs"]:
                with hub.lock:
                    newest = hub.messages[0]["id"] if hub.messages else None
                if newest is None:
                    return self._send(200, text_page("Messages", "No messages."), headers=[OFF_HISTORY])
                route = ["msg", newest]
            if len(route) == 3 and route[2] == "ok":
                hub.acknowledge(route[1])
                return self._panel()
            if len(route) == 3 and route[2] == "go":  # bring the finished session to the front on the Studio
                found = hub.message(route[1])
                if not found or not found[0].get("session") or not hub.desk:
                    return self._send(200, text_page("Messages", "This message is gone."), headers=[OFF_HISTORY])
                try:
                    result = hub.desk.focus(found[0]["session"])
                except Exception as exc:
                    log(f"desk focus: {exc}")
                    result = {"ok": False, "error": "电脑没回应"}
                if result.get("ok"):
                    hub.acknowledge(route[1])
                    return self._panel()
                # the notice again, saying why (a text page pushed to the phone does not show)
                error = str(result.get("error") or "")
                found[0]["note"] = GO_ERRORS.get(error, f"没打开：{error[:40]}")
                route = ["msg", route[1]]
            if len(route) != 2:
                return self._send(404, "not found", "text/plain")
            found = hub.message(route[1][:-4] if route[1].endswith(".png") else route[1])
            if not found:
                return self._send(200, text_page("Messages", "This message is gone."), headers=[OFF_HISTORY])
            msg, i, count = found
            if route[1].endswith(".png"):
                head = msg["title"]  # a warning's is in the accent colour (the font has no ⚠)
                foot = msg.get("note") or f"{time.strftime('%m-%d %H:%M', time.localtime(msg['at']))}   {i + 1}/{count}"
                hub.message_seen(msg)
                return self._send(200, phone_render.render_screen(head, msg["text"], foot,
                                                                  accent=msg["level"] == "warning", arrows=True),
                                  "image/png")
            with hub.lock:
                older = hub.messages[i + 1]["id"] if i + 1 < len(hub.messages) else None
                newer = hub.messages[i - 1]["id"] if i > 0 else None
            keys = [("GO", f"{BASE_URL}/phone/msg/{msg['id']}/go")] if msg["level"] == "done" else []
            keys += [("OK", f"{BASE_URL}/phone/msg/{msg['id']}/ok") if msg["level"] != "info"
                     else ("Exit", "SoftKey:Exit")]
            keys += [(name, f"{BASE_URL}/phone/msg/{other}") for name, other in (("Older", older), ("Newer", newer))
                     if other]
            return self._send(200, image_page("Message", f"{BASE_URL}/phone/msg/{msg['id']}.png?{time.time():.0f}",
                                              keys, wide=True), headers=[OFF_HISTORY])

        # front door (Home Assistant's Blink cameras, ha_door) ------------------------------
        def _door_page(self, route, query):
            """The front door's picture: pushed when someone comes (Hub.door_alert), or looked at now
            from the menu (cam=<camera index>); Other shows the other camera."""
            door = hub.door
            if door is None:
                return self._send(200, text_page("Front door", "No door watch here."))
            if route == ["door.png"]:
                latest = door.latest
                if latest is None:
                    return self._send(404, "not found", "text/plain")
                picture, index, taken = latest
                png = phone_render.render_photo(picture, door_caption(door.label(index), taken), arrows=True)
                hub.chime_soon()
                return self._send(200, png, "image/png")
            cam = query.get("cam", [""])[0]
            try:
                if cam.isdigit() or door.latest is None:  # look now
                    door.show(int(cam) if cam.isdigit() else 0)
            except LookupError:
                return self._send(200, text_page("Front door", "No Home Assistant token on the Mini (ha.token)."),
                                  headers=[OFF_HISTORY])
            except Exception as exc:  # HA restarting, the Air asleep
                log(f"door: {exc}")
                return self._send(200, text_page("Front door", "Home Assistant is not reachable."),
                                  headers=[OFF_HISTORY])
            index, here = door.latest[1], f"{BASE_URL}/phone/door"
            return self._send(200, image_page("Front door", f"{here}.png?{time.time():.0f}", [
                ("Update", f"{here}?cam={index}"), ("Other", f"{here}?cam={index + 1}"),
                ("Exit", "SoftKey:Exit")], wide=True), headers=[OFF_HISTORY])

        # desk sessions (Claude desktop app on the Studio) ---------------------------------
        def _desk_page(self, route, query):
            desk = hub.desk
            if desk is None:
                return self._send(200, text_page("Desk sessions", "No desk relay configured."))
            try:
                if route in ([], ["up"], ["down"], ["pageup"], ["pagedown"]):  # page*: the PgUp/PgDn line keys
                    desk.refresh()
                    rows = phone_render.body_capacity(DESK_FOOTER)
                    cursor = desk.move({"up": -1, "down": 1, "pageup": -rows, "pagedown": rows}[route[0]] if route else 0)
                    first, capacity = phone_render.list_window(len(desk.listed), cursor, DESK_FOOTER)
                    areas = []  # each row on screen opens its session when touched
                    for k, session in enumerate(desk.listed[first:first + capacity]):
                        host_id, top = str(session.get("id") or ""), phone_render.BODY_TOP + k * phone_render.LINE_H - 1
                        if DESK_ID.fullmatch(host_id):
                            areas.append((f"Session {first + k + 1}", f"{BASE_URL}/phone/desk/view/{host_id}",
                                          (0, top, phone_render.WIDTH - 1, top + phone_render.LINE_H - 1)))
                    areas += arrow_areas(f"{BASE_URL}/phone/desk/pageup", f"{BASE_URL}/phone/desk/pagedown")
                    return self._send(200, touch_page("Desk sessions", f"{BASE_URL}/phone/desk/list.png?{time.time():.0f}",
                                                      areas, wide=True, keys=[
                        ("Open", f"{BASE_URL}/phone/desk/open"), ("Down", f"{BASE_URL}/phone/desk/down"),
                        ("Up", f"{BASE_URL}/phone/desk/up"), ("Exit", "SoftKey:Exit")]))
                if route == ["list.png"]:
                    rows = [("【忙】" if s.get("status") == "busy" else "") + str(s.get("title") or "?") for s in desk.listed]
                    png = phone_render.render_list("桌面会话", rows or ["（没有打开的会话）"], desk.cursor, DESK_FOOTER,
                                                   arrows=True)
                    return self._send(200, png, "image/png")
                if route == ["open"]:
                    session = desk.at_cursor()
                    return self._desk_view(session["id"] if session else "")
                if len(route) == 2 and route[0] == "view":
                    start = query.get("at", [""])[0]
                    start = int(start) if start.isdigit() else None
                    if route[1].endswith(".png"):
                        return self._desk_view_png(route[1][:-4], start)
                    return self._desk_view(route[1], start)
                return self._send(404, "not found", "text/plain")
            except Exception as exc:  # the Studio asleep or the relay down
                log(f"desk relay: {exc}")
                return self._send(200, text_page("Desk sessions", "The Mac Studio is not reachable."))

        def _desk_view(self, host_id, start=None):
            """The last few turns of a session, a page at a time: it opens at the last thing said
            to it; Up/Down (softkeys, or touching the upper/lower half) page back and on."""
            desk = hub.desk
            view = desk.view(host_id) if DESK_ID.fullmatch(host_id) else {"error": "bad id"}
            if view.get("error"):
                return self._send(200, text_page("Desk sessions", "This session is no longer open."))
            session = next((s for s in desk.listed if s.get("id") == host_id),
                           {"id": host_id, "title": view.get("title"), "session_id": ""})
            desk.select(session)  # a call to 300 now talks to this session
            lines, last_question = phone_render.conversation_lines(view.get("turns") or [])
            start, up, down = desk_pages(len(lines), last_question if start is None else start)
            url = f"{BASE_URL}/phone/desk/view/{host_id}"
            top, rows = phone_render.BODY_TOP - 1, phone_render.body_capacity(DESK_VIEW_FOOTER)
            middle, bottom, right = top + rows * phone_render.LINE_H // 2, top + rows * phone_render.LINE_H - 1, phone_render.WIDTH - 1
            hub.screen = ("view", host_id, up, down)  # PgUp/PgDn page it as Up/Down do
            return self._send(200, touch_page("Desk session", f"{url}.png?at={start}&t={time.time():.0f}", [
                ("Up", f"{url}?at={up}", (0, top, right, middle - 1)),
                ("Down", f"{url}?at={down}", (0, middle, right, bottom))] + arrow_areas(f"{url}?at={up}", f"{url}?at={down}"), [
                ("Talk", "Dial:300"), ("Up", f"{url}?at={up}"), ("Down", f"{url}?at={down}"),
                ("Back", f"{BASE_URL}/phone/desk")], wide=True))

        def _desk_view_png(self, host_id, start=None):
            view = hub.desk.view(host_id) if DESK_ID.fullmatch(host_id) else {"error": "bad id"}
            if view.get("error"):
                return self._send(404, "not found", "text/plain")
            lines, last_question = phone_render.conversation_lines(view.get("turns") or [])
            start, _, _ = desk_pages(len(lines), last_question if start is None else start)
            end = min(len(lines), start + phone_render.body_capacity(DESK_VIEW_FOOTER))
            footer = (f"{start + 1}-{end}/{len(lines)} 行 | " if lines else "") + \
                ("忙" if view.get("status") == "busy" else "空闲") + " | Talk 拨 300"
            return self._send(200, phone_render.render_lines(view.get("title") or "?", lines or ["（还没有对话）"],
                                                             start, footer, arrows=True), "image/png")

        def _request_page(self, route, token):
            rid = route[0][:-4] if route[0].endswith(".png") else route[0]
            req = hub.get(rid, token)
            if not req:
                return self._send(200, text_page("Claude Code", "This request is no longer open."),
                                  headers=[OFF_HISTORY])
            if req.questions is not None:
                return self._question_page(req, route, token)
            if route[0].endswith(".png"):
                with hub.lock:
                    project = hub.sessions.get(req.session, {}).get("project", "?")
                png = phone_render.render_screen(f"需要批准 | {project}", f"{req.tool_name}\n{req.text}",
                                                 REQUEST_FOOTER, accent=True, arrows=True)
                hub.ring_soon(req)
                return self._send(200, png, "image/png")
            if len(route) == 2 and route[1] in ("allow", "deny"):
                recorded = hub.decide(rid, token, route[1])
                word = "Allowed" if route[1] == "allow" else "Denied"
                text = f"{word}: {req.tool_name}" if recorded else "Already answered."
                if recorded:
                    hub.go_back_soon(req)
                return self._send(200, text_page("Claude Code", text), headers=[OFF_HISTORY])
            q = f"?t={urllib.parse.quote(token)}"
            hub.screen = ("req", rid, token)  # PgUp/PgDn, pressed by mistake, leave it on screen
            return self._send(200, image_page("Claude Code: approve?", f"{BASE_URL}/phone/req/{rid}.png{q}", [
                ("Allow", f"{BASE_URL}/phone/req/{rid}/allow{q}"),
                ("Deny", f"{BASE_URL}/phone/req/{rid}/deny{q}"),
                ("Exit", "SoftKey:Exit")], wide=True), headers=[("Refresh", f"10; url={BASE_URL}/phone/panel"), OFF_HISTORY])

        def _question_page(self, req, route, token):
            """An AskUserQuestion, one question at a time: touch an option or press its number;
            where several may be chosen, toggle them and press Done."""
            if len(route) == 3 and route[1] == "pick" and route[2].isdigit():
                req = hub.choose(req.id, token, int(route[2])) or req
            elif len(route) == 2 and route[1] == "done":
                req = hub.choose(req.id, token) or req
            if req.decision == "allow":
                if len(route) > 1:  # this load answered it
                    hub.go_back_soon(req)
                return self._send(200, text_page("Claude Code", "Answered: sent to Claude."), headers=[OFF_HISTORY])
            q = req.questions[req.step]
            lines, first = phone_render.choice_lines(q["question"], q["options"], req.chosen, q["several"])
            if route[0].endswith(".png"):
                with hub.lock:
                    project = hub.sessions.get(req.session, {}).get("project", "?")
                footer = (f"第 {req.step + 1}/{len(req.questions)} 题 · "
                          + ("可多选，选好按 Done" if q["several"] else "点选项或按数字"))
                hub.ring_soon(req)
                return self._send(200, phone_render.render_lines(f"需要选择 | {project}", lines, 0, footer, arrows=True),
                                  "image/png")
            base, tq = f"{BASE_URL}/phone/req/{req.id}", f"?t={urllib.parse.quote(token)}"
            areas = [(f"Option {i + 1}", f"{base}/pick/{i}{tq}",
                      (0, top, phone_render.WIDTH - 1, top + phone_render.LINE_H - 1))
                     for i, top in ((i, phone_render.BODY_TOP + (first + i) * phone_render.LINE_H - 1)
                                    for i in range(len(q["options"])))]
            keys = [(str(i + 1), f"{base}/pick/{i}{tq}") for i in range(len(q["options"]))]
            keys.append(("Done", f"{base}/done{tq}") if q["several"] else ("Exit", "SoftKey:Exit"))
            hub.screen = ("req", req.id, token)  # as for an approval
            return self._send(200, touch_page("Claude Code: choose", f"{base}.png{tq}&n={time.time():.0f}",
                                              areas, keys, wide=True),
                              headers=[("Refresh", f"10; url={BASE_URL}/phone/panel"), OFF_HISTORY])

    def desk_local(path, payload):
        """voice_agent's way to the desk relay; relay failures come back as {"error": ...}."""
        desk = hub.desk
        if desk is None:
            return {"error": "no desk relay"}
        try:
            if path == "/local/desk/target":
                started = time.monotonic()
                target = desk.target()
                if time.monotonic() - started > desk_gateway.TARGET_DEADLINE:
                    return {"target": None}  # voice_agent has given up on it: hold nothing for the call
                if target and target.get("session_id"):
                    desk.attach(str(target["session_id"]))  # in the call from now, before any words go in
                return {"target": target}
            if path == "/local/desk/send":
                return desk.send(str(payload.get("id") or ""), str(payload.get("text") or ""))
            if path == "/local/desk/reply":
                return desk.reply(str(payload.get("id") or ""), payload.get("offset"), str(payload.get("sent") or ""))
            desk.end(str(payload.get("session_id") or ""))
            return {"ok": True}
        except Exception as exc:  # the Studio asleep or the relay down: the call says so
            log(f"desk relay: {exc}")
            return {"error": f"relay: {exc}"}

    return Handler


DESK_ROUTES = ("/local/desk/target", "/local/desk/send", "/local/desk/reply", "/local/desk/end")
DEBUG_ROUTES = ("/local/screenshot", "/local/show", "/local/ring", "/local/door")  # check the phone from the Mini
DESK_FOOTER = "点一行就打开 · Up/Down 翻动"
DESK_VIEW_FOOTER = "x"  # the session page always has a footer line, which sets how many lines fit


def door_caption(label, taken, now=None):
    """Which camera, and when its picture was taken: the time alone if today, else the date too."""
    now = time.time() if now is None else now
    fmt = "%H:%M" if time.localtime(taken)[:3] == time.localtime(now)[:3] else "%m-%d %H:%M"
    return f"{label} · {time.strftime(fmt, time.localtime(taken))}"


def desk_pages(total, start):
    """(start, the start one page back, one page on) for a session page of total lines; each
    page keeps one line of the one before."""
    rows = phone_render.body_capacity(DESK_VIEW_FOOTER)
    last = max(0, total - rows)
    start = max(0, min(start, last))
    return start, max(0, start - (rows - 1)), min(last, start + rows - 1)


def main():
    secrets_path = os.path.join(HERE, "secrets.env")
    values = render.ensure_passwords(secrets_path, render.load_secrets(secrets_path), {"HUB_HOOK_PASSWORD"})
    hook_key = values["HUB_HOOK_PASSWORD"]
    push_auth = PushAuth()
    # HUB_DESK=0 / HUB_DOOR=0: a hub for another phone and its owner's own Claude Code (Sam's),
    # without mo's desk sessions on the Studio or the front door
    desk = desk_gateway.DeskGateway(hook_key) if os.environ.get("HUB_DESK", "1") != "0" else None
    hub = Hub(push_via_cgi(push_auth), state_path=os.path.join(HERE, "hub_state.json"),
              desk=desk, ring=ring_via_cgi(push_auth), chime=ring_via_cgi(push_auth, DOOR_CHIME))
    if os.environ.get("HUB_DOOR", "1") != "0":
        hub.door = ha_door.DoorWatch(ha_door.HomeAssistant(), alert=hub.door_alert, log=log)
        threading.Thread(target=hub.door.run, daemon=True).start()
    server = HubServer(("0.0.0.0", PORT), make_handler(hub, hook_key, push_auth))
    log(f"phone-hub on :{PORT}, phone {PHONE_IP}, hooks from {HOOK_NET}, mode {hub.mode}")
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

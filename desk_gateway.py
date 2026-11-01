"""phone-hub's side of the desk relay (desk_relay.py on the Mac Studio).

The hub reaches the relay with signed requests (hubauth) and keeps three bits of state:
  - the cursor of the session list on the phone screen;
  - the selection: the session last opened on the phone. A call to 300 talks to it for
    SELECTION_TTL seconds; otherwise calls go to the desktop session titled PHONE_SESSION;
  - the sessions a call is talking to, from the moment the call picks its target until it ends:
    their permission requests wait for the phone whatever the hub's mode, and voice_agent can
    answer them from the keypad.
The relay's answers must be signed for the request they answer (hubauth).
"""
import threading
import time

import hubauth

RELAY_URL = "http://192.168.1.20:8091"  # the Mac Studio (DHCP reservation on the router)
PHONE_SESSION = "电话"
SELECTION_TTL = 600
ATTACH_TTL = 900
RELAY_TIMEOUT = 8
TARGET_TIMEOUT = 3  # each read of the relay's answer; voice_agent waits 5 s for the target
TARGET_DEADLINE = 4  # the whole lookup: a target found later is not held, the call has moved on
FOCUS_TIMEOUT = 15  # the relay may wait 2 s behind a send, then give the link 10 s
SEND_TIMEOUT = 60  # opening the session and finding its prompt box takes a few seconds


def relay_post(url, key, path, payload, timeout=RELAY_TIMEOUT):
    return hubauth.post(url, key, path, payload, timeout=timeout, signed_answer=True)


class DeskGateway:
    def __init__(self, key, relay_url=RELAY_URL, post=relay_post):
        self.key, self.relay_url, self.post = key, relay_url, post
        self.lock = threading.Lock()
        self.cursor = 0
        self.listed = []       # the list the phone screen shows, for its picture and Open
        self.selection = None  # (session, when)
        self.attached = {}     # transcript session id -> expiry

    def _call(self, path, payload, timeout=RELAY_TIMEOUT):
        return self.post(self.relay_url, self.key, path, payload, timeout=timeout)

    def sessions(self, timeout=RELAY_TIMEOUT):
        return self._call("/sessions", {}, timeout=timeout)["sessions"]

    def refresh(self):
        sessions = self.sessions()
        with self.lock:
            self.listed = sessions
            self.cursor = max(0, min(self.cursor, len(sessions) - 1))
        return sessions

    def view(self, host_id):
        return self._call("/view", {"id": host_id})

    def send(self, host_id, text):
        result = self._call("/send", {"id": host_id, "text": text}, timeout=SEND_TIMEOUT)
        if result.get("ok") and result.get("session_id"):
            with self.lock:  # a call still on stays held; one that ended is not taken back
                if self.attached.get(result["session_id"], 0) > time.time():
                    self.attached[result["session_id"]] = time.time() + ATTACH_TTL
        return result

    def reply(self, host_id, offset, sent):
        return self._call("/reply", {"id": host_id, "offset": offset, "sent": sent})

    def focus(self, session_id):
        """Bring the desktop session running this transcript to the front: {"ok", "error"}."""
        return self._call("/focus", {"session_id": session_id}, timeout=FOCUS_TIMEOUT)

    def attach(self, session_id):
        with self.lock:
            self.attached[session_id] = time.time() + ATTACH_TTL

    def in_call(self, session_id):
        with self.lock:
            return self.attached.get(session_id, 0) > time.time()

    def end(self, session_id):
        with self.lock:
            self.attached.pop(session_id, None)

    def select(self, session):
        with self.lock:
            self.selection = (session, time.time())

    def target(self):
        """The session a call to 300 talks to, as the relay lists it now (with its current
        transcript id), or None. Relay errors propagate."""
        sessions = self.sessions(timeout=TARGET_TIMEOUT)
        with self.lock:
            selected = self.selection
        if selected and time.time() - selected[1] < SELECTION_TTL:
            found = next((s for s in sessions if s.get("id") == selected[0].get("id")), None)
            if found:
                return found
        return next((s for s in sessions if s.get("title") == PHONE_SESSION), None)

    def move(self, delta):
        with self.lock:
            self.cursor = max(0, min(self.cursor + delta, len(self.listed) - 1))
            return self.cursor

    def at_cursor(self):
        with self.lock:
            return self.listed[self.cursor] if 0 <= self.cursor < len(self.listed) else None

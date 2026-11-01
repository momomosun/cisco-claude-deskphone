"""HMAC request signing shared by voice_agent, desk_relay and phone-hub's calls to the relay.

A request carries X-Hub-Time, X-Hub-Nonce and X-Hub-Signature: HMAC-SHA256 over time, nonce,
path and body joined by newlines, with a key both ends hold and never send. The receiver
accepts it once, within SIG_WINDOW seconds. desk_relay also signs its answers (X-Hub-Answer over
the request's nonce and the answer body), so the hub cannot be handed a forged or replayed one,
say with another session's id in it. Stdlib only. claude_hooks/phone_hook.py keeps its
own copy of sign() because it runs outside this directory; phone_hub.py checks hook requests
inline (it refuses an unsigned request before reading its body).
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.request

SIG_WINDOW = 120
MAX_ANSWER = 1 << 20


def sign(key, *parts):
    mac = hmac.new(key.encode(), digestmod=hashlib.sha256)
    for i, part in enumerate(parts):
        if i:
            mac.update(b"\n")
        mac.update(part if isinstance(part, bytes) else str(part).encode())
    return mac.hexdigest()


def signed_headers(key, path, body):
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    return {"Content-Type": "application/json", "X-Hub-Time": ts, "X-Hub-Nonce": nonce,
            "X-Hub-Signature": sign(key, ts, nonce, path, body)}


def post(base_url, key, path, payload, timeout=5, signed_answer=False):
    """Signed JSON POST; returns the decoded answer. Raises on network or HTTP errors, and, with
    signed_answer, on an answer not signed for this request."""
    body = json.dumps(payload).encode()
    headers = signed_headers(key, path, body)
    req = urllib.request.Request(base_url + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data, given = resp.read(MAX_ANSWER), resp.headers.get("X-Hub-Answer") or ""
    if signed_answer and not hmac.compare_digest(given, sign(key, headers["X-Hub-Nonce"], data)):
        raise ValueError("answer not signed for this request")
    return json.loads(data)


class NonceCache:
    """Nonces seen inside the signature window, so a captured request cannot be replayed."""

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


def verify(key, headers, path, body, nonces, window=SIG_WINDOW):
    """True for a correctly signed, fresh request whose nonce was not seen before."""
    ts, nonce = headers.get("X-Hub-Time") or "", headers.get("X-Hub-Nonce") or ""
    given = headers.get("X-Hub-Signature") or ""
    try:
        fresh = abs(time.time() - int(ts)) <= window
    except ValueError:
        return False
    if not (fresh and nonce and given):
        return False
    if not hmac.compare_digest(given, sign(key, ts, nonce, path, body)):
        return False
    return nonces.add(nonce)

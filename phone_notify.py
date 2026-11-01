#!/usr/bin/env python3
"""Put a message on the desk phone (phone_hub.py /notify) from any program on the LAN.

    phone_notify.py "crypto bot" "仓位亏损超过 5%" --warn    # comes up on the phone now and rings
    phone_notify.py "备份" "今晚备份完成"                     # waits under Messages in the services menu
    echo "long text" | phone_notify.py "title" -             # text from stdin

The request is signed (HMAC) with the hub's key, which never goes over the wire. The key is read
from PHONE_HUB_TOKEN_FILE (default ~/.claude/phone-hub.token, as the Claude hooks use), or, on the
Mac Mini itself, from HUB_HOOK_PASSWORD in secrets.env next to this script. Stdlib only, so the file
can be copied to another machine as it is. Exits 0 once the hub took the message, 1 otherwise.
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request

HUB = os.environ.get("PHONE_HUB_URL", "http://192.168.1.10:8090")
TOKEN_FILE = os.path.expanduser(os.environ.get("PHONE_HUB_TOKEN_FILE", "~/.claude/phone-hub.token"))
SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.env")
PATH = "/notify"


def sign(key, *parts):
    """Same construction as phone_hub.sign."""
    mac = hmac.new(key.encode(), digestmod=hashlib.sha256)
    for i, part in enumerate(parts):
        if i:
            mac.update(b"\n")
        mac.update(part if isinstance(part, bytes) else str(part).encode())
    return mac.hexdigest()


def read_key():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    if os.path.exists(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                if line.startswith("HUB_HOOK_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    return ""


def notify(title, text, warning=False, key=None, hub=HUB, timeout=5):
    """Post the message; returns its id on the hub. Raises on a missing key, network or HTTP error."""
    key = key if key is not None else read_key()
    if not key:
        raise RuntimeError(f"no hub key: neither {TOKEN_FILE} nor HUB_HOOK_PASSWORD in {SECRETS}")
    body = json.dumps({"level": "warning" if warning else "info", "title": title, "text": text}).encode()
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    req = urllib.request.Request(hub + PATH, data=body, headers={
        "Content-Type": "application/json", "X-Hub-Time": ts, "X-Hub-Nonce": nonce,
        "X-Hub-Signature": sign(key, ts, nonce, PATH, body)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        answer = json.loads(resp.read(65536))
    if not (isinstance(answer, dict) and answer.get("ok")):
        raise RuntimeError(f"hub refused the message: {answer!r}")
    return answer.get("id")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Put a message on the desk phone.")
    parser.add_argument("title")
    parser.add_argument("text", nargs="?", default="", help='the message; "-" reads it from stdin')
    parser.add_argument("--warn", action="store_true", help="show it on the phone now and ring")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.text == "-" else args.text
    try:
        notify(args.title, text, warning=args.warn)
    except Exception as exc:
        print(f"phone_notify: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

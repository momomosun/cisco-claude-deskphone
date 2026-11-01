#!/usr/bin/env python3
"""Look at the desk phone's screen (run on the Mini, next to phone-hub):

    phone_screen.py OUT.bmp                save what the phone shows now
    phone_screen.py /phone/desk OUT.bmp    open that phone-hub page on the phone first

Uses phone-hub's loopback-only /local/show and /local/screenshot, signed with the hook key.
"""
import base64
import os
import sys
import time

import hubauth
import render

HERE = os.path.dirname(os.path.abspath(__file__))
HUB_URL = "http://127.0.0.1:8090"
BASE_URL = os.environ.get("HUB_BASE_URL", "http://192.168.1.10:8090")  # as phone_hub.BASE_URL
PAGE_WAIT = 2  # seconds for the phone to fetch and draw a page


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    key = render.load_secrets(os.path.join(HERE, "secrets.env"))["HUB_HOOK_PASSWORD"]
    if len(argv) == 3:
        if not hubauth.post(HUB_URL, key, "/local/show", {"url": BASE_URL + argv[1]}).get("ok"):
            sys.exit(f"the phone did not open {argv[1]}")
        time.sleep(PAGE_WAIT)
    got = hubauth.post(HUB_URL, key, "/local/screenshot", {}, timeout=20)
    if "image" not in got:
        sys.exit(got.get("error") or "no screenshot")
    with open(argv[-1], "wb") as f:
        f.write(base64.b64decode(got["image"]))
    print(f"{argv[-1]}: {got.get('type')}")


if __name__ == "__main__":
    main(sys.argv)

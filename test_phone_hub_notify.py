"""Messages from other programs on the desk phone: phone_notify.py -> phone-hub /notify -> the phone."""
import json
import time
import urllib.error

import pytest

import phone_hub
import phone_notify
from test_phone_hub import HOOK_KEY, get, hub_server, picture, post, softkey_urls, wait_for_push  # noqa: F401


def test_a_warning_comes_up_on_the_phone_and_rings_once(hub_server, monkeypatch):
    hub, push, base, *_ = hub_server
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0)
    rings = []
    hub.ring = lambda: rings.append(time.time()) or True
    status, answer = post(base, "/notify", {"level": "warning", "title": "crypto bot", "text": "仓位亏损超过 5%"})
    assert status == 200 and answer["ok"]
    url = wait_for_push(push)
    assert url == f"{base}/phone/msg/{answer['id']}"
    _, _, page = get(url)
    assert set(softkey_urls(page)) == {"OK"}
    get(picture(page))
    get(picture(page))  # looking again is quiet
    time.sleep(0.2)
    assert len(rings) == 1


def test_info_waits_under_messages_without_ringing(hub_server):
    hub, push, base, *_ = hub_server
    hub.ring = lambda: pytest.fail("an info message must not ring")
    post(base, "/notify", {"title": "备份", "text": "今晚备份完成"})
    time.sleep(0.1)
    assert push.urls == []
    _, _, menu = get(f"{base}/phone/menu")
    assert b"Messages (1)" in menu
    _, _, page = get(f"{base}/phone/msgs")
    assert set(softkey_urls(page)) == {"Exit"}
    status, ctype, png = get(picture(page))
    assert status == 200 and ctype == "image/png" and png[:4] == b"\x89PNG"


def test_older_and_newer_step_through_the_messages(hub_server):
    hub, _, base, *_ = hub_server
    ids = [post(base, "/notify", {"title": f"m{i}"})[1]["id"] for i in range(3)]
    _, _, newest = get(f"{base}/phone/msgs")
    keys = softkey_urls(newest)
    assert "Newer" not in keys and keys["Older"].endswith(f"/phone/msg/{ids[1]}")
    _, _, middle = get(keys["Older"])
    keys = softkey_urls(middle)
    assert keys["Older"].endswith(ids[0]) and keys["Newer"].endswith(ids[2])


def test_a_warning_holds_the_panel_until_its_ok(hub_server):
    hub, push, base, *_ = hub_server
    post(base, "/notify", {"title": "info", "text": "x"})  # an info message never takes the panel
    assert b"panel.png" in get(f"{base}/phone/panel")[2]
    msg_id = post(base, "/notify", {"level": "warning", "title": "t", "text": "x"})[1]["id"]
    _, _, page = get(wait_for_push(push))
    ok = softkey_urls(page)["OK"]
    assert ok == f"{base}/phone/msg/{msg_id}/ok"
    # the idle panel (60 s without a key) would have covered it: it shows the warning instead
    assert f"/phone/msg/{msg_id}.png".encode() in get(f"{base}/phone/panel")[2]
    assert b"panel.png" in get(ok)[2]  # OK: seen, back to the panel
    assert b"panel.png" in get(f"{base}/phone/panel")[2]


def test_an_old_unacknowledged_warning_lets_the_panel_go(hub_server, monkeypatch):
    hub, push, base, *_ = hub_server
    monkeypatch.setattr(phone_hub, "WARNING_HOLD", 0)
    post(base, "/notify", {"level": "warning", "title": "t"})
    time.sleep(0.05)
    assert b"panel.png" in get(f"{base}/phone/panel")[2]


def test_messages_are_trimmed_and_capped(hub_server, monkeypatch):
    hub, _, base, *_ = hub_server
    monkeypatch.setattr(phone_hub, "MAX_MESSAGES", 3)
    for i in range(5):
        post(base, "/notify", {"title": "t" * 500 + "\n", "text": "x" * 5000})
    assert len(hub.messages) == 3
    assert len(hub.messages[0]["title"]) == phone_hub.MESSAGE_TITLE
    assert len(hub.messages[0]["text"]) == phone_hub.MESSAGE_TEXT


def test_unsigned_messages_are_refused(hub_server):
    hub, push, base, *_ = hub_server
    status, _ = post(base, "/notify", {"level": "warning", "title": "x"}, headers={"Content-Type": "application/json"})
    assert status == 401 and hub.messages == [] and push.urls == []


def test_the_command_line_tool_posts_a_signed_message(hub_server):
    hub, push, base, *_ = hub_server
    msg_id = phone_notify.notify("crypto bot", "仓位亏损超过 5%", warning=True, key=HOOK_KEY, hub=base)
    assert hub.messages[0]["id"] == msg_id and hub.messages[0]["text"] == "仓位亏损超过 5%"
    assert hub.messages[0]["level"] == "warning"
    with pytest.raises(urllib.error.HTTPError):
        phone_notify.notify("x", "y", key="wrong", hub=base)


def test_the_command_line_tool_fails_loudly_without_a_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(phone_notify, "TOKEN_FILE", str(tmp_path / "none"))
    monkeypatch.setattr(phone_notify, "SECRETS", str(tmp_path / "none.env"))
    assert phone_notify.main(["title", "text"]) == 1
    assert "no hub key" in capsys.readouterr().err


def test_the_command_line_tool_reads_the_minis_secrets(monkeypatch, tmp_path):
    env = tmp_path / "secrets.env"
    env.write_text("PHONE_101_PASSWORD=a\nHUB_HOOK_PASSWORD=the-key\n")
    monkeypatch.setattr(phone_notify, "TOKEN_FILE", str(tmp_path / "none"))
    monkeypatch.setattr(phone_notify, "SECRETS", str(env))
    assert phone_notify.read_key() == "the-key"

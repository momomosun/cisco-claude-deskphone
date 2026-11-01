"""phone-hub over real HTTP on localhost, with a fake phone push."""
import base64
import email.utils
import http.client
import http.server
import io
import ipaddress
import json
import os
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.dom.minidom

import pytest
from PIL import Image

import phone_hub
import phone_render

HOOK_KEY = "hook-secret"


class FakePush:
    def __init__(self, ok=True):
        self.ok = ok
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.ok


@pytest.fixture
def hub_server(monkeypatch):
    monkeypatch.setattr(phone_hub, "PHONE_IP", "127.0.0.1")
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("127.0.0.0/8"))
    monkeypatch.setattr(phone_hub, "SOCKET_TIMEOUT", 2)
    monkeypatch.setattr(phone_hub, "REQUEST_DEADLINE", 3)
    state = os.path.join(tempfile.mkdtemp(), "state.json")
    push, push_auth = FakePush(), phone_hub.PushAuth()
    hub = phone_hub.Hub(push, wait_seconds=2, state_path=state)
    server = phone_hub.HubServer(("127.0.0.1", 0), phone_hub.make_handler(hub, HOOK_KEY, push_auth),
                                 max_connections=4)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(phone_hub, "BASE_URL", base)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, push, base, state, push_auth, server
    server.shutdown()


def signed_headers(path, body, key=HOOK_KEY, ts=None, nonce=None):
    ts = str(int(time.time())) if ts is None else ts
    nonce = nonce or secrets.token_hex(8)
    return {"Content-Type": "application/json", "X-Hub-Time": ts, "X-Hub-Nonce": nonce,
            "X-Hub-Signature": phone_hub.sign(key, ts, nonce, path, body)}, nonce


def post(base, path, payload, headers=None, timeout=10):
    body = json.dumps(payload).encode()
    if headers is None:
        headers, _ = signed_headers(path, body)
    req = urllib.request.Request(base + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        return err.code, None


def get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def permission(**kw):
    return {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/Users/x/cisco-phone",
            "tool_name": "Bash", "tool_input": {"command": "ls -la", "description": "列出文件"}, **kw}


def ask_in_background(base, payload):
    result = {}
    t = threading.Thread(target=lambda: result.update(zip(("status", "body"), post(base, "/hook/permission", payload))))
    t.start()
    return t, result


def wait_for_push(push, n=1):
    for _ in range(100):
        if len(push.urls) >= n:
            return push.urls[n - 1]
        time.sleep(0.02)
    raise AssertionError("no push")


def softkey_urls(page):
    doc = xml.dom.minidom.parseString(page)
    return {k.getElementsByTagName("Name")[0].firstChild.data: k.getElementsByTagName("URL")[0].firstChild.data
            for k in doc.getElementsByTagName("SoftKeyItem")}


# --- hook authentication -----------------------------------------------------------
def test_hook_requests_must_be_signed_fresh_and_unique(hub_server):
    _, _, base, _, _, _ = hub_server
    path, body = "/hook/event", json.dumps({}).encode()
    assert post(base, path, {}, headers={"Content-Type": "application/json"})[0] == 401
    assert post(base, path, {}, headers=signed_headers(path, body, key="wrong")[0])[0] == 401
    stale = str(int(time.time()) - 3600)
    assert post(base, path, {}, headers=signed_headers(path, body, ts=stale)[0])[0] == 401
    headers, _ = signed_headers(path, body)
    assert post(base, path, {}, headers=headers)[0] == 200
    assert post(base, path, {}, headers=headers)[0] == 401  # replay of the same nonce


def test_signature_covers_the_body(hub_server):
    _, _, base, _, _, _ = hub_server
    headers, _ = signed_headers("/hook/event", json.dumps({"a": 1}).encode())
    assert post(base, "/hook/event", {"a": 2}, headers=headers)[0] == 401


def test_hook_from_outside_hook_net_is_forbidden(hub_server, monkeypatch):
    _, _, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("10.0.0.0/8"))
    monkeypatch.setattr(phone_hub, "LOOPBACK", ipaddress.ip_network("10.0.0.0/8"))
    assert post(base, "/hook/event", {})[0] == 403


def test_hooks_from_this_machine_count_and_local_routes_are_only_for_it(hub_server, monkeypatch):
    _, _, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("10.0.0.0/8"))
    assert post(base, "/hook/event", {})[0] == 200  # loopback is always a hook client
    assert post(base, "/local/pending", {"session_id": "x"})[0] == 200
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("127.0.0.0/8"))
    monkeypatch.setattr(phone_hub, "LOOPBACK", ipaddress.ip_network("10.0.0.0/8"))
    assert post(base, "/hook/event", {})[0] == 200
    assert post(base, "/local/pending", {"session_id": "x"})[0] == 403
    assert post(base, "/local/decide", {"id": "x", "session_id": "x", "decision": "allow"})[0] == 403


def test_local_routes_need_the_hook_key(hub_server):
    _, _, base, _, _, _ = hub_server
    body = json.dumps({"session_id": "x"}).encode()
    headers = signed_headers("/local/pending", body, key="wrong")[0]
    assert post(base, "/local/pending", {"session_id": "x"}, headers=headers)[0] == 401


def test_negative_or_oversized_content_length_is_rejected(hub_server, monkeypatch):
    _, _, base, _, _, _ = hub_server
    host, port = base[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/hook/event")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 413
    monkeypatch.setattr(phone_hub, "MAX_BODY", 100)
    assert post(base, "/hook/event", {"message": "x" * 500})[0] == 413


# --- permission flow -----------------------------------------------------------------
def test_phone_pages_only_for_the_phone_ip(hub_server, monkeypatch):
    _, _, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "PHONE_IP", "10.1.1.1")
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "/phone/menu")
    assert err.value.code == 403


def test_desk_mode_falls_back_to_the_computer_immediately(hub_server):
    hub, push, base, _, _, _ = hub_server
    hub.set_mode("desk")
    started = time.time()
    assert post(base, "/hook/permission", permission())[1]["decision"] is None
    assert time.time() - started < 1 and push.urls == []


@pytest.mark.parametrize("verdict", ["allow", "deny"])
def test_phone_softkey_answers_the_waiting_hook_with_a_signed_answer(hub_server, verdict):
    hub, push, base, _, _, _ = hub_server
    body = json.dumps(permission()).encode()
    headers, nonce = signed_headers("/hook/permission", body)
    result = {}
    t = threading.Thread(target=lambda: result.update(answer=post(base, "/hook/permission", permission(),
                                                                    headers=headers)[1]))
    t.start()
    url = wait_for_push(push)
    page = get(url)[2]
    keys = softkey_urls(page)
    assert set(keys) == {"Allow", "Deny", "Exit"}
    png_url = xml.dom.minidom.parseString(page).getElementsByTagName("URL")[0].firstChild.data
    assert get(png_url)[2].startswith(b"\x89PNG")
    get(keys["Allow" if verdict == "allow" else "Deny"])
    t.join(5)
    assert result["answer"]["decision"] == verdict
    assert result["answer"]["sig"] == phone_hub.sign(HOOK_KEY, nonce, verdict)
    assert hub.pending == {}


def test_first_answer_wins(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, result = ask_in_background(base, permission())
    keys = softkey_urls(get(wait_for_push(push))[2])
    get(keys["Deny"])
    assert b"Already answered" in get(keys["Allow"])[2] or b"no longer open" in get(keys["Allow"])[2]
    t.join(5)
    assert result["body"]["decision"] == "deny"


def test_decide_records_only_the_first_answer():
    # direct check: in the HTTP flow the waiter removes the request almost at once, which hides
    # whether a second press could overwrite the first
    hub = phone_hub.Hub(FakePush())
    req = phone_hub.Request("s", "Bash", "$ ls")
    hub.pending[req.id] = req
    assert hub.decide(req.id, req.token, "deny") is True
    assert hub.decide(req.id, req.token, "allow") is False
    assert req.decision == "deny"
    req2 = phone_hub.Request("s", "Bash", "$ ls")
    req2.closed = True
    hub.pending[req2.id] = req2
    assert hub.decide(req2.id, req2.token, "allow") is False and req2.decision is None


def test_panel_shows_a_waiting_request_instead_of_the_status(hub_server):
    # the panel auto-refreshes and is also the idle screen; it must not hide a pending approval
    hub, push, base, _, _, _ = hub_server
    t, result = ask_in_background(base, permission())
    wait_for_push(push)
    keys = softkey_urls(get(base + "/phone/panel")[2])
    assert {"Allow", "Deny"} <= set(keys)
    get(keys["Allow"])
    t.join(5)
    assert result["body"]["decision"] == "allow"
    assert "Allow" not in softkey_urls(get(base + "/phone/panel")[2])


def test_wrong_token_cannot_decide_and_request_times_out(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, result = ask_in_background(base, permission())
    url = wait_for_push(push)
    rid = urllib.parse.urlparse(url).path.split("/")[-1]
    assert b"no longer open" in get(f"{base}/phone/req/{rid}/allow?t=guess")[2]
    t.join(5)
    assert result["body"]["decision"] is None


@pytest.mark.parametrize("payload", [
    permission(tool_input={"command": "ls", "dangerouslyDisableSandbox": True}),
    permission(tool_input={"command": "ls", "run_in_background": True}),
    permission(tool_name="Write", tool_input={"file_path": "/x/a.txt", "content": "hello"}),
    permission(tool_name="Edit", tool_input={"file_path": "/x/a.txt", "old_string": "a", "new_string": "b"}),
    permission(tool_input={"command": "echo " + "x" * 400}),
    permission(truncated=True),
])
def test_requests_the_phone_cannot_show_in_full_stay_on_the_computer(hub_server, payload):
    hub, push, base, _, _, _ = hub_server
    started = time.time()
    assert post(base, "/hook/permission", payload)[1]["decision"] is None
    assert push.urls == [] and time.time() - started < 1


def test_long_description_is_dropped_so_the_command_stays_visible(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, _ = ask_in_background(base, permission(tool_input={"command": "ls -la", "description": "说明" * 200}))
    wait_for_push(push)
    req = next(iter(hub.pending.values()))
    assert req.text == "$ ls -la"
    hub.decide(req.id, req.token, "deny")
    t.join(5)


def test_bash_timeout_field_is_harmless_and_still_goes_to_the_phone(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, _ = ask_in_background(base, permission(tool_input={"command": "ls", "timeout": 60000}))
    wait_for_push(push)
    req = next(iter(hub.pending.values()))
    hub.decide(req.id, req.token, "deny")
    t.join(5)


def test_a_stalled_push_is_abandoned(hub_server, monkeypatch):
    hub, push, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "PUSH_DEADLINE", 0.5)
    hub.push = lambda url: time.sleep(5) or True
    started = time.time()
    assert post(base, "/hook/permission", permission())[1]["decision"] is None
    assert time.time() - started < 2 and hub.pending == {}


def test_failed_push_falls_back_without_waiting(hub_server):
    hub, push, base, _, _, _ = hub_server
    push.ok = False
    started = time.time()
    assert post(base, "/hook/permission", permission())[1]["decision"] is None
    assert time.time() - started < 1 and hub.pending == {}


# --- a phone call to Claude (voice_agent answers with the keypad) ----------------------
def call_permission(**kw):
    return permission(session_id="call-1", cwd="/Users/x/phone-claude", phone_call=True, **kw)


def local_pending(base, session):
    return post(base, "/local/pending", {"session_id": session})[1]["requests"]


def wait_for_local_pending(base, session):
    for _ in range(100):
        reqs = local_pending(base, session)
        if reqs:
            return reqs
        time.sleep(0.02)
    raise AssertionError("nothing pending")


@pytest.mark.parametrize("push_ok", [True, False])
def test_keypad_answers_a_call_request_even_in_desk_mode(hub_server, push_ok):
    hub, push, base, _, _, _ = hub_server
    hub.set_mode("desk")  # mo is on the phone, so the phone answers whatever the mode says
    push.ok = push_ok     # and the keypad still works if the screen push fails
    t, result = ask_in_background(base, call_permission())
    reqs = wait_for_local_pending(base, "call-1")
    assert [(r["tool_name"], r["command"], r["description"]) for r in reqs] == [("Bash", "ls -la", "列出文件")]
    assert local_pending(base, "someone-else") == []

    def decide(session, decision):
        return post(base, "/local/decide", {"id": reqs[0]["id"], "session_id": session,
                                            "decision": decision})[1]["ok"]

    assert decide("someone-else", "allow") is False  # only the call's own requests
    assert decide("call-1", "maybe") is False
    assert decide("call-1", "allow") is True
    t.join(5)
    assert result["body"]["decision"] == "allow" and hub.pending == {}


def test_unanswered_call_request_times_out_and_only_true_marks_a_call(hub_server):
    hub, push, base, _, _, _ = hub_server
    assert post(base, "/hook/permission", call_permission())[1]["decision"] is None  # 2 s wait
    hub.set_mode("desk")
    started = time.time()
    assert post(base, "/hook/permission", permission(phone_call="yes"))[1]["decision"] is None
    assert time.time() - started < 1


def test_keypad_answers_only_its_own_open_call_requests():
    hub = phone_hub.Hub(FakePush())
    unnamed = phone_hub.Request("?", "Bash", "$ ls", phone_call=True)  # payload without session_id
    hub.pending[unnamed.id] = unnamed
    assert hub.pending_for("?") == [] and hub.decide_in_call(unnamed.id, "?", "allow") is False
    desk = phone_hub.Request("call-1", "Bash", "$ ls")  # same session id, but not from a call
    hub.pending[desk.id] = desk
    assert hub.pending_for("call-1") == [] and hub.decide_in_call(desk.id, "call-1", "allow") is False
    req = phone_hub.Request("call-1", "Bash", "$ ls", phone_call=True, command="ls")
    hub.pending[req.id] = req
    assert hub.pending_for("call-1") == [{"id": req.id, "tool_name": "Bash", "text": "$ ls",
                                          "command": "ls", "description": None}]
    assert hub.decide(req.id, req.token, "deny") is True  # the screen answered first
    assert hub.decide_in_call(req.id, "call-1", "allow") is False
    assert req.decision == "deny" and hub.pending_for("call-1") == []


def test_pending_requests_are_capped(hub_server, monkeypatch):
    hub, push, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "MAX_PENDING", 1)
    t, _ = ask_in_background(base, permission())
    wait_for_push(push)
    assert post(base, "/hook/permission", permission(session_id="s2"))[1]["decision"] is None
    t.join(5)


# --- push authentication -------------------------------------------------------------
def test_push_passwords_are_one_time(hub_server):
    _, _, base, _, push_auth, _ = hub_server
    pw = push_auth.issue()
    other = push_auth.issue()
    assert get(f"{base}/phone/auth?UserID=claude&Password={pw}&devicename=SEP1")[2] == b"AUTHORIZED"
    assert get(f"{base}/phone/auth?UserID=claude&Password={pw}")[2] == b"UN-AUTHORIZED"
    assert get(f"{base}/phone/auth?UserID=root&Password={other}")[2] == b"UN-AUTHORIZED"
    assert get(f"{base}/phone/auth?UserID=claude&Password={other}")[2] == b"AUTHORIZED"
    assert get(f"{base}/phone/auth?UserID=claude&Password=guess")[2] == b"UN-AUTHORIZED"


# --- robustness ----------------------------------------------------------------------
def closed_without_answer(sock):
    """True if the server closed the connection (clean close or reset) without sending anything."""
    try:
        return sock.recv(100) == b""
    except ConnectionResetError:
        return True


def test_silent_clients_are_dropped_and_connections_are_capped(hub_server):
    _, _, base, _, _, server = hub_server
    host, port = base[len("http://"):].split(":")
    idle = [socket.create_connection((host, int(port))) for _ in range(4)]
    time.sleep(0.2)
    extra = socket.create_connection((host, int(port)))
    extra.settimeout(3)
    extra.sendall(b"GET /phone/menu HTTP/1.0\r\n\r\n")
    assert closed_without_answer(extra)  # over the cap: closed without an answer
    for s in idle:  # the idle ones are timed out by the server (SOCKET_TIMEOUT=2)
        s.settimeout(5)
        assert closed_without_answer(s)
    assert get(base + "/phone/menu")[0] == 200  # slots are free again


def test_trickling_clients_are_cut_off_at_the_request_deadline(hub_server, monkeypatch):
    _, _, base, _, _, _ = hub_server
    host, port = base[len("http://"):].split(":")
    s = socket.create_connection((host, int(port)))
    s.settimeout(1)
    started = time.time()
    s.sendall(b"GET /phone/menu HTTP/1.1\r\n")
    closed = False
    while time.time() - started < 8:  # one header byte per 0.5 s keeps the idle timer happy
        try:
            s.sendall(b"X")
            if s.recv(100) == b"":
                closed = True
                break
        except socket.timeout:
            continue
        except (ConnectionResetError, BrokenPipeError):
            closed = True
            break
        time.sleep(0.5)
    assert closed and time.time() - started < phone_hub.REQUEST_DEADLINE + 2


def test_unsigned_posts_are_refused_before_the_body_is_read(hub_server):
    _, _, base, _, _, _ = hub_server
    host, port = base[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/hook/event")
    conn.putheader("Content-Length", "100000")  # promises a body that never comes
    conn.endheaders()
    started = time.time()
    assert conn.getresponse().status == 401 and time.time() - started < 1


def test_untrusted_text_is_escaped_in_phone_xml(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, _ = ask_in_background(base, permission(tool_name='<b>&"x', tool_input={"q": 1}))
    wait_for_push(push)
    menu = get(base + "/phone/pending")[2]
    names = [n.firstChild.data for n in xml.dom.minidom.parseString(menu).getElementsByTagName("Name")]
    assert names[0].startswith('<b>&"x ')
    hub.decide(*next((r.id, r.token) for r in hub.pending.values()), "deny")
    t.join(5)


def test_events_update_the_panel_and_mode_persists(hub_server):
    hub, _, base, state, _, _ = hub_server
    post(base, "/hook/event", {"hook_event_name": "Stop", "session_id": "s9", "cwd": "/w/yellowtag",
                               "last_assistant_message": "改好了，测试全过"})
    post(base, "/hook/event", {"hook_event_name": "Notification", "session_id": "s8", "cwd": "/w/kerbie",
                               "notification_type": "idle_prompt", "message": "Claude is waiting"})
    title, body, footer = hub.panel_text()
    assert "yellowtag 完成" in body and "改好了" in body and "kerbie 等你回复" in body
    assert get(base + "/phone/panel.png")[2].startswith(b"\x89PNG")
    get(base + "/phone/mode/desk/yes")
    assert json.load(open(state)) == {"mode": "desk"}
    assert phone_hub.Hub(FakePush(), state_path=state).mode == "desk"


# --- checking the phone's screen from the Mini -----------------------------------------------
def test_a_screenshot_uses_a_fresh_one_time_password(monkeypatch):
    push_auth, seen = phone_hub.PushAuth(), []

    class Phone(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            user, _, password = base64.b64decode(self.headers["Authorization"].split()[1]).decode().partition(":")
            seen.append(password)
            ok = self.path == "/CGI/Screenshot" and user == phone_hub.PUSH_USER and push_auth.check(password)
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "image/bmp")
            self.end_headers()
            self.wfile.write(b"BM-screen" if ok else b"")

    phone = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phone)
    threading.Thread(target=phone.serve_forever, daemon=True).start()
    monkeypatch.setattr(phone_hub, "PHONE_IP", f"127.0.0.1:{phone.server_address[1]}")
    try:
        shot = phone_hub.screenshot_via_cgi(push_auth)
        assert shot() == ("image/bmp", b"BM-screen") and shot() == ("image/bmp", b"BM-screen")
        assert len(set(seen)) == 2  # each one good for one look only
    finally:
        phone.shutdown()


def test_the_debug_routes_only_open_phone_pages_and_need_the_key(hub_server):
    hub, push, base, _, _, _ = hub_server
    assert post(base, "/local/show", {"url": base + "/phone/desk"})[1] == {"ok": True}
    assert post(base, "/local/show", {"url": "Dial:0011234"})[1] == {"ok": False}
    assert post(base, "/local/show", {"url": "http://evil/phone/x"})[1] == {"ok": False}
    assert post(base, "/local/show", {"url": "Init:Services"})[1] == {"ok": True}  # home screen
    assert post(base, "/local/show", {"url": "Init:Services/x"})[1] == {"ok": False}
    assert push.urls == [base + "/phone/desk", "Init:Services"]
    assert post(base, "/local/screenshot", {})[1] == {"error": "no screenshot"}  # no phone in tests
    assert post(base, "/local/show", {}, headers={"Content-Type": "application/json"})[0] == 401


def test_the_menus_carry_the_claude_icon(hub_server):
    hub, push, base, _, _, _ = hub_server
    doc = xml.dom.minidom.parseString(get(base + "/phone/menu")[2])
    assert doc.documentElement.tagName == "CiscoIPPhoneIconFileMenu"
    index = doc.getElementsByTagName("Title")[0].getAttribute("IconIndex")
    icons = {i.getElementsByTagName("Index")[0].firstChild.data: i.getElementsByTagName("URL")[0].firstChild.data
             for i in doc.getElementsByTagName("IconItem")}
    status, kind, png = get(icons[index])
    assert kind == "image/png" and png.startswith(b"\x89PNG")
    names = [n.firstChild.data for n in doc.getElementsByTagName("Name")]
    assert "Status panel" in names
    # no desk relay or door watch here (a hub for Sam's phone): neither is offered
    assert "Desk sessions" not in names and "Front door" not in names


# --- ringing while an approval waits on the phone ----------------------------------------------
def picture(page):
    return xml.dom.minidom.parseString(page).getElementsByTagName("URL")[0].firstChild.data


def test_the_phone_rings_each_time_the_approval_page_has_loaded(hub_server, monkeypatch):
    hub, push, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0.01)
    rings = []
    hub.ring = lambda: rings.append(time.monotonic()) or True
    t, result = ask_in_background(base, permission())
    page = get(wait_for_push(push))[2]
    time.sleep(0.2)
    assert rings == []  # not while the phone may still be loading the page: a ring would cut it off
    for n in (1, 2):  # the pushed page, then its own refresh every 10 s
        get(picture(page))
        time.sleep(0.2)
        assert len(rings) == n
    get(softkey_urls(page)["Allow"])
    assert get(picture(page))[1] != "image/png" and len(rings) == 2  # answered: gone, and quiet
    t.join(5)
    assert result["body"]["decision"] == "allow"


def test_no_ringing_for_a_call_or_without_a_ringer(hub_server, monkeypatch):
    hub, push, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0.01)
    ask_in_background(base, permission())  # no ringer configured
    get(picture(get(wait_for_push(push))[2]))
    rings = []
    hub.ring = lambda: rings.append(1) or True
    ask_in_background(base, permission(phone_call=True, session_id="s2"))  # a call reads it out instead
    get(picture(get(wait_for_push(push, 2))[2]))
    time.sleep(0.2)
    assert rings == []


def test_ringing_asks_the_phone_to_play_the_ring_file(monkeypatch):
    push_auth, seen = phone_hub.PushAuth(), []

    class Phone(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            user, _, password = base64.b64decode(self.headers["Authorization"].split()[1]).decode().partition(":")
            ok = self.path == "/CGI/Execute" and user == phone_hub.PUSH_USER and push_auth.check(password)
            seen.append(form["XML"][0])
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b'<CiscoIPPhoneResponse><ResponseItem Status="0" Data="" URL=""/></CiscoIPPhoneResponse>'
                             if ok else b"")

    phone = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phone)
    threading.Thread(target=phone.serve_forever, daemon=True).start()
    monkeypatch.setattr(phone_hub, "PHONE_IP", f"127.0.0.1:{phone.server_address[1]}")
    try:
        assert phone_hub.ring_via_cgi(push_auth)() is True
        assert 'URL="Play:ClaudeRing.raw"' in seen[0]
    finally:
        phone.shutdown()


def test_the_ring_can_be_tried_from_the_mini(hub_server):
    hub, push, base, _, _, _ = hub_server
    assert post(base, "/local/ring", {})[1] == {"ok": False}  # this hub has nothing to ring with
    hub.ring = lambda: True
    assert post(base, "/local/ring", {})[1] == {"ok": True}


# --- questions (AskUserQuestion) answered on the phone ------------------------------------------
ASK = {"questions": [
    {"question": "用哪个方案？", "header": "方案", "multiSelect": False,
     "options": [{"label": "甲", "description": "快"}, {"label": "乙", "description": "稳"}]},
    {"question": "要哪些功能？", "header": "功能", "multiSelect": True,
     "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}]}


def ask_question(base, tool_input=ASK, **kw):
    payload = permission(tool_name="AskUserQuestion", tool_input=tool_input, **kw)
    headers, nonce = signed_headers("/hook/permission", json.dumps(payload).encode())
    result = {}
    t = threading.Thread(target=lambda: result.update(zip(("status", "body"), post(
        base, "/hook/permission", payload, headers=headers))))
    t.start()
    return t, result, nonce


def touch_urls(page):
    return [item.getElementsByTagName("URL")[0].firstChild.data
            for item in xml.dom.minidom.parseString(page).getElementsByTagName("MenuItem")]


def test_a_question_is_answered_on_the_phone_and_the_answers_go_back_signed(hub_server):
    hub, push, base, _, _, _ = hub_server
    t, result, nonce = ask_question(base)
    page = get(wait_for_push(push))[2]
    assert list(softkey_urls(page)) == ["1", "2", "Exit"] and len(touch_urls(page)) == 2
    page = get(touch_urls(page)[1])[2]  # touch 乙: on to the next question
    keys = softkey_urls(page)
    assert list(keys) == ["1", "2", "3", "Done"] and len(touch_urls(page)) == 3
    get(keys["1"])
    get(keys["2"])
    get(keys["2"])  # chosen, then unchosen
    get(keys["3"])
    assert b"Answered" in get(keys["Done"])[2]
    t.join(5)
    answers = {"用哪个方案？": "乙", "要哪些功能？": ["A", "C"]}
    assert result["body"] == {"decision": "allow", "answers": answers,
                              "sig": phone_hub.sign(HOOK_KEY, nonce, "allow", phone_hub.canonical(answers))}


@pytest.mark.parametrize("tool_input, extra", [
    ({"questions": [{"question": "叫什么？", "header": "名", "kind": "text"}]}, {}),
    ({"questions": [{"question": "选？", "header": "选", "options": [{"label": str(i)} for i in range(5)]}]}, {}),
    (ASK, {"phone_call": True}),
    (ASK, {"truncated": True}),
])
def test_questions_the_phone_cannot_take_go_to_the_computer(hub_server, tool_input, extra):
    hub, push, base, _, _, _ = hub_server
    started = time.time()
    status, body = post(base, "/hook/permission", permission(tool_name="AskUserQuestion", tool_input=tool_input,
                                                             **extra))
    assert body["decision"] is None and "answers" not in body and time.time() - started < 1 and push.urls == []


def test_allow_and_deny_do_not_answer_a_question(hub_server):
    hub, push, base, _, _, _ = hub_server
    hub.wait_seconds = 1
    t, result, nonce = ask_question(base)
    url = urllib.parse.urlparse(wait_for_push(push))
    token = urllib.parse.parse_qs(url.query)["t"][0]
    get(f"{base}{url.path}/allow?t={urllib.parse.quote(token)}")
    t.join(5)
    assert result["body"]["decision"] is None and "answers" not in result["body"]


# --- the line keys' column -----------------------------------------------------------------
def wide(page):
    """The page and its picture are wide: they cover the line keys' globes with icons of their own."""
    size = Image.open(io.BytesIO(get(picture(page))[2])).size
    return (xml.dom.minidom.parseString(page).documentElement.getAttribute("WindowMode") == "Wide"
            and size == (phone_render.WIDE_WIDTH, phone_render.HEIGHT))


def test_the_panel_approvals_and_questions_cover_the_line_keys(hub_server):
    hub, push, base, _, _, _ = hub_server
    hub.wait_seconds = 1
    assert wide(get(base + "/phone/panel")[2])
    t, result = ask_in_background(base, permission())
    page = get(wait_for_push(push))[2]
    assert wide(page)
    get(softkey_urls(page)["Allow"])
    t.join(5)
    t, result, _ = ask_question(base)
    assert wide(get(wait_for_push(push, 2))[2])
    t.join(5)


def test_the_page_keys_leave_an_approval_or_a_question_on_screen(hub_server):
    hub, push, base, _, _, _ = hub_server
    hub.wait_seconds = 1
    t, result = ask_in_background(base, permission())
    get(wait_for_push(push))  # the phone shows the approval
    for key in ("pageup", "pagedown"):  # line keys 2 and 3, pressed by mistake: it stays
        keys = softkey_urls(get(f"{base}/phone/desk/{key}")[2])
        assert list(keys) == ["Allow", "Deny", "Exit"]
    get(keys["Allow"])
    t.join(5)
    assert result["body"]["decision"] == "allow"
    assert "Allow" not in softkey_urls(get(base + "/phone/desk/pagedown")[2])  # answered: back to their own job
    t, result, _ = ask_question(base)
    get(touch_urls(get(wait_for_push(push, 2))[2])[1])  # first question answered; the second is on screen
    assert list(softkey_urls(get(base + "/phone/desk/pagedown")[2])) == ["1", "2", "3", "Done"]
    t.join(5)  # unanswered, it went back to the computer while still on screen
    assert "Done" not in softkey_urls(get(base + "/phone/desk/pagedown")[2])


# --- waiting on the phone ---------------------------------------------------------------------
def test_the_phone_rings_at_first_then_the_request_waits_quietly(hub_server, monkeypatch):
    hub, push, base, _, _, _ = hub_server
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0.01)
    monkeypatch.setattr(phone_hub, "RING_WINDOW", 0.5)
    rings = []
    hub.ring = lambda: rings.append(time.monotonic()) or True
    t, result = ask_in_background(base, permission())
    page = get(wait_for_push(push))[2]
    get(picture(page))
    time.sleep(0.2)
    assert len(rings) == 1
    time.sleep(0.5)  # past RING_WINDOW: the page's own refreshes no longer ring
    get(picture(page))
    time.sleep(0.2)
    assert len(rings) == 1
    get(softkey_urls(page)["Allow"])
    t.join(5)
    assert result["body"]["decision"] == "allow"  # and it can still be answered


def test_a_hook_that_hangs_up_takes_its_request_off_the_phone(hub_server):
    # the hook hangs up once the computer has the answer (it watches the session's transcript)
    hub, push, base, _, _, _ = hub_server
    hub.wait_seconds = 30
    body = json.dumps(permission()).encode()
    headers, _ = signed_headers("/hook/permission", body)
    conn = http.client.HTTPConnection("127.0.0.1", urllib.parse.urlparse(base).port, timeout=5)
    conn.request("POST", "/hook/permission", body=body, headers=headers)
    url = wait_for_push(push)
    assert "Allow" in softkey_urls(get(url)[2])
    started = time.monotonic()
    conn.close()
    while hub.pending and time.monotonic() - started < 5:
        time.sleep(0.05)
    assert not hub.pending and time.monotonic() - started < 3
    assert b"no longer open" in get(url)[2]  # what the phone's next refresh finds


def test_a_next_step_question_goes_to_the_phone_only_when_away(hub_server):
    hub, push, base, _, _, _ = hub_server
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "mcp__ccd_turn__wrap_up"}
    for mode, extra, redirect in (("away", {}, True), ("away", {"phone_call": True}, False), ("desk", {}, False)):
        hub.set_mode(mode)
        p = {**payload, **extra}
        headers, nonce = signed_headers("/hook/next-step", json.dumps(p).encode())
        status, answer = post(base, "/hook/next-step", p, headers=headers)
        assert answer == {"redirect": redirect, "sig": phone_hub.sign(HOOK_KEY, nonce, "redirect" if redirect else "keep")}
    assert push.urls == []  # the phone hears of it only when the session then asks


# --- the phone's history -----------------------------------------------------------------------
def expired(url):
    """Whether the page at url came with an Expires date already past: the phone keeps such pages
    out of its history, so Exit never goes back to them."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        stamp = resp.headers.get("Expires")
    return bool(stamp) and email.utils.parsedate_to_datetime(stamp).timestamp() < time.time()


def test_answered_requests_do_not_come_back_on_exit(hub_server):
    hub, push, base, _, _, _ = hub_server
    hub.wait_seconds = 2
    t, result = ask_in_background(base, permission())
    url = wait_for_push(push)
    assert expired(url) and expired(base + "/phone/panel")  # the approval, also as the panel shows it
    allow = softkey_urls(get(url)[2])["Allow"]
    assert expired(allow)  # "Allowed": Exit from it goes back past the approval
    t.join(5)
    assert expired(url)  # "no longer open"
    t, result, _ = ask_question(base)
    url = wait_for_push(push, 2)
    assert expired(url)
    second = touch_urls(get(url)[2])[1]
    assert expired(second)  # the next question
    keys = softkey_urls(get(second)[2])
    get(keys["1"])
    assert expired(keys["Done"])  # "Answered: sent to Claude."
    t.join(5)
    assert not expired(base + "/phone/menu")  # pages to come back to stay in the history


# --- after an answer, back to what was on screen ------------------------------------------
def answer_a_question(base, push, count):
    t, result, _ = ask_question(base)
    page = get(wait_for_push(push, count))[2]
    keys = softkey_urls(get(touch_urls(page)[0])[2])  # 甲, then the second question
    get(keys["1"])
    assert b"Answered" in get(keys["Done"])[2]
    t.join(5)


def wait_for_urls(push, count, seconds=3):
    deadline = time.time() + seconds
    while len(push.urls) < count and time.time() < deadline:
        time.sleep(0.05)
    return push.urls


def test_an_answer_goes_back_to_the_home_screen_after_a_while(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.3)
    hub, push, base, _, _, _ = hub_server
    answer_a_question(base, push, 1)
    assert wait_for_urls(push, 2)[1:] == ["Init:Services"]  # the services page closes: the phone's own screen


def test_an_answer_goes_back_to_the_desk_session_it_came_over(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.3)
    hub, push, base, _, _, _ = hub_server
    hub.screen = ("view", "abc123", 0, 10)  # a desk session was on screen when the question came
    answer_a_question(base, push, 1)
    assert wait_for_urls(push, 2)[1:] == [f"{base}/phone/desk/view/abc123"]


def test_no_going_back_once_another_page_is_up(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.5)
    hub, push, base, _, _, _ = hub_server
    answer_a_question(base, push, 1)
    get(base + "/phone/menu")  # pressed something within the delay
    time.sleep(1)
    assert len(push.urls) == 1


def test_an_approval_also_goes_back(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.3)
    hub, push, base, _, _, _ = hub_server
    t, result = ask_in_background(base, permission())
    get(softkey_urls(get(wait_for_push(push))[2])["Allow"])
    t.join(5)
    assert wait_for_urls(push, 2)[1:] == ["Init:Services"]


def test_an_answer_goes_back_to_the_page_that_was_up(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.3)
    hub, push, base, _, _, _ = hub_server
    get(base + "/phone/panel")  # the panel was on screen when the question came
    answer_a_question(base, push, 1)
    assert wait_for_urls(push, 2)[1:] == [f"{base}/phone/panel"]


def test_a_page_seen_long_ago_is_not_gone_back_to(hub_server, monkeypatch):
    monkeypatch.setattr(phone_hub, "BACK_DELAY", 0.3)
    hub, push, base, _, _, _ = hub_server
    get(base + "/phone/panel")
    hub.last_page = (hub.last_page[0], time.time() - phone_hub.BACK_RECENT - 1)  # left since, likely
    answer_a_question(base, push, 1)
    assert wait_for_urls(push, 2)[1:] == ["Init:Services"]



def test_switching_modes_asks_first(hub_server):
    hub, push, base, _, _, _ = hub_server
    page = get(base + "/phone/mode/desk")[2]  # the panel's Mode key
    assert hub.mode == "away"  # a stray press switches nothing
    keys = softkey_urls(page)
    assert list(keys) == ["Cancel", "Exit", "Switch"]  # not in the second place, where Mode was
    assert b"Refresh" in get(keys["Switch"])[2] and hub.mode == "desk"  # then back on the panel

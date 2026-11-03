"""cell_bridge: the desk phone's calls out through the iPhone, with a stand-in baresip and screen."""
import json

import pytest

import cell_bridge as cb


def netstring(obj):
    data = json.dumps(obj).encode()
    return str(len(data)).encode() + b":" + data + b","


class FakeSock:
    """Hands out the given bytes in small pieces, records what is sent."""

    def __init__(self, data=b"", piece=7):
        self.chunks = [data[i:i + piece] for i in range(0, len(data), piece)]
        self.sent = []

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def commands(self):
        return [json.loads(s.split(b":", 1)[1][:-1])["command"] for s in self.sent]


def incoming(name, uri="sip:101@192.168.1.10"):
    return {"event": True, "class": "call", "type": "CALL_INCOMING", "peerdisplayname": name, "peeruri": uri,
            "id": "c1"}


CLOSED = {"event": True, "class": "call", "type": "CALL_CLOSED", "id": "c1"}


def bridge(screen_answers=None):
    """A bridge with a stand-in screen (answers per command; the leading "cancel" before each dial is
    left out of the record), dialling, and a clock that moves only when the bridge sleeps."""
    dialled, screen, now = [], [], [0.0]
    answers = {"cancel": "NO_BANNER", **(screen_answers or {"call": "CALLED", "hangup": "ENDED", "dump": "== x"})}

    def look(cmd):
        if cmd != "cancel" or screen:
            screen.append(cmd)
        return answers[cmd]

    def sleep(seconds):
        now[0] += seconds

    b = cb.Bridge(dial=dialled.append, screen=look, spawn=lambda f: f(), sleep=sleep, clock=lambda: now[0])
    b.sock = FakeSock()
    return b, dialled, screen


def test_netstrings_are_read_across_pieces():
    events = [{"event": True, "type": "CALL_INCOMING"}, {"response": True, "ok": True}]
    sock = FakeSock(b"".join(netstring(e) for e in events))
    assert list(cb.netstrings(sock)) == events


def test_a_bad_netstring_ends_the_stream():
    with pytest.raises(ValueError):
        list(cb.netstrings(FakeSock(b"5x:hello,")))


@pytest.mark.parametrize("name, uri, number", [
    ("cell-0412345678", "sip:101@192.168.1.10", "0412345678"),
    ("cell-1300123456", "sip:101@192.168.1.10", "1300123456"),
    ("cell-+61412345678", "sip:101@192.168.1.10:5060", "+61412345678"),
    ("cell-000", "sip:101@192.168.1.10", None),           # emergency calls stay on the mobile
    ("cell-04123;rm", "sip:101@192.168.1.10", None),       # nothing but a phone number reaches open
    ("7970G", "sip:101@192.168.1.10", None),               # someone ringing 103 itself
    ("cell-0412345678", "sip:101@10.0.0.9", None),          # not from our PBX
])
def test_only_a_number_from_the_pbx_is_dialled(name, uri, number):
    assert cb.number_of(incoming(name, uri)) == number


def test_a_call_from_the_desk_phone_goes_out_and_its_banner_is_pressed():
    b, dialled, screen = bridge()
    b.on_event(incoming("cell-0412345678"))
    assert dialled == ["0412345678"] and screen == ["call"] and b.active
    # the iPhone call opened the virtual devices in its own format: baresip opens both devices again after it
    sent = [json.loads(m.split(b":", 1)[1][:-1]) for m in b.sock.sent]
    assert [(m["command"], m["params"]) for m in sent] == [("ausrc", "coreaudio,CallSpk 2ch Tap"),
                                                            ("auplay", "coreaudio,CallMic 2ch Feed")]


def test_anything_else_reaching_103_is_hung_up():
    b, dialled, screen = bridge()
    b.on_event(incoming("7970G"))
    assert dialled == [] and screen == [] and b.sock.commands() == ["hangup"] and not b.active


def test_hanging_up_the_desk_phone_ends_the_iphone_call():
    b, _, screen = bridge()
    b.on_event(incoming("cell-0412345678"))
    b.on_event(CLOSED)
    assert screen == ["call", "hangup"] and not b.active


def test_an_end_button_not_found_leaves_what_the_screen_shows_in_the_log(monkeypatch):
    logged = []
    monkeypatch.setattr(cb, "log", logged.append)
    b, _, screen = bridge({"call": "CALLED", "hangup": "NO_BUTTON", "dump": "== Notification Center\nAXButton | 静音"})
    b.on_event(incoming("cell-0412345678"))
    b.on_event(CLOSED)
    assert screen == ["call", "hangup", "dump"] and any("静音" in line for line in logged)


def test_the_iphone_call_ending_hangs_up_the_desk_phone():
    b, _, screen = bridge()
    b.on_event(incoming("cell-0412345678"))
    b.on_call_log("... [com.apple.calls.callservicesd:Default] All calls ended. Clearing system uplink muted cache")
    assert b.sock.commands() == ["ausrc", "auplay", "hangup"] and not b.active
    b.on_event(CLOSED)  # the desk side closing after that does not reach for the screen again
    assert screen == ["call"] and b.sock.commands()[-1] == "hangup"


def test_call_log_lines_mean_nothing_without_a_call():
    b, _, _ = bridge()
    b.on_call_log("All calls ended.")
    b.on_call_log("something else entirely")
    assert b.sock.sent == []


def test_other_events_change_nothing():
    b, dialled, screen = bridge()
    b.on_event(CLOSED)
    b.on_event({"response": True, "ok": True})
    assert dialled == [] and screen == [] and b.sock.sent == []


def test_the_log_shows_only_the_start_of_a_number():
    assert cb.masked("0412345678") == "0412******"


def test_dial_wakes_the_display_then_asks_macos_to_call_through_the_iphone(monkeypatch):
    ran = []
    monkeypatch.setattr(cb.subprocess, "run", lambda args, **kw: ran.append(args))
    cb.dial("0412345678")
    assert ran == [["/usr/bin/caffeinate", "-u", "-t", "2"], ["/usr/bin/open", "tel://0412345678"]]


def test_screen_runs_the_script_and_returns_its_answer(monkeypatch):
    class Done:
        stdout, stderr = "CALLED\n", ""
    ran = []
    monkeypatch.setattr(cb.subprocess, "run", lambda args, **kw: ran.append(args) or Done())
    assert cb.screen("call") == "CALLED"
    assert ran[0][:3] == ["/usr/bin/osascript", "-l", "JavaScript"] and ran[0][-1] == "call"


def test_a_banner_not_found_leaves_what_the_screen_shows_in_the_log(monkeypatch):
    logged = []
    monkeypatch.setattr(cb, "log", logged.append)
    b, dialled, screen = bridge({"call": "NO_BANNER", "dump": "== Notification Center\nAXButton | 呼叫"})
    b.on_event(incoming("cell-0412345678"))
    assert set(screen[:-1]) == {"call"} and screen[-1] == "dump" and any("AXButton | 呼叫" in line for line in logged)



def test_the_last_calls_ending_logged_late_does_not_hang_up_a_new_call():
    b, _, _ = bridge({"call": "NO_BANNER", "dump": ""})
    b.on_event(incoming("cell-0412345678"))  # dialled, its banner not pressed (yet)
    b.on_call_log("All calls ended. Clearing system uplink muted cache")
    assert "hangup" not in b.sock.commands() and b.active



def test_a_banner_is_looked_for_until_it_shows_then_pressed():
    answers = iter(["NO_BANNER", "NO_BANNER", "CALLED"])
    b, dialled, screen = bridge({"hangup": "ENDED", "dump": ""})
    b.screen = lambda cmd: screen.append(cmd) or (next(answers) if cmd == "call" else "NO_BANNER")
    b.on_event(incoming("cell-0412345678"))
    assert screen == ["cancel", "call", "call", "call"] and dialled == ["0412345678"] and b.connected


def test_leftover_banners_are_closed_before_the_number_is_dialled():
    order = []
    b, _, _ = bridge()
    b.screen = lambda cmd: order.append(cmd) or "CALLED"
    b.dial = lambda number: order.append("dial")
    b.on_event(incoming("cell-0412345678"))
    assert order[:3] == ["cancel", "dial", "call"]


def test_the_desk_phone_hanging_up_first_stops_the_wait_and_closes_the_banner():
    b, _, screen = bridge({"hangup": "ENDED", "dump": ""})

    def look(cmd):
        screen.append(cmd)
        if cmd == "call":
            b.on_event(CLOSED)  # hung up while the banner was being looked for
            return "NO_BANNER"
        return "CANCELLED" if cmd == "cancel" else "?"
    b.screen = look
    b.on_event(incoming("cell-0412345678"))
    assert screen == ["cancel", "call", "cancel"] and "hangup" not in screen and not b.connected
    assert b.sock.sent == []  # nor the audio set up for a call that is not there


def test_a_banner_pressed_just_as_the_desk_phone_hangs_up_is_ended_again():
    b, _, screen = bridge({"hangup": "ENDED", "dump": ""})

    def look(cmd):
        screen.append(cmd)
        if cmd == "call":
            b.on_event(CLOSED)
            return "CALLED"
        return {"cancel": "NO_BANNER", "hangup": "ENDED"}[cmd]
    b.screen = look
    b.on_event(incoming("cell-0412345678"))
    assert screen == ["cancel", "call", "hangup"] and not b.connected


def test_the_banner_is_given_up_on_after_the_wait():
    b, _, screen = bridge({"call": "NO_BANNER", "dump": ""})
    b.on_event(incoming("cell-0412345678"))
    calls = screen.count("call")
    assert 0 < calls <= cb.BANNER_WAIT / 0.5 + 1 and screen[-1] == "dump"


def test_a_failing_screen_script_is_an_answer_not_an_exception(monkeypatch):
    def hang(*a, **kw):
        raise cb.subprocess.TimeoutExpired("osascript", cb.SCREEN_TIMEOUT)
    monkeypatch.setattr(cb.subprocess, "run", hang)
    assert cb.screen("call") == "TIMEOUT"
    monkeypatch.setattr(cb.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")))
    assert cb.screen("call").startswith("ERROR")


def test_a_failing_thread_says_so_in_the_log(monkeypatch):
    logged, done = [], cb.threading.Event()
    monkeypatch.setattr(cb, "log", lambda text: logged.append(text) or done.set())

    def broken():
        raise RuntimeError("boom")
    cb.spawn(broken)
    assert done.wait(2) and "broken failed" in logged[0] and "boom" in logged[0]


def test_a_number_with_a_trailing_newline_is_not_dialled():
    assert cb.number_of(incoming("cell-0412345678\n")) is None


def test_a_refused_caller_s_name_stays_out_of_the_log(monkeypatch):
    logged = []
    monkeypatch.setattr(cb, "log", logged.append)
    b, _, _ = bridge()
    b.on_event(incoming("0412345678"))
    assert logged and "0412345678" not in logged[0]


def test_events_that_are_not_objects_are_ignored():
    b, dialled, screen = bridge()
    for event in (["CALL_INCOMING"], "x", None, 3):
        b.on_event(event)
    assert dialled == [] and screen == []

"""desk_gateway: phone-hub's state around the desk relay, with a stand-in relay."""
import time

import pytest

import desk_gateway as dg

PHONE = {"id": "local_phone", "session_id": "s-phone", "title": "电话", "status": "idle"}
APP = {"id": "local_app", "session_id": "s-app", "title": "demo app", "status": "busy"}


class FakeRelay:
    def __init__(self, sessions=(PHONE, APP), send_ok=True, down=False):
        self.sessions, self.send_ok, self.down = list(sessions), send_ok, down
        self.calls = []

    def __call__(self, url, key, path, payload, timeout=5):
        self.calls.append((url, key, path, payload, timeout))
        if self.down:
            raise OSError("no route to host")
        if path == "/sessions":
            return {"sessions": self.sessions}
        if path == "/send":
            return {"ok": self.send_ok, "error": "" if self.send_ok else "busy", "offset": 10,
                    "session_id": "s-app" if payload["id"] == "local_app" else "s-phone"}
        return {"path": path, "payload": payload}


def gateway(**kw):
    relay = FakeRelay(**kw)
    return dg.DeskGateway("k", relay_url="http://studio:8091", post=relay), relay


def test_relay_calls_are_signed_requests_to_the_studio():
    gw, relay = gateway()
    assert gw.sessions() == [PHONE, APP]
    gw.view("local_app")
    gw.reply("local_app", 10, "几点了")
    assert [(c[0], c[1], c[2]) for c in relay.calls] == [("http://studio:8091", "k", "/sessions"),
                                                           ("http://studio:8091", "k", "/view"),
                                                           ("http://studio:8091", "k", "/reply")]
    assert relay.calls[2][3] == {"id": "local_app", "offset": 10, "sent": "几点了"}


def test_a_call_goes_to_the_session_opened_on_the_phone_then_back_to_the_phone_session(monkeypatch):
    gw, _ = gateway()
    assert gw.target() == PHONE
    gw.select(APP)
    assert gw.target() == APP
    later = time.time() + dg.SELECTION_TTL + 1
    monkeypatch.setattr(dg.time, "time", lambda: later)
    assert gw.target() == PHONE


def test_the_target_is_looked_up_afresh_by_its_id():
    gw, relay = gateway()
    gw.select(dict(APP, session_id=""))  # opened from a stale page: no transcript id
    assert gw.target() == APP and relay.calls[-1][4] == dg.TARGET_TIMEOUT < 5  # inside voice_agent's wait
    relay.sessions = [PHONE]  # the selected session closed
    assert gw.target() == PHONE


def test_no_target_without_a_phone_session_and_relay_errors_propagate():
    gw, _ = gateway(sessions=[APP])
    assert gw.target() is None
    gw, _ = gateway(down=True)
    with pytest.raises(OSError):
        gw.target()


def test_the_call_target_counts_as_in_call_until_the_call_ends(monkeypatch):
    gw, _ = gateway()
    gw.attach("s-app")
    assert gw.in_call("s-app") and not gw.in_call("s-phone")
    gw.end("s-app")
    assert not gw.in_call("s-app")
    gw.attach("s-app")
    later = time.time() + dg.ATTACH_TTL + 1
    monkeypatch.setattr(dg.time, "time", lambda: later)
    assert not gw.in_call("s-app")  # a call that never said goodbye does not hold it forever


def test_a_late_send_answer_does_not_put_an_ended_call_back():
    gw, _ = gateway()
    gw.attach("s-app")
    gw.end("s-app")  # the caller hung up while the words were on their way
    assert gw.send("local_app", "跑一下测试")["ok"] is True and not gw.in_call("s-app")


def test_each_send_keeps_a_long_call_held(monkeypatch):
    gw, _ = gateway()
    gw.attach("s-app")
    later = time.time() + dg.ATTACH_TTL - 10
    monkeypatch.setattr(dg.time, "time", lambda: later)
    gw.send("local_app", "再跑一次")
    monkeypatch.setattr(dg.time, "time", lambda: later + 20)
    assert gw.in_call("s-app")


def test_relay_answers_must_be_signed(monkeypatch):
    seen = []
    monkeypatch.setattr(dg.hubauth, "post", lambda *args, **kw: seen.append(kw) or {"sessions": []})
    dg.DeskGateway("k").sessions()
    assert seen == [{"timeout": dg.RELAY_TIMEOUT, "signed_answer": True}]


def test_cursor_stays_inside_the_list_the_phone_shows():
    gw, relay = gateway()
    assert gw.at_cursor() is None  # nothing listed yet
    gw.refresh()
    assert gw.move(-1) == 0 and gw.at_cursor() == PHONE
    assert gw.move(+1) == 1 and gw.move(+1) == 1 and gw.at_cursor() == APP
    relay.sessions = [PHONE]
    gw.refresh()  # the list shrank
    assert gw.cursor == 0 and gw.at_cursor() == PHONE


def test_go_waits_for_the_relay_longer_than_the_relay_may_take():
    import desk_relay
    gw, relay = gateway()
    gw.focus("s-app")
    assert relay.calls[-1][2:4] == ("/focus", {"session_id": "s-app"})
    assert relay.calls[-1][4] > desk_relay.FOCUS_LOCK_WAIT + desk_relay.FOCUS_TIMEOUT

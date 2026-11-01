"""A desktop session that finishes its turn comes up on the phone; GO brings it to the front on the Studio."""
import time

import pytest

import phone_hub
from test_phone_hub import get, hub_server, picture, post, softkey_urls, wait_for_push  # noqa: F401
from test_phone_hub_desk import desk_hub  # noqa: F401

STOP = {"hook_event_name": "Stop", "session_id": "s-app", "cwd": "/Users/x/demo",
        "title": "demo app", "last_assistant_message": "测试全过了，已经推送。"}


@pytest.fixture
def done_hub(desk_hub, monkeypatch):
    hub, desk, relay, base, push = desk_hub
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0)
    relay.focused, relay.focus_answer = [], {"ok": True, "error": ""}
    plain = relay.__call__

    def call(url, key, path, payload, timeout=5):
        if path == "/focus":
            relay.focused.append(payload)
            return relay.focus_answer
        return plain(url, key, path, payload, timeout)

    desk.post = call
    rings = []
    hub.ring = lambda: rings.append(time.time()) or True
    yield hub, relay, base, push, rings


def test_a_finished_session_comes_up_with_go(done_hub):
    hub, relay, base, push, rings = done_hub
    assert post(base, "/hook/event", STOP)[0] == 200
    url = wait_for_push(push)
    _, _, page = get(url)
    keys = softkey_urls(page)
    assert list(keys) == ["GO", "OK"]
    get(picture(page))
    time.sleep(0.1)
    assert len(rings) == 1  # away (the default mode): it rings once
    assert b"/phone/msg/" in get(f"{base}/phone/panel")[2]  # the idle panel would cover it: it holds the panel
    assert b"panel.png" in get(keys["GO"])[2]  # GO: opened on the computer, back to the panel
    assert relay.focused == [{"session_id": "s-app"}]
    assert b"panel.png" in get(f"{base}/phone/panel")[2]  # seen: the panel is the panel again


def test_at_the_desk_it_comes_up_without_ringing(done_hub):
    hub, _, base, push, rings = done_hub
    hub.set_mode("desk")
    post(base, "/hook/event", STOP)
    _, _, page = get(wait_for_push(push))
    get(picture(page))
    time.sleep(0.1)
    assert rings == []


@pytest.mark.parametrize("extra", [{"on_screen": True}, {"phone_call": True}])
def test_a_session_on_screen_or_in_a_call_stays_quiet(done_hub, extra):
    hub, _, base, push, _ = done_hub
    post(base, "/hook/event", {**STOP, **extra})
    time.sleep(0.1)
    assert push.urls == [] and hub.messages == []


def test_a_waiting_approval_is_not_covered(done_hub):
    hub, _, base, push, _ = done_hub
    with hub.lock:
        hub.pending["x"] = phone_hub.Request("s1", "Bash", "ls")
    post(base, "/hook/event", STOP)
    time.sleep(0.1)
    assert push.urls == [] and len(hub.messages) == 1  # it waits; the panel shows it once the approval is done


def test_the_same_session_finishing_again_replaces_its_notice(done_hub):
    hub, _, base, push, _ = done_hub
    post(base, "/hook/event", STOP)
    post(base, "/hook/event", dict(STOP, last_assistant_message="第二轮也完了"))
    wait_for_push(push, 2)
    assert [m["text"] for m in hub.messages] == ["第二轮也完了"]


def test_go_that_fails_says_why(done_hub):
    hub, relay, base, push, _ = done_hub
    relay.focus_answer = {"ok": False, "error": "locked"}
    post(base, "/hook/event", STOP)
    _, _, page = get(wait_for_push(push))
    msg_id = hub.messages[0]["id"]
    answer = get(softkey_urls(page)["GO"])[2]
    assert list(softkey_urls(answer)) == ["GO", "OK"]  # the notice again: GO may be tried once unlocked
    assert hub.messages[0]["note"] == "电脑锁屏了，没打开" and not hub.messages[0]["acked"]
    assert get(picture(answer))[0] == 200
    assert f"/phone/msg/{msg_id}".encode() in get(f"{base}/phone/panel")[2]


def test_markdown_marks_are_left_out(done_hub):
    hub, _, base, push, _ = done_hub
    post(base, "/hook/event", dict(STOP, last_assistant_message="**修了什么：** `render.py`"))
    wait_for_push(push)
    assert hub.messages[0]["text"] == "修了什么： render.py"


def test_without_the_desk_relay_nothing_comes_up(hub_server):
    hub, push, base, *_ = hub_server
    post(base, "/hook/event", STOP)
    time.sleep(0.1)
    assert push.urls == [] and hub.messages == []



def test_a_session_talking_on_the_phone_stays_quiet(done_hub):
    hub, _, base, push, rings = done_hub
    hub.desk.attach("s-app")  # 300 picked this session: its hook carries no phone_call mark
    post(base, "/hook/event", STOP)
    time.sleep(0.1)
    assert push.urls == [] and hub.messages == [] and rings == []


# --- GO on a question: open its session on the computer, the question stays on the phone ----------
from test_phone_hub import ask_question, touch_urls  # noqa: E402


@pytest.mark.parametrize("answer, note", [({"ok": True, "error": ""}, "已在电脑上打开"),
                                          ({"ok": False, "error": "locked"}, "电脑锁屏了，没打开")])
def test_go_on_a_question_opens_its_session_and_leaves_the_question_up(done_hub, answer, note):
    hub, relay, base, push, _ = done_hub
    relay.focus_answer = answer
    t, result, _ = ask_question(base)
    page = get(wait_for_push(push))[2]
    assert list(softkey_urls(page)) == ["1", "2", "GO", "Exit"]
    page = get(softkey_urls(page)["GO"])[2]
    assert relay.focused == [{"session_id": "s1"}]
    assert list(softkey_urls(page)) == ["1", "2", "GO", "Exit"] and len(touch_urls(page)) == 2
    req = next(iter(hub.pending.values()))
    assert req.note == note and req.decision is None
    assert get(picture(page))[0] == 200
    get(touch_urls(page)[0])  # still answerable on the phone
    assert req.step == 1 and req.note == ""  # answering puts the usual footer back
    with hub.lock:
        req.closed = True
    req.done.set()
    t.join(5)


def test_done_after_go_puts_the_usual_footer_back_on_the_next_question(done_hub):
    hub, relay, base, push, _ = done_hub
    several = {"question": "要哪些？", "header": "多", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]}
    t, result, _ = ask_question(base, tool_input={"questions": [several, dict(several, question="还要哪些？")]})
    keys = softkey_urls(get(wait_for_push(push))[2])
    get(keys["1"])
    keys = softkey_urls(get(keys["GO"])[2])
    get(keys["Done"])
    req = next(iter(hub.pending.values()))
    assert req.step == 1 and req.note == ""
    with hub.lock:
        req.closed = True
    req.done.set()
    t.join(5)

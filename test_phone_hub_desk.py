"""phone-hub's desk pages and the routes voice_agent uses, with a stand-in relay on the Studio."""
import io
import ipaddress
import json
import os
import tempfile
import threading
import time
import urllib.parse
import xml.dom.minidom

import pytest
from PIL import Image

import desk_gateway
import phone_hub
import phone_render
from test_desk_gateway import APP, PHONE, FakeRelay
from test_phone_hub import HOOK_KEY, FakePush, get, permission, post, softkey_urls

APP_ID = "local_0718b38e-ae75-4dfe-b44c-3960a7ba5a06"
PHONE_ID = "local_f41cc4d7-bb00-4732-b75b-c3f51f34dea0"


class Relay(FakeRelay):
    turns = [{"role": "user", "text": "跑一下测试"}, {"role": "assistant", "text": "全过了。"}]

    def __call__(self, url, key, path, payload, timeout=5):
        if path in ("/view", "/send") and not self.down:
            self.calls.append((url, key, path, payload, timeout))
            session = next(s for s in self.sessions if s["id"] == payload["id"])
            if path == "/send":
                return {"ok": True, "error": "", "offset": 10, "session_id": session["session_id"]}
            return {"title": session["title"], "status": session["status"], "turns": self.turns}
        return super().__call__(url, key, path, payload, timeout)


@pytest.fixture
def desk_hub(monkeypatch):
    monkeypatch.setattr(phone_hub, "PHONE_IP", "127.0.0.1")
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("127.0.0.0/8"))
    relay = Relay(sessions=[dict(PHONE, id=PHONE_ID), dict(APP, id=APP_ID)])
    desk = desk_gateway.DeskGateway(HOOK_KEY, relay_url="http://studio:8091", post=relay)
    push = FakePush()
    hub = phone_hub.Hub(push, wait_seconds=2, state_path=os.path.join(tempfile.mkdtemp(), "s.json"), desk=desk)
    server = phone_hub.HubServer(("127.0.0.1", 0), phone_hub.make_handler(hub, HOOK_KEY, phone_hub.PushAuth()))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(phone_hub, "BASE_URL", base)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, desk, relay, base, push
    server.shutdown()


def image_url(page):
    return xml.dom.minidom.parseString(page).getElementsByTagName("URL")[0].firstChild.data


def test_the_list_shows_desktop_sessions_and_the_cursor_moves(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    page = get(base + "/phone/desk")[2]
    keys = softkey_urls(page)
    assert list(keys) == ["Open", "Down", "Up", "Exit"]
    assert get(image_url(page))[2].startswith(b"\x89PNG")
    get(keys["Down"])
    assert desk.cursor == 1 and desk.at_cursor()["title"] == "parkabout app"
    get(keys["Up"])
    assert desk.cursor == 0
    assert b"Desk sessions" in get(base + "/phone/menu")[2]


def touch_areas(page):
    doc = xml.dom.minidom.parseString(page)
    assert doc.documentElement.tagName == "CiscoIPPhoneGraphicFileMenu"
    areas = []
    for item in doc.getElementsByTagName("MenuItem"):
        area = item.getElementsByTagName("TouchArea")[0]
        areas.append((item.getElementsByTagName("URL")[0].firstChild.data,
                      tuple(int(area.getAttribute(k)) for k in ("X1", "Y1", "X2", "Y2"))))
    return areas


def test_touching_a_row_opens_that_session(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    areas = [a for a in touch_areas(get(base + "/phone/desk")[2]) if "/view/" in a[0]]  # the rows, not the arrows
    assert [url for url, _ in areas] == [f"{base}/phone/desk/view/{PHONE_ID}", f"{base}/phone/desk/view/{APP_ID}"]
    for k, (_, (x1, y1, x2, y2)) in enumerate(areas):  # on the drawn row, inside the screen
        assert y1 == phone_render.BODY_TOP + k * phone_render.LINE_H - 1 and y2 == y1 + phone_render.LINE_H - 1
        assert 0 <= x1 < x2 < phone_render.WIDTH and y2 < phone_render.HEIGHT
    opened = get(areas[1][0])[2]
    assert softkey_urls(opened)["Talk"] == "Dial:300" and desk.target()["id"] == APP_ID


def test_the_touch_rows_follow_the_scrolled_list(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    ids = [f"local_{n:08d}-0000-4000-8000-000000000000" for n in range(10)]
    relay.sessions = [dict(APP, id=i, session_id=f"s-{n}", title=f"会话{n}") for n, i in enumerate(ids)]
    relay.sessions[9]["id"] = "local_x\"><bad"  # not an id: drawn, but nothing to touch
    get(base + "/phone/desk")
    for _ in range(7):
        page = get(base + "/phone/desk/down")[2]
    first, capacity = phone_render.list_window(10, 7, phone_hub.DESK_FOOTER)
    shown = [relay.sessions[i]["id"] for i in range(first, min(10, first + capacity))]
    rows = [url.rsplit("/", 1)[1] for url, _ in touch_areas(page) if "/view/" in url]
    assert first > 0 and rows == [i for i in shown if i in ids]


def test_open_shows_the_session_and_makes_it_the_one_300_talks_to(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    get(base + "/phone/desk")
    get(base + "/phone/desk/down")
    page = get(base + "/phone/desk/open")[2]
    keys = softkey_urls(page)
    assert keys["Talk"] == "Dial:300" and list(keys) == ["Talk", "Up", "Down", "Back"]
    assert get(image_url(page))[2].startswith(b"\x89PNG")
    assert desk.target()["id"] == APP_ID
    assert post(base, "/local/desk/target", {})[1]["target"]["id"] == APP_ID


def test_only_session_ids_from_the_relay_make_it_into_urls(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    page = get(base + "/phone/desk/view/local_x%22%3E%3Cscript%3E")[2]
    assert b"no longer open" in page and relay.calls == []


def test_an_unreachable_studio_is_said_on_the_screen_and_to_voice_agent(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    relay.down = True
    assert b"Mac Studio" in get(base + "/phone/desk")[2]
    assert post(base, "/local/desk/target", {})[1]["error"].startswith("relay")


def test_voice_agent_routes_pass_through_and_the_target_is_in_call_until_the_end(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    assert post(base, "/local/desk/target", {})[1]["target"]["id"] == PHONE_ID
    assert desk.in_call("s-phone")  # from the start of the call, before any words are sent
    sent = post(base, "/local/desk/send", {"id": PHONE_ID, "text": "（电话）跑一下测试"})[1]
    assert sent["ok"] is True
    assert relay.calls[-1][2:4] == ("/send", {"id": PHONE_ID, "text": "（电话）跑一下测试"})
    post(base, "/local/desk/reply", {"id": PHONE_ID, "offset": 10, "sent": "（电话）跑一下测试"})
    assert relay.calls[-1][2:4] == ("/reply", {"id": PHONE_ID, "offset": 10, "sent": "（电话）跑一下测试"})
    assert post(base, "/local/desk/end", {"session_id": "s-phone"})[1] == {"ok": True}
    assert not desk.in_call("s-phone")


def test_a_target_found_too_late_for_the_call_is_not_held(desk_hub, monkeypatch):
    hub, desk, relay, base, _ = desk_hub
    monkeypatch.setattr(desk_gateway, "TARGET_DEADLINE", 0.1)
    slow = relay.__call__

    def late(url, key, path, payload, timeout=5):
        time.sleep(0.2)  # each read within its socket timeout, the whole answer too late
        return slow(url, key, path, payload, timeout)

    desk.post = late
    assert post(base, "/local/desk/target", {})[1] == {"target": None} and not desk.in_call("s-phone")


def test_a_desk_session_in_a_call_asks_the_phone_even_in_desk_mode(desk_hub):
    hub, desk, relay, base, push = desk_hub
    hub.set_mode("desk")
    desk.select(dict(APP, id=APP_ID))
    post(base, "/local/desk/target", {})  # the call talks to s-app; its first request may come at once
    result = {}
    t = threading.Thread(target=lambda: result.update(answer=post(base, "/hook/permission",
                                                                  permission(session_id="s-app"))[1]))
    t.start()
    for _ in range(100):
        reqs = post(base, "/local/pending", {"session_id": "s-app"})[1]["requests"]
        if reqs:
            break
        time.sleep(0.02)
    assert reqs and post(base, "/local/decide", {"id": reqs[0]["id"], "session_id": "s-app",
                                                "decision": "allow"})[1]["ok"] is True
    t.join(5)
    assert result["answer"]["decision"] == "allow"
    started = time.time()  # another desk session, not in a call: the computer decides
    assert post(base, "/hook/permission", permission(session_id="s-other"))[1]["decision"] is None
    assert time.time() - started < 1


def at(url):
    return int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["at"][0])


def test_a_long_conversation_opens_at_the_last_question_and_pages_both_ways(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    relay.turns = [{"role": "user", "text": "第一问"}, {"role": "assistant", "text": "很长的回答。" * 60},
                   {"role": "user", "text": "第二问"}, {"role": "assistant", "text": "第二个回答也很长。" * 40}]
    lines, last_question = phone_render.conversation_lines(relay.turns)
    capacity = phone_render.body_capacity("footer")
    assert lines[last_question].startswith("你：第二问") and len(lines) > last_question + capacity
    page = get(f"{base}/phone/desk/view/{APP_ID}")[2]
    keys, areas = softkey_urls(page), touch_areas(page)
    step = capacity - 1  # a page keeps one line of the one before
    assert at(keys["Up"]) == last_question - step and at(keys["Down"]) == last_question + step
    assert [url for url, _ in areas[:2]] == [keys["Up"], keys["Down"]]  # upper half back, lower half on
    (_, (_, top, _, middle)), (_, (_, below, _, bottom)) = areas[:2]
    assert top == phone_render.BODY_TOP - 1 and middle < below and bottom < phone_render.HEIGHT
    url = keys["Down"]
    for _ in range(30):  # paging on stops at the last page
        url = softkey_urls(get(url)[2])["Down"]
    assert at(url) == len(lines) - capacity
    url = keys["Up"]
    for _ in range(30):
        url = softkey_urls(get(url)[2])["Up"]
    assert at(url) == 0
    assert get(image_url(get(url)[2]))[2].startswith(b"\x89PNG")
    assert get(f"{base}/phone/desk/view/{APP_ID}?at=99999")[0] == 200  # out of range is clamped



def test_page_keys_move_the_list_a_screen_at_a_time(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    relay.sessions = [dict(APP, id=f"local_{n:08d}-0000-4000-8000-000000000000", title=f"会话{n}") for n in range(10)]
    rows = phone_render.body_capacity(phone_hub.DESK_FOOTER)
    get(base + "/phone/desk/pagedown")
    assert desk.cursor == rows
    get(base + "/phone/desk/pagedown")
    assert desk.cursor == 9  # the last one
    get(base + "/phone/desk/pageup")
    assert desk.cursor == 9 - rows
    get(base + "/phone/desk/pageup")
    assert desk.cursor == 0


def test_the_list_is_wide_and_its_arrows_page_too(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    page = get(base + "/phone/desk")[2]
    doc = xml.dom.minidom.parseString(page)
    assert doc.documentElement.getAttribute("WindowMode") == "Wide"  # covers the line keys' globes
    areas = dict(touch_areas(page))
    arrows = {url.rsplit("/", 1)[1]: box for url, box in areas.items() if url.endswith(("pageup", "pagedown"))}
    assert set(arrows) == {"pageup", "pagedown"}
    for box in arrows.values():
        assert box[0] >= phone_render.WIDTH and box[2] < phone_render.WIDE_WIDTH  # the strip right of the rows
    assert arrows["pageup"][3] < arrows["pagedown"][1]



def test_on_a_session_page_the_page_keys_page_its_conversation(desk_hub):
    hub, desk, relay, base, _ = desk_hub
    relay.turns = [{"role": "user", "text": "第一问"}, {"role": "assistant", "text": "很长的回答。" * 60}]
    first = get(f"{base}/phone/desk/view/{APP_ID}")[2]
    keys = softkey_urls(first)
    assert xml.dom.minidom.parseString(first).documentElement.getAttribute("WindowMode") == "Wide"
    png = Image.open(io.BytesIO(get(image_url(first))[2]))
    assert png.size == (phone_render.WIDE_WIDTH, phone_render.HEIGHT)  # it draws the arrows beside the keys
    arrows = [url for url, box in touch_areas(first) if box[0] >= phone_render.WIDTH]
    assert arrows == [keys["Up"], keys["Down"]]  # and touching an arrow pages too
    page = get(base + "/phone/desk/pagedown")[2]  # line key 3
    assert at(image_url(page)) == at(keys["Down"]) > at(image_url(first))
    page = get(base + "/phone/desk/pageup")[2]  # line key 2
    assert at(image_url(page)) == at(image_url(first)) and desk.cursor == 0
    get(base + "/phone/desk")  # back on the list, they page the list again
    get(base + "/phone/desk/pagedown")
    assert desk.cursor == 1

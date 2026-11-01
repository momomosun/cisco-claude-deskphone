"""phone-hub's front door pages: someone at the door comes up on the phone, with a stand-in HA."""
import io
import ipaddress
import os
import tempfile
import threading
import time
import xml.dom.minidom

import pytest
from PIL import Image

import ha_door
import phone_hub
import phone_render
from test_ha_door import jpeg
from test_phone_hub import FakePush, expired, get, picture, post, softkey_urls, wait_for_push, HOOK_KEY


class StubHA:
    def __init__(self):
        self.states = {"binary_sensor.front_door_motion": "off", "binary_sensor.front_door_motion_2": "off"}
        self.pictures = {"camera.front_door": jpeg((200, 0, 0)), "camera.front_door_2": jpeg((0, 0, 200))}
        self.fetched, self.down = [], False

    def state(self, entity_id):
        if self.down:
            raise OSError("down")
        return self.states.get(entity_id)

    def picture(self, camera):
        if self.down:
            raise OSError("down")
        self.fetched.append(camera)
        return self.pictures[camera]

    def update(self, entity_id):
        pass

    def entity(self, entity_id):
        return {"state": "idle", "attributes": {"last_record": "2026-10-27T13:18:23+00:00"}}


@pytest.fixture
def door_hub(monkeypatch):
    monkeypatch.setattr(phone_hub, "PHONE_IP", "127.0.0.1")
    monkeypatch.setattr(phone_hub, "HOOK_NET", ipaddress.ip_network("127.0.0.0/8"))
    monkeypatch.setattr(phone_hub, "RING_DELAY", 0.01)
    ha, push, chimes = StubHA(), FakePush(), []
    hub = phone_hub.Hub(push, wait_seconds=2, state_path=os.path.join(tempfile.mkdtemp(), "s.json"),
                        chime=lambda: chimes.append(time.time()) or True)
    hub.door = ha_door.DoorWatch(ha, alert=hub.door_alert)
    server = phone_hub.HubServer(("127.0.0.1", 0), phone_hub.make_handler(hub, HOOK_KEY, phone_hub.PushAuth()))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(phone_hub, "BASE_URL", base)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub, ha, push, chimes, base
    server.shutdown()


def wide_picture_page(page):
    root = xml.dom.minidom.parseString(page).documentElement
    return root.tagName == "CiscoIPPhoneImageFile" and root.getAttribute("WindowMode") == "Wide"


def test_someone_at_the_door_comes_up_on_the_phone_with_one_chime(door_hub):
    hub, ha, push, chimes, base = door_hub
    hub.door.check()  # all quiet
    ha.states["binary_sensor.front_door_motion"] = "on"
    hub.door.check()
    url = wait_for_push(push)
    page = get(url)[2]
    assert wide_picture_page(page) and expired(url)  # out of the history, like a request
    png = get(picture(page))[2]
    assert Image.open(io.BytesIO(png)).size == (phone_render.WIDE_WIDTH, phone_render.HEIGHT)
    time.sleep(0.2)
    assert len(chimes) == 1  # once the picture is on the screen
    get(picture(page))
    time.sleep(0.2)
    assert len(chimes) == 1  # once per visit
    assert ha.fetched == ["camera.front_door"]  # the doorbell, whose motion it was


def test_the_door_can_be_looked_at_from_the_menu_quietly(door_hub):
    hub, ha, push, chimes, base = door_hub
    menu = xml.dom.minidom.parseString(get(base + "/phone/menu")[2])
    items = {i.getElementsByTagName("Name")[0].firstChild.data: i.getElementsByTagName("URL")[0].firstChild.data
             for i in menu.getElementsByTagName("MenuItem")}
    page = get(items["Front door"])[2]
    assert wide_picture_page(page) and ha.fetched == ["camera.front_door"]
    get(picture(page))
    other = get(softkey_urls(page)["Other"])[2]  # the front door camera
    assert ha.fetched[-1] == "camera.front_door_2"
    get(softkey_urls(other)["Update"])  # look again at the same camera
    assert ha.fetched[-1] == "camera.front_door_2"
    time.sleep(0.2)
    assert chimes == [] and push.urls == []


def test_home_assistant_out_of_reach_is_said_on_the_screen(door_hub):
    hub, ha, push, chimes, base = door_hub
    ha.down = True
    assert b"not reachable" in get(base + "/phone/door?cam=0")[2]


def test_the_door_alert_can_be_tried_from_the_mini(door_hub):
    hub, ha, push, chimes, base = door_hub
    status, answer = post(base, "/local/door", {})
    assert status == 200 and answer["ok"] is True
    url = wait_for_push(push)
    get(picture(get(url)[2]))
    time.sleep(0.2)
    assert len(chimes) == 1 and ha.fetched == ["camera.front_door"]


def test_the_caption_says_when_the_picture_was_taken(door_hub, monkeypatch):
    hub, ha, push, chimes, base = door_hub
    monkeypatch.setenv("TZ", "Australia/Sydney")  # any fixed zone
    time.tzset()
    page = get(base + "/phone/door?cam=0")[2]
    get(picture(page))
    picture_, index, taken = hub.door.latest
    assert taken == 1793107103
    assert phone_hub.door_caption("门铃", taken, now=taken + 60) == "门铃 · 00:18"  # Sydney, today
    assert phone_hub.door_caption("门铃", taken, now=taken + 86400) == "门铃 · 10-28 00:18"  # an older one

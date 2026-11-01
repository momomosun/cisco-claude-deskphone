"""ha_door: Home Assistant's front door cameras watched for the desk phone, against a stand-in HA."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

import ha_door

TOKEN = "ha-token"


def jpeg(color=(200, 60, 40), size=(640, 360)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def fake_ha():
    """Answers /api/states/<id> from states and /api/camera_proxy/<id> from pictures, only with
    the right bearer token; records what was asked."""
    states, pictures, asked = {}, {}, []
    clips, media = {}, {}  # camera -> clip it would save; saved file name -> clip

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            asked.append((self.path, body, self.headers.get("Authorization")))
            if self.path == "/api/services/blink/save_video" and body.get("entity_id") in clips:
                media[os.path.basename(body["filename"])] = clips[body["entity_id"]]
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

        def do_GET(self):
            asked.append(self.path)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_response(401)
                self.end_headers()
                return
            kind, _, entity = self.path.partition("/api/")[2].partition("/")
            if self.path.startswith("/media/local/") and self.path[len("/media/local/"):] in media:
                body, ctype = media[self.path[len("/media/local/"):]], "video/mp4"
            elif kind == "states" and entity in states:
                value = states[entity]  # a state, or {"state": ..., "attributes": {...}}
                value = value if isinstance(value, dict) else {"state": value}
                body, ctype = json.dumps({"entity_id": entity, **value}).encode(), "application/json"
            elif kind == "camera_proxy" and entity in pictures:
                body, ctype = pictures[entity], "image/jpeg"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token_file = os.path.join(tempfile.mkdtemp(), "ha.token")
    with open(token_file, "w") as f:
        f.write(TOKEN + "\n")
    ha = ha_door.HomeAssistant(f"http://127.0.0.1:{server.server_address[1]}", token_file)
    ha.clips = clips
    yield ha, states, pictures, asked, token_file
    server.shutdown()


def test_home_assistant_is_asked_with_the_token(fake_ha):
    ha, states, pictures, asked, _ = fake_ha
    states["binary_sensor.front_door_motion"] = "off"
    pictures["camera.front_door"] = jpeg()
    assert ha.state("binary_sensor.front_door_motion") == "off"
    assert ha.picture("camera.front_door") == pictures["camera.front_door"]


def test_without_a_token_home_assistant_is_not_asked(fake_ha):
    ha, states, _, asked, token_file = fake_ha
    os.remove(token_file)
    with pytest.raises(LookupError):
        ha.state("binary_sensor.front_door_motion")
    assert asked == []


def watch(ha, alerts, cooldown=60):
    return ha_door.DoorWatch(ha, alert=lambda: alerts.append(time.time()), cooldown=cooldown)


def test_someone_coming_shows_that_cameras_picture(fake_ha):
    ha, states, pictures, _, _ = fake_ha
    states.update({"binary_sensor.front_door_motion": "on", "binary_sensor.front_door_motion_2": "off"})
    pictures.update({"camera.front_door": jpeg((200, 0, 0)), "camera.front_door_2": jpeg((0, 0, 200))})
    alerts = []
    door = watch(ha, alerts)
    assert door.check() is None and alerts == []  # motion already on when it starts: an old visit
    states["binary_sensor.front_door_motion"] = "off"
    door.check()
    states["binary_sensor.front_door_motion_2"] = "on"  # the front door camera sees someone
    assert door.check() == 1 and len(alerts) == 1
    picture, index, when = door.latest
    assert picture == pictures["camera.front_door_2"] and index == 1 and time.time() - when < 5


def test_one_visit_alerts_once(fake_ha):
    ha, states, pictures, _, _ = fake_ha
    states.update({"binary_sensor.front_door_motion": "off", "binary_sensor.front_door_motion_2": "off"})
    pictures.update({"camera.front_door": jpeg(), "camera.front_door_2": jpeg()})
    alerts = []
    door = watch(ha, alerts)
    door.check()
    states["binary_sensor.front_door_motion"] = "on"
    door.check()
    states["binary_sensor.front_door_motion_2"] = "on"  # the other camera, same visit
    door.check()
    states["binary_sensor.front_door_motion"] = "off"
    door.check()
    states["binary_sensor.front_door_motion"] = "on"
    door.check()
    assert len(alerts) == 1
    door = watch(ha, alerts, cooldown=0)  # once the cooldown is over, a new visit alerts again
    door.check()
    states["binary_sensor.front_door_motion"] = "off"
    door.check()
    states["binary_sensor.front_door_motion"] = "on"
    door.check()
    assert len(alerts) == 2


def test_looking_now_fetches_a_fresh_picture(fake_ha):
    ha, states, pictures, _, _ = fake_ha
    pictures["camera.front_door"] = jpeg((10, 200, 10))
    door = watch(ha, [])
    picture, index, _ = door.show(0)
    assert picture == pictures["camera.front_door"] and index == 0
    assert door.label(0) == "门铃" and door.label(1) == "门口"


def test_blink_is_asked_to_refresh_every_minute(fake_ha):
    # HA's Blink integration polls Blink's cloud only every 300 s, whatever its scan_interval option
    ha, states, pictures, asked, _ = fake_ha
    states.update({"binary_sensor.front_door_motion": "off", "binary_sensor.front_door_motion_2": "off"})
    door = ha_door.DoorWatch(ha, alert=lambda: None, refresh=60)
    door.check()
    door.check()
    for _ in range(50):  # the refresh runs in the background: it takes HA seconds
        posts = [a for a in asked if isinstance(a, tuple)]
        if posts:
            break
        time.sleep(0.05)
    assert posts == [("/api/services/homeassistant/update_entity",
                      {"entity_id": "binary_sensor.front_door_motion"}, f"Bearer {TOKEN}")]  # once a minute


def test_no_token_no_noise(fake_ha):
    ha, states, _, asked, token_file = fake_ha
    os.remove(token_file)
    logged = []
    door = ha_door.DoorWatch(ha, alert=lambda: None, refresh=0, log=logged.append)
    with pytest.raises(LookupError):
        door.check()
    time.sleep(0.3)
    assert logged == [] and asked == []


def test_the_picture_carries_the_time_it_was_taken(fake_ha):
    ha, states, pictures, _, _ = fake_ha
    pictures["camera.front_door"] = jpeg()
    states["camera.front_door"] = {"state": "idle", "attributes": {"last_record": "2026-10-27T13:18:23+00:00"}}
    door = watch(ha, [])
    assert door.show(0)[2] == 1793107103  # Blink's last clip (2026-10-27 13:18:23 UTC), not when it was fetched
    states["camera.front_door"] = {"state": "idle", "attributes": {}}
    assert time.time() - door.show(0)[2] < 5  # nothing known: now


needs_ffmpeg = pytest.mark.skipif(not ha_door.ffmpeg(), reason="no ffmpeg")


def mp4(color):
    """A 3-second clip of one colour, like a Blink clip saved from the Sync Module's USB stick."""
    path = os.path.join(tempfile.mkdtemp(), "clip.mp4")
    subprocess.run([ha_door.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=320x320:d=3",
                    "-pix_fmt", "yuv420p", path], check=True)
    with open(path, "rb") as f:
        return f.read()


@needs_ffmpeg
def test_the_picture_is_a_frame_of_the_last_clip_on_the_usb_stick(fake_ha):
    # Blink keeps clips on the Sync Module's USB stick; the camera's cloud thumbnail is days old
    ha, states, pictures, asked, _ = fake_ha
    pictures["camera.front_door"] = jpeg((200, 0, 0))  # the stale cloud thumbnail: red
    ha.clips["camera.front_door"] = mp4("lime")
    picture, index, _ = watch(ha, []).show(0)
    assert Image.open(io.BytesIO(picture)).convert("RGB").getpixel((160, 160))[1] > 200  # green: the clip
    saves = [a for a in asked if isinstance(a, tuple) and a[0] == "/api/services/blink/save_video"]
    assert saves and saves[0][1]["entity_id"] == "camera.front_door"
    assert saves[0][1]["filename"].startswith("/media/")  # HA's media folder: only with a login, not /local


def test_without_the_clip_the_cloud_thumbnail_is_shown(fake_ha):
    ha, states, pictures, _, _ = fake_ha
    pictures["camera.front_door"] = jpeg((200, 0, 0))  # no clip saved: Blink could not fetch it
    picture, _, _ = watch(ha, []).show(0)
    assert picture == pictures["camera.front_door"]

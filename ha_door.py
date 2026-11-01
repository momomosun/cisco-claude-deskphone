"""Someone at the front door: the desk phone shows them.

Home Assistant on the Air (Blink integration) has the doorbell's and the front door camera's
motion sensors and pictures. HA polls Blink's cloud every scan_interval, and a motion clip's
thumbnail becomes the camera's picture: the moment someone came. This watches the motion sensors
through HA's REST API, with a long-lived access token made in HA for this hub (ha.token, not in
git; without it nothing is asked), and on new motion keeps that camera's picture for the phone and
calls alert(). Blink keeps the clips on the Sync Module's USB stick, and a camera's cloud
thumbnail is only renewed when asked for, so it can be days old: the picture is a frame of the
camera's last clip instead, which HA's blink.save_video writes to its media folder (served only
with a login, unlike /local) and ffmpeg cuts a frame from; the thumbnail is the fallback. The integration (a copy of HA's own, under custom_components/blink) polls Blink's
cloud every 300 s whatever its scan_interval option says, so this also asks HA to refresh it every
REFRESH seconds: someone at the door shows up within about a minute.
"""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HA_URL = os.environ.get("HA_URL", "http://192.168.1.40:8123")
TOKEN_FILE = os.path.join(HERE, "ha.token")
# (motion sensor, camera, what the phone calls it): the Blink doorbell, then the front door camera
CAMERAS = (("binary_sensor.front_door_motion", "camera.front_door", "门铃"),
           ("binary_sensor.front_door_motion_2", "camera.front_door_2", "门口"))
POLL = 3
REFRESH = 60  # seconds between refreshes of the Blink integration, each a request to Blink's cloud
COOLDOWN = 60  # one alert a visit: both cameras see it, and Blink reports it again a scan later
TIMEOUT = 10
MAX_STATE = 1 << 16
MAX_PICTURE = 4 << 20
MAX_CLIP = 32 << 20
CLIP_FRAME = 2  # seconds into a clip: Blink starts recording as someone comes into view
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")


def ffmpeg():
    """The ffmpeg to cut frames with, or None."""
    return FFMPEG if os.access(FFMPEG, os.X_OK) else shutil.which("ffmpeg")


def clip_frame(clip, at=CLIP_FRAME):
    """A JPEG of the frame at seconds into an MP4 clip (the first frame if it is shorter)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "clip.mp4")
        with open(path, "wb") as f:
            f.write(clip)
        for start in (at, 0):
            frame = subprocess.run([ffmpeg() or "ffmpeg", "-v", "error", "-ss", str(start), "-i", path,
                                    "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"],
                                   capture_output=True, timeout=30).stdout
            if frame:
                return frame
    raise ValueError("no frame in the clip")


class HomeAssistant:
    """HA's REST API, with the token in token_file (read each time: it can be added while running)."""

    def __init__(self, url=HA_URL, token_file=TOKEN_FILE):
        self.url, self.token_file = url.rstrip("/"), token_file

    def _request(self, path, limit, body=None):
        try:
            with open(self.token_file) as f:
                token = f.read().strip()
        except OSError:
            token = ""
        if not token:
            raise LookupError("no Home Assistant token")
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(limit)

    def _get(self, path, limit):
        return self._request(path, limit)

    def entity(self, entity_id):
        """entity_id's state object: {"state": ..., "attributes": {...}, ...}."""
        return json.loads(self._get("/api/states/" + urllib.parse.quote(entity_id), MAX_STATE))

    def state(self, entity_id):
        return self.entity(entity_id).get("state")

    def picture(self, camera):
        return self._get("/api/camera_proxy/" + urllib.parse.quote(camera), MAX_PICTURE)

    def clip(self, camera):
        """camera's last clip, from the Sync Module's USB stick through Blink's cloud: HA saves it in
        its media folder (blink.save_video) and serves it from there."""
        name = camera.replace(".", "_") + ".mp4"
        self._request("/api/services/blink/save_video", MAX_STATE,
                      {"entity_id": camera, "filename": "/media/" + name})
        return self._get("/media/local/" + name, MAX_CLIP)

    def update(self, entity_id):
        """Have HA refresh entity_id's integration now (homeassistant.update_entity)."""
        self._request("/api/services/homeassistant/update_entity", MAX_STATE, {"entity_id": entity_id})


class DoorWatch:
    """Polls the motion sensors; when one goes from off to on outside the cooldown, keeps its
    camera's picture as latest and calls alert()."""

    def __init__(self, ha, alert, cameras=CAMERAS, poll=POLL, cooldown=COOLDOWN, refresh=REFRESH, log=print):
        self.ha, self.alert, self.cameras = ha, alert, cameras
        self.poll, self.cooldown, self.refresh, self.log = poll, cooldown, refresh, log
        self.refreshed = float("-inf")
        self.lock = threading.Lock()
        self.seen = {}
        self.alerted = float("-inf")
        self.latest = None  # (picture, camera index, when) for the phone

    def label(self, index):
        return self.cameras[index % len(self.cameras)][2]

    def show(self, index):
        """Fetch camera index's picture now and keep it as latest, with when it was taken: the
        camera's last clip (Blink's last_record), or now if HA does not say. The picture is a frame
        of that clip, or the camera's thumbnail when the clip cannot be had."""
        index %= len(self.cameras)
        camera = self.cameras[index][1]
        try:
            picture = clip_frame(self.ha.clip(camera))
        except LookupError:
            raise
        except Exception as exc:
            self.log(f"door: no clip from {camera} ({exc}): its thumbnail instead")
            picture = self.ha.picture(camera)
        try:
            stamp = self.ha.entity(camera)["attributes"]["last_record"]
            taken = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            taken = time.time()
        with self.lock:
            self.latest = (picture, index, taken)
            return self.latest

    def refresh_blink(self):
        """Ask HA to refresh Blink, in the background: it waits for Blink's cloud, seconds at times."""
        def ask():
            try:
                self.ha.update(self.cameras[0][0])
            except LookupError:  # no token yet
                pass
            except Exception as exc:
                self.log(f"door watch: Blink refresh: {exc}")

        threading.Thread(target=ask, daemon=True).start()

    def check(self):
        """One poll: the index of the camera it alerted for, or None."""
        if time.time() - self.refreshed >= self.refresh:
            self.refreshed = time.time()
            self.refresh_blink()
        for index, (motion, _, _) in enumerate(self.cameras):
            state = self.ha.state(motion)
            before, self.seen[motion] = self.seen.get(motion), state
            if before == "off" and state == "on" and time.time() - self.alerted >= self.cooldown:
                self.alerted = time.time()
                self.log(f"door: someone at the {self.cameras[index][1]}")
                self.show(index)
                self.alert()
                return index
        return None

    def run(self):
        failing = False
        while True:
            try:
                self.check()
                failing = False
            except LookupError:  # no token yet
                pass
            except Exception as exc:  # HA restarting, the Air asleep: say so once
                if not failing:
                    self.log(f"door watch: {exc}")
                failing = True
            time.sleep(self.poll)

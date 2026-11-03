"""The desk phone's calls out through the iPhone (Mac Mini, Continuity "Calls from iPhone").

baresip registers as extension 103 with its audio on virtual devices (the iPhone call's sound comes in
on CallSpk 2ch Tap, what is said on the desk phone goes out on CallMic 2ch Feed) and answers whatever
FreeSWITCH sends it. FreeSWITCH's cell_out sends a number dialled on 101 there with the caller name
"cell-<number>" (no colon: an unquoted display name cannot hold one). This watches baresip's
control socket (ctrl_tcp, netstrings of JSON) and has macOS place that number through the iPhone
(open tel:...), then presses the 呼叫 button of the banner macOS asks with (cell_confirm.js). Hanging up
either side ends the other: the desk phone's CALL_CLOSED presses the call's end button on screen,
callservicesd's "All calls ended" hangs baresip up. Anything else reaching 103 is hung up.
Not done, for want of any way in (11-03): keys pressed on the desk phone (the call's keypad and end
button can only be clicked on screen, and its bar drops out of the accessibility tree soon after the
call connects; TelephonyUtilities refuses programs without Apple's access-calls entitlement), and
ending the iPhone's side: pressing the bar's end button only takes the Mac off the call.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time

CTRL = ("127.0.0.1", 4444)
PBX_HOST = "192.168.1.10"
NUMBER = re.compile(r"(0\d{9}|0011\d{6,15}|13\d{4}|1[38]00\d{6}|\+\d{8,15})")  # as cell_out; fullmatch
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "cell_bridge.log")
MAX_NETSTRING = 1 << 20
# The Mini's default output is CallSpk 2ch and its default input CallMic 2ch (cell_bridge.sh), which the
# iPhone call uses: its sound is heard on CallSpk's mirror Tap, its microphone fed through CallMic's Feed.
SOURCE = "coreaudio,CallSpk 2ch Tap"   # the iPhone call's sound, towards the desk phone
PLAYER = "coreaudio,CallMic 2ch Feed"  # the desk phone's sound, the iPhone call's microphone
REOPEN_AFTER = 4  # seconds after the banner is pressed: the call has opened its audio by then
BANNER_WAIT = 45  # seconds the call banner is looked for (FaceTime may be slow to start)
SCREEN_TIMEOUT = 30  # one cell_confirm.js run; a dump of Notification Center is the slow one


def log(text):
    fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # numbers and screen contents
    with os.fdopen(fd, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def masked(number):
    return number[:4] + "*" * (len(number) - 4)


def netstrings(sock):
    """Decoded JSON messages from baresip, as they arrive; ValueError on a malformed one."""
    buf = b""
    while True:
        while b":" in buf:
            head, rest = buf.split(b":", 1)
            if not head.isdigit() or len(head) > 7 or int(head) > MAX_NETSTRING:
                raise ValueError("bad netstring length")
            size = int(head)
            if len(rest) < size + 1:
                break
            if rest[size:size + 1] != b",":
                raise ValueError("netstring without its comma")
            yield json.loads(rest[:size])
            buf = rest[size + 1:]
        chunk = sock.recv(4096)
        if not chunk:
            return
        buf += chunk


def send(sock, command, params=""):
    data = json.dumps({"command": command, "params": params, "token": command}).encode()
    sock.sendall(str(len(data)).encode() + b":" + data + b",")


def peer_host(event):
    return str(event.get("peeruri") or "").split("@", 1)[-1].split(":", 1)[0].split(";", 1)[0]


def number_of(event):
    """The number to dial, when the call comes from our PBX's cell_out; otherwise None."""
    name = str(event.get("peerdisplayname") or "")
    if peer_host(event) != PBX_HOST or not name.startswith("cell-"):
        return None
    number = name[len("cell-"):]
    return number if NUMBER.fullmatch(number) else None


def dial(number):
    # The display goes off (and the screen locks with it) even under caffeinate -d (11-02); waking it
    # first brings the desktop back, no password being set, so the banner can be pressed.
    try:
        subprocess.run(["/usr/bin/caffeinate", "-u", "-t", "2"], check=False, timeout=10)
        subprocess.run(["/usr/bin/open", f"tel://{number}"], check=False, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"dial: {exc}")


def screen(command):
    """cell_confirm.js: call (press the banner's 呼叫 if it shows), cancel (close waiting banners),
    hangup (press the call's end button), dump. TIMEOUT or ERROR when the script itself failed."""
    try:
        out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", os.path.join(HERE, "cell_confirm.js"),
                              command], capture_output=True, text=True, timeout=SCREEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"ERROR {exc}"
    return (out.stdout or out.stderr).strip()


def spawn(work):
    def run():
        try:
            work()
        except Exception as exc:  # a thread's failure would otherwise vanish: the log is all there is
            log(f"{getattr(work, '__name__', 'work')} failed: {exc!r}")
    threading.Thread(target=run, daemon=True).start()


class Bridge:
    """One call at a time: the desk phone's, from cell_out to the iPhone and back. Each call gets a
    number (self.call); work started for a call checks it is still that call before it acts."""

    def __init__(self, dial=dial, screen=screen, spawn=spawn, sleep=time.sleep, clock=time.monotonic):
        self.dial, self.screen, self.spawn, self.sleep, self.clock = dial, screen, spawn, sleep, clock
        self.lock = threading.Lock()
        self.sending = threading.Lock()  # one netstring at a time on the control socket
        self.sock = None
        self.call = 0           # which call this is
        self.active = False     # an iPhone call placed for the desk phone, not yet over
        self.connected = False  # its banner was pressed: an "All calls ended" from now on is this call's

    def send(self, command, params=""):
        with self.lock:
            sock = self.sock
        if not sock:
            return
        try:
            with self.sending:
                send(sock, command, params)
        except OSError as exc:
            log(f"baresip control: {command}: {exc}")

    def current(self, call):
        with self.lock:
            return self.active and self.call == call

    def on_event(self, event):
        if not isinstance(event, dict) or not event.get("event"):
            return
        if event.get("type") == "CALL_INCOMING":
            number = number_of(event)
            if not number:
                log(f"refused a call to 103 from {peer_host(event)!r}")  # its name may hold a number
                self.send("hangup")
                return
            log(f"desk phone dials {masked(number)}: out through the iPhone")
            with self.lock:
                self.call += 1
                call, self.active, self.connected = self.call, True, False
            self.spawn(lambda: self.call_on_screen(call, number))
        elif event.get("type") == "CALL_CLOSED":
            with self.lock:
                was, connected, self.active, self.connected = self.active, self.connected, False, False
            if was and connected:  # the desk phone hung up: end the iPhone's call too
                self.spawn(self.end_on_screen)
            # hung up before the banner was pressed: call_on_screen stops looking and closes it

    def call_on_screen(self, call, number):
        """Dial, then press the banner's 呼叫 once it shows, while the desk phone is still on."""
        self.screen("cancel")  # a banner left from before must not be the one pressed
        self.dial(number)
        deadline = self.clock() + BANNER_WAIT
        answer = "NO_BANNER"
        while self.current(call) and self.clock() < deadline:
            answer = self.screen("call")
            if answer == "CALLED":
                break
            self.sleep(0.5)
        if answer == "CALLED":
            with self.lock:
                still = self.active and self.call == call
                self.connected = still
            if not still:  # the desk phone hung up as the banner was pressed
                log("call banner: CALLED after the desk phone hung up")
                self.end_on_screen()
                return
        log(f"call banner: {answer}")
        if answer != "CALLED":
            if not self.current(call):
                log(f"desk phone hung up first: banner {self.screen('cancel')}")
                return
            log("what the screen shows:\n" + self.screen("dump"))
        # baresip opened its devices when it answered; the iPhone call then opens the same devices in its
        # own format, which leaves baresip's streams silent (11-02): open them again once it has
        self.sleep(REOPEN_AFTER)
        if self.current(call):
            self.send("ausrc", SOURCE)
            self.send("auplay", PLAYER)

    def end_on_screen(self):
        answer = self.screen("hangup")
        log(f"desk phone hung up, iPhone call: {answer}")
        if answer != "ENDED":
            log("what the screen shows:\n" + self.screen("dump"))

    def on_call_log(self, line):
        """A line from callservicesd's log: the iPhone's call over means the desk phone's is too."""
        if "All calls ended" not in line:
            return
        with self.lock:  # before this call connected it is the last call's ending, logged late (11-03)
            if not self.connected:
                return
            was, self.active, self.connected = self.active, False, False
        if was:
            log("iPhone call ended: hanging up the desk phone")
            self.send("hangup")


def watch_calls(bridge):
    """Feed callservicesd's log to the bridge, restarting the stream if it stops."""
    while True:
        proc = subprocess.Popen(["/usr/bin/log", "stream", "--style", "compact", "--predicate",
                                 'process == "callservicesd"'], stdout=subprocess.PIPE, text=True)
        for line in proc.stdout:
            bridge.on_call_log(line)
        proc.wait()
        time.sleep(3)


def main():
    log(f"screen check: {screen('find')}")  # NOT_FOUND: can read the screen; empty or an error: no access
    bridge = Bridge()
    spawn(lambda: watch_calls(bridge))
    while True:
        try:
            with socket.create_connection(CTRL, timeout=10) as sock:
                sock.settimeout(None)
                with bridge.lock:
                    bridge.sock = sock
                log("connected to baresip")
                for event in netstrings(sock):
                    bridge.on_event(event)
        except (OSError, ValueError) as exc:
            log(f"baresip control: {exc}")
        with bridge.lock:
            bridge.sock = None
        time.sleep(3)


if __name__ == "__main__":
    main()

"""esl.Call against a scripted FreeSWITCH on the other end of a socket pair."""
import socket
import threading
import urllib.parse

import pytest

import esl

CHANNEL = {"Unique-ID": "abc-123", "Caller-Caller-ID-Number": "101", "Channel-Name": "sofia/internal/101@pbx"}


def message(headers, body=b""):
    head = "".join(f"{k}: {v}\n" for k, v in headers.items())
    if body:
        head = f"Content-Length: {len(body)}\n" + head
    return head.encode() + b"\n" + body


def event(variables=None, **fields):
    """An event-plain message; field names use - for _, channel variables keep their names."""
    fields = {**{k.replace("_", "-"): v for k, v in fields.items()},
              **{f"variable_{k}": v for k, v in (variables or {}).items()}}
    body = "".join(f"{k}: {urllib.parse.quote(str(v))}\n" for k, v in fields.items())
    return message({"Content-Type": "text/event-plain"}, (body + "\n").encode())


class FakeFS(threading.Thread):
    """Replies to connect/myevents, then answers each sendmsg with script(app, arg, tag) -> bytes."""

    def __init__(self, sock, script):
        super().__init__(daemon=True)
        self.sock, self.script = sock, script
        self.commands = []
        self.sendmsg_reply = "+OK"

    def run(self):
        reader = self.sock.makefile("rb")
        while True:
            lines = []
            while True:
                line = reader.readline()
                if not line:
                    return
                line = line.decode().rstrip("\n")
                if not line:
                    break
                lines.append(line)
            self.commands.append(lines)
            if lines[0] == "connect":
                fields = {k: urllib.parse.quote(v) for k, v in CHANNEL.items()}
                self.sock.sendall(message({"Content-Type": "command/reply", "Reply-Text": "+OK", **fields}))
            elif lines[0] == "sendmsg":
                h = dict(line.split(": ", 1) for line in lines[1:])
                self.sock.sendall(message({"Content-Type": "command/reply", "Reply-Text": self.sendmsg_reply}))
                out = self.script(h["execute-app-name"], h["execute-app-arg"], h["event-uuid"])
                if out is None:  # FreeSWITCH drops the socket
                    self.sock.shutdown(socket.SHUT_RDWR)
                    return
                self.sock.sendall(out)
            else:
                self.sock.sendall(message({"Content-Type": "command/reply", "Reply-Text": "+OK Events Enabled"}))


def completes(app, arg, tag, **variables):
    return (event(Event_Name="CHANNEL_EXECUTE", Application=app, Application_UUID=tag)
            + event(Event_Name="CHANNEL_EXECUTE_COMPLETE", Application="playback", Application_UUID="older")
            + event(variables, Event_Name="CHANNEL_EXECUTE_COMPLETE", Application=app, Application_UUID=tag))


def connect(script):
    ours, theirs = socket.socketpair()
    fs = FakeFS(theirs, script)
    fs.start()
    return esl.Call(ours), fs


def test_connect_reads_the_channel_and_subscribes_to_its_events():
    call, fs = connect(lambda app, arg, tag: completes(app, arg, tag))
    assert call.uuid == "abc-123"
    assert call.variables["Channel-Name"] == "sofia/internal/101@pbx"  # URL-decoded
    assert [c[0] for c in fs.commands] == ["connect", "myevents"]


def test_execute_waits_for_its_own_completion_and_returns_the_variables():
    call, fs = connect(lambda app, arg, tag: completes(app, arg, tag, digit="1", path="/tmp/a b.wav"))
    done = call.execute("play_and_get_digits", "1 1 1 5000 # /tmp/p.wav silence_stream://250 digit ^[12]$")
    assert done["Application-UUID"] == fs.commands[-1][-1].split(": ")[1]
    assert done["variable_digit"] == "1" and done["variable_path"] == "/tmp/a b.wav"
    assert fs.commands[-1][1:4] == ["call-command: execute", "execute-app-name: play_and_get_digits",
                                    "execute-app-arg: 1 1 1 5000 # /tmp/p.wav silence_stream://250 digit ^[12]$"]


def test_a_newline_cannot_smuggle_extra_headers_into_a_command():
    call, fs = connect(lambda app, arg, tag: completes(app, arg, tag))
    with pytest.raises(ValueError):
        call.execute("playback", "/tmp/x.wav\ncall-command: hangup")
    assert len(fs.commands) == 2


@pytest.mark.parametrize("ending", [
    event(Event_Name="CHANNEL_HANGUP", Hangup_Cause="NORMAL_CLEARING"),
    message({"Content-Type": "text/disconnect-notice"}, b"Disconnected, goodbye.\n"),
])
def test_hangup_during_an_application_raises_hangup(ending):
    call, _ = connect(lambda app, arg, tag: event(Event_Name="CHANNEL_EXECUTE", Application_UUID=tag) + ending)
    with pytest.raises(esl.Hangup):
        call.execute("record", "/tmp/u.wav 30 200 2")


def test_a_dropped_socket_raises_hangup():
    call, _ = connect(lambda app, arg, tag: None)
    with pytest.raises(esl.Hangup):
        call.execute("playback", "/tmp/x.wav")


def test_refused_command_raises():
    call, fs = connect(lambda app, arg, tag: b"")
    fs.sendmsg_reply = "-ERR no such channel"
    with pytest.raises(esl.ESLError):
        call.execute("playback", "/tmp/x.wav")

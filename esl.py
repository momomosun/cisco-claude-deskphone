"""One FreeSWITCH call, driven over the outbound event socket.

The dialplan's `socket 127.0.0.1:8085 async full` connects each call to voice_agent. Call wraps
that connection: execute() runs one dialplan application (answer, playback, record, ...) and
returns its CHANNEL_EXECUTE_COMPLETE event, matched by the event-uuid it was tagged with, so
completions of earlier applications are never mistaken for it. The caller hanging up, or
FreeSWITCH dropping the socket, raises Hangup from whatever is waiting.
"""
import collections
import urllib.parse
import uuid


class Hangup(Exception):
    """The caller hung up or FreeSWITCH closed the socket."""


class ESLError(Exception):
    """FreeSWITCH refused a command."""


HANGUP_EVENTS = ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE")


class Call:
    def __init__(self, sock):
        self.sock = sock
        self.reader = sock.makefile("rb")
        self.events = collections.deque()
        self.variables = self._command("connect")
        self.uuid = self.variables.get("Unique-ID", "")
        self._command("myevents")

    def execute(self, app, arg=""):
        """Run a dialplan application on the call and wait until it finishes. The returned
        event carries the channel variables as variable_<name>."""
        if "\n" in app or "\n" in arg:
            raise ValueError("newline in a dialplan application or its argument")
        tag = str(uuid.uuid4())
        self._command(f"sendmsg\ncall-command: execute\nexecute-app-name: {app}\n"
                      f"execute-app-arg: {arg}\nevent-uuid: {tag}")
        while True:
            event = self._next_event()
            if event.get("Event-Name") == "CHANNEL_EXECUTE_COMPLETE" and event.get("Application-UUID") == tag:
                return event

    def close(self):
        for closable in (self.reader, self.sock):
            try:
                closable.close()
            except OSError:
                pass

    # --- wire format ------------------------------------------------------------------
    def _command(self, text):
        self.sock.sendall(text.encode("utf-8") + b"\n\n")
        while True:
            headers, body = self._read_message()
            if headers.get("Content-Type") == "command/reply":
                reply = headers.get("Reply-Text", "")
                if reply.startswith("-ERR"):
                    raise ESLError(f"{text.splitlines()[0]}: {reply}")
                return headers
            self._take(headers, body)

    def _next_event(self):
        while not self.events:
            self._take(*self._read_message())
        return self.events.popleft()

    def _take(self, headers, body):
        kind = headers.get("Content-Type", "")
        if kind == "text/disconnect-notice":
            raise Hangup("disconnected")
        if kind == "text/event-plain":
            event = self._parse_headers(body.decode("utf-8", "replace").split("\n\n", 1)[0].split("\n"))
            if event.get("Event-Name") in HANGUP_EVENTS:
                raise Hangup(event.get("Hangup-Cause", "hangup"))
            self.events.append(event)

    def _read_message(self):
        lines = []
        while True:
            raw = self.reader.readline()
            if not raw:
                raise Hangup("socket closed")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                lines.append(line)
            elif lines:
                break
        headers = self._parse_headers(lines)
        length = int(headers.get("Content-Length") or 0)
        body = self.reader.read(length) if length else b""
        if len(body) < length:
            raise Hangup("socket closed")
        return headers, body

    @staticmethod
    def _parse_headers(lines):
        headers = {}
        for line in lines:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip()] = urllib.parse.unquote(value.strip())
        return headers

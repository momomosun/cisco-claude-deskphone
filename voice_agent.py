#!/usr/bin/env python3
"""voice-agent: dial 300 on the desk phone and talk to Claude Code on this Mac (the Mini).

FreeSWITCH hands every call to 300 to this process over its outbound event socket (esl.Call).
A call is a conversation with one Claude session:
  - beep, then record until the caller pauses for SILENCE_SECONDS or presses #;
  - transcribe it with Whisper (speech.Transcriber); silence is skipped;
  - give the text to `claude -p` in WORKSPACE (first turn --session-id, later --resume) while
    the caller hears a soft tick every two seconds;
  - read the answer back with `say` (# skips the rest). A short goodbye or hanging up ends it.
That Claude's hook marks its permission requests phone_call (claude_hooks/phone_call_settings.json),
so phone-hub holds them for the phone; this agent reads each one out and the caller presses
1 (allow) or 2 (deny). Only a short one-line Bash command, read out whole, can be allowed by
ear; anything else is allowed on the phone screen, whose softkeys work for every request (the
first answer counts). Only the desk phone (claude_caller in its directory entry) gets through,
one call at a time; a Watchdog restarts the agent if a call stops making progress.

A call talks to a session in the Claude desktop app on the Mac Studio when phone-hub names one
(the session last opened on the phone screen, else the one titled "电话"): the words go into
that session through desk_relay and its final answer is read back (DeskTurn). Only when no desk
session can take the call does the Mini's own `claude -p` answer, and the caller is told so.
"""
import itertools
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import desk_gateway
import esl
import hubauth
import render
import speech

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN = ("127.0.0.1", 8085)
HUB_URL = "http://127.0.0.1:8090"
CLAUDE = os.path.expanduser("~/.local/bin/claude")
WORKSPACE = os.path.expanduser("~/phone-claude")
SESSION_SETTINGS = os.path.join(HERE, "claude_hooks", "phone_call_settings.json")
TOKEN_FILE = os.path.join(HERE, "claude.token")  # from `claude setup-token`; optional
MODEL_DIR = os.path.join(HERE, "models", "whisper-large-v3-turbo")
VOICE = "Tingting"
SOCKET_TIMEOUT = 300    # FreeSWITCH silent this long on a call: give the call up
RECORD_LIMIT = 30       # seconds of speech per turn
SILENCE_LEVEL = 200     # FreeSWITCH record: mean |sample| (16-bit) below this is silence
SILENCE_SECONDS = 2     # ...and this much of it ends the turn
PATIENCE = 10           # silent recordings in a row before hanging up
TURN_TIMEOUT = 300      # seconds for one answer from Claude, permission waits included
LAND_TIMEOUT = 20       # seconds for words a desk session took to show up in its transcript
STALL_SECONDS = 240     # a call step this long means something hung (longest real one: ~100 s)
SPEAKABLE_COMMAND = 60  # longest command read out whole, so the keypad may allow it
KEYPAD_TIMEOUT_MS = 10000
MAX_SPOKEN = 400        # characters of an answer read aloud
BEEP = "tone_stream://%(150,0,880)"
TICK = "tone_stream://v=-18;%(80,1920,660)"  # two seconds: one soft tick
GREETING = "老板你好，很高兴为您服务，有事您吩咐。"  # mo's wording
BYE = "好的，再见。"
NOBODY = "好像没人说话，我先挂了。"
CLAUDE_FAILED = "Claude 那边出错了，详情记在日志里。"
TOO_SLOW = "Claude 太久没答完，我先停下了。"
KEYS = "按 1 允许，按 2 拒绝。"
SCREEN_ONLY = "完整内容在话机屏幕上，要批准请按屏幕上的允许；按 2 拒绝。"
NO_KEY = "没收到按键，也可以在话机屏幕上批准。"
ALREADY_ANSWERED = "这个请求已经处理过了。"
TRUNCATED = "……后面还有，就不念了。"
PHONE_PREFIX = "（电话语音）"  # marks the words a desk session gets as speech-recognized
DESK_FALLBACK = "电脑那边没接上，先用备用的 Claude 跟您说。"
DESK_BUSY = "这个会话正在忙，等它做完再说吧。"
DESK_NOT_SHOWN = "电脑上没能打开那个会话，可能锁屏了，或者窗口被切走了。"
DESK_LOCKED = "工作室那台电脑锁屏了，解锁以后才能往桌面会话里说话。"
DESK_UNCLEAR = "这个会话的名字不唯一，分不清是哪一个，先在电脑上给它改个名。"
DESK_UNCHECKED = "电脑上的会话清单这会儿读不全，没法确认是哪个会话，这次先不送。"
DESK_UNSURE = "不确定话送进去了没有，请在电脑上看一眼。"
DESK_NOT_LANDED = "话没送进那个会话，请在电脑上看一眼。"
DESK_DRAFT = "那个会话的输入框里有没发出去的字，我没动它。"
DESK_UNREACHABLE = "连不上电脑。"
DESK_TOO_SLOW = "它还在做，做完的结果可以在电脑上看。"
DESK_DONE = "做完了。"
GOODBYE = re.compile(r"(再见|拜拜|挂了|挂吧|没事了)[啊吧啦呀哈喽]*$")  # at the end: "电话挂了吗" asks
VOICE_PROMPT = (
    "你在通过座机电话和 mo 对话：他说的话由语音识别转成文字给你，可能有错字或同音字，按意思理解；"
    "你的回答会用语音念给他听。所以要口语化、简短，一般不超过三句话；不要用 Markdown、列表、"
    "表格、代码块或表情；网址、路径和长命令不要念出来，说清楚做了什么、结果是什么就行。"
    "查天气、新闻、资料这类信息用 WebSearch 或 WebFetch，不要用命令行去查。每条命令都要 mo 在电话上"
    f"听完再按键批准，所以确实要运行命令时，一次只运行一条单行短命令（{SPEAKABLE_COMMAND} 个字符以内，"
    "不要用 ; && | 拼接），description 用简短中文写，它会被念出来。")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# --- words --------------------------------------------------------------------------------
def speakable(text, limit=MAX_SPOKEN):
    """Claude's answer as something to listen to: no Markdown marks or links, not too long."""
    text = re.sub(r"```.*?```", "（代码略）", text, flags=re.S)
    text = re.sub(r"https?://\S+", "（链接）", text)
    text = re.sub(r"[*_`#>|~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = max(text.rfind(mark, 0, limit) for mark in "。！？；!?")
    return text[: cut + 1 if cut >= limit // 2 else limit] + TRUNCATED


def is_goodbye(text):
    bare = re.sub(r"[\s，。,.!！?？、～~]", "", text)
    return len(bare) <= 8 and bool(GOODBYE.search(bare))


def describe_request(req):
    """(words for a permission request, whether the keypad may allow it). Only a one-line Bash
    command short enough to be read out whole can be allowed by ear; anything else is approved
    on the phone screen, which shows it in full."""
    tool, command = str(req.get("tool_name") or "?"), req.get("command")
    if tool == "Bash" and isinstance(command, str) and command:
        if "\n" in command or len(command) > SPEAKABLE_COMMAND:
            return "Claude 想运行一条长命令。", False
        description = req.get("description")
        return f"Claude 想运行命令：{command}。" + (f"{description}。" if description else ""), True
    return f"Claude 想使用 {tool} 工具。", False


# --- Claude -------------------------------------------------------------------------------
def claude_argv(session, resume):
    return [CLAUDE, "-p", "--output-format", "json", "--settings", SESSION_SETTINGS,
            "--strict-mcp-config", "--append-system-prompt", VOICE_PROMPT,
            "--resume" if resume else "--session-id", session]


def claude_env(token_file=TOKEN_FILE, base=None):
    """The environment for claude: plus the long-lived token from `claude setup-token`, if saved."""
    env = dict(os.environ if base is None else base)
    try:
        with open(token_file) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if token.startswith("sk-ant-"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


class ClaudeTurn:
    """One `claude -p` answer, running while the call plays ticks and reads out permission requests.
    Claude gets its own process group, so stop() ends the commands it started as well; that also
    puts it out of reach of launchd's clean-up when this agent exits, hence kill_all()."""

    running = set()

    def __init__(self, argv, prompt, env=None, cwd=WORKSPACE, timeout=TURN_TIMEOUT):
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        ClaudeTurn.running.add(self)
        self.deadline = time.monotonic() + timeout
        self.result = None
        self.thread = threading.Thread(target=self._wait, args=(prompt,), daemon=True)
        self.thread.start()

    def _wait(self, prompt):
        try:
            out, err = self.proc.communicate(prompt.encode("utf-8"))
            self.result = (self.proc.returncode, out, err)
        except Exception as exc:  # recorded as a failed turn
            self.result = (None, b"", repr(exc).encode())
        finally:
            ClaudeTurn.running.discard(self)

    @classmethod
    def kill_all(cls):
        """Before this agent exits: end every Claude still running and what it started."""
        for turn in list(cls.running):
            try:
                os.killpg(turn.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def done(self):
        return not self.thread.is_alive()

    def expired(self):
        return time.monotonic() > self.deadline

    def stop(self):
        """End claude and everything it started (an approved Bash command would run on)."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            self.thread.join(2)
            if self.done():
                break
        self.thread.join(5)

    def answer(self):
        """Claude's reply, or None if it failed (the reason goes to the log)."""
        code, out, err = self.result or (None, b"", b"no result")
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("is_error") or not isinstance(data.get("result"), str):
            log(f"claude failed (exit {code}): {(out or err)[-300:]!r}")
            return None
        return data["result"]


# --- phone-hub ----------------------------------------------------------------------------
class HubClient:
    """phone-hub's loopback routes for this call's permission requests, signed like the hooks.
    Failures are logged and read as "nothing pending" / "not recorded": never end the call."""

    def __init__(self, url, key):
        self.url, self.key = url, key

    def _post(self, path, payload, timeout=5):
        return hubauth.post(self.url, self.key, path, payload, timeout=timeout)

    def pending(self, session):
        try:
            requests = self._post("/local/pending", {"session_id": session}).get("requests")
        except Exception as exc:
            log(f"hub pending: {exc}")
            return []
        return [r for r in requests if isinstance(r, dict)] if isinstance(requests, list) else []

    def decide(self, request_id, session, decision):
        try:
            answer = self._post("/local/decide", {"id": request_id, "session_id": session, "decision": decision})
        except Exception as exc:
            log(f"hub decide: {exc}")
            return False
        return answer.get("ok") is True

    def desk_target(self):
        """The desktop session this call talks to, or None (then the Mini's Claude answers)."""
        try:
            answer = self._post("/local/desk/target", {})
        except Exception as exc:
            log(f"hub desk target: {exc}")
            return None
        if answer.get("error"):
            log(f"desk: {answer['error']}")
        target = answer.get("target")
        return target if isinstance(target, dict) and target.get("id") else None

    def desk_send(self, host_id, text):
        try:  # the hub waits for the relay, which opens the session and finds its prompt box
            return self._post("/local/desk/send", {"id": host_id, "text": text}, timeout=75)
        except Exception as exc:
            return {"ok": False, "error": f"relay: {exc}"}

    def desk_reply(self, host_id, offset, sent):
        try:
            return self._post("/local/desk/reply", {"id": host_id, "offset": offset, "sent": sent}, timeout=15)
        except Exception as exc:
            return {"error": f"relay: {exc}"}

    def desk_end(self, session_id):
        try:
            self._post("/local/desk/end", {"session_id": session_id})
        except Exception as exc:
            log(f"hub desk end: {exc}")


def desk_failure(error):
    """What to say when a desk session did not take the words."""
    if error == "busy":
        return DESK_BUSY
    if error == "unclear title":
        return DESK_UNCLEAR
    if error == "titles unknown":
        return DESK_UNCHECKED
    if error.startswith("UNKNOWN"):  # the relay was cut off and the words did not show up (yet)
        return DESK_UNSURE
    if error.startswith("ABORT: the prompt box holds"):
        return DESK_DRAFT
    if error.startswith("ABORT: the screen is locked"):
        return DESK_LOCKED
    if error.startswith("ABORT"):
        return DESK_NOT_SHOWN
    return DESK_UNREACHABLE


class DeskTurn:
    """One answer from a session in the Claude desktop app, through phone-hub and desk_relay.
    Same face as ClaudeTurn. The send (opening the session, finding its prompt box) runs in the
    background so the caller hears ticks meanwhile. stop() leaves the session alone: its work is
    the user's own, so hanging up or giving up waiting does not interrupt it."""

    too_slow = DESK_TOO_SLOW

    def __init__(self, hub, target, text, timeout=TURN_TIMEOUT):
        self.hub, self.host_id, self.sent = hub, target["id"], PHONE_PREFIX + text
        self.session = str(target.get("session_id") or "")
        self.deadline = time.monotonic() + timeout
        self.offset, self.failure, self.texts, self.ended = None, None, [], False
        self.sent_at = None
        self.sending = threading.Thread(target=self._send, daemon=True)
        self.sending.start()

    def _send(self):
        result = self.hub.desk_send(self.host_id, self.sent)
        if result.get("ok"):
            self.offset = result.get("offset", 0)
            self.session = str(result.get("session_id") or self.session)
            self.sent_at = time.monotonic()
        else:
            self.failure = desk_failure(str(result.get("error") or ""))

    def done(self):
        self.sending.join(0.2)
        if self.sending.is_alive():
            return False
        if self.failure or self.ended:
            return True
        reply = self.hub.desk_reply(self.host_id, self.offset, self.sent)
        if reply.get("error"):
            self.failure = desk_failure(str(reply["error"]))
            return True
        if not reply.get("landed") and time.monotonic() - self.sent_at >= LAND_TIMEOUT:
            self.failure = DESK_NOT_LANDED  # Send was pressed, but the words never went in
            return True
        self.texts, self.ended = list(reply.get("texts") or []), bool(reply.get("ended"))
        return self.ended

    def expired(self):
        return time.monotonic() > self.deadline

    def stop(self):
        pass

    def answer(self):
        if self.failure or not self.ended:
            return None
        return self.texts[-1] if self.texts else DESK_DONE


# --- the call -----------------------------------------------------------------------------
class Watchdog:
    """Restarts the agent (launchd starts a fresh one) when a call stops making progress: a hung
    Whisper, or a FreeSWITCH completion that never comes while other events do, would otherwise
    hold the only call slot for good. The call reports every finished step with kick()."""

    def __init__(self, limit=STALL_SECONDS, on_stall=None):
        self.limit = limit
        self.on_stall = on_stall or self._restart
        self.lock = threading.Lock()
        self.since = None  # when the current call last made progress; None = no call
        threading.Thread(target=self._watch, daemon=True).start()

    def start(self):
        with self.lock:
            self.since = time.monotonic()

    def kick(self):
        with self.lock:
            if self.since is not None:
                self.since = time.monotonic()

    def stop(self):
        with self.lock:
            self.since = None

    def _watch(self):
        while True:
            time.sleep(min(5.0, self.limit / 4))
            with self.lock:
                stalled = self.since is not None and time.monotonic() - self.since > self.limit
                if stalled:
                    self.since = None
            if stalled:
                log(f"call made no progress for {self.limit:.0f} s: restarting")
                self.on_stall()

    @staticmethod
    def _restart():
        ClaudeTurn.kill_all()
        os._exit(70)  # launchd KeepAlive brings the agent back; FreeSWITCH drops the stuck call


class Conversation:
    """One phone call with Claude: listen, ask, answer, until goodbye or hangup."""

    def __init__(self, call, transcribe, start_turn, hub, synthesize, progress=lambda: None):
        self.call = call
        self.transcribe = transcribe  # wav path -> text ("" for silence)
        self.start_turn = start_turn  # (text, session, resume) -> ClaudeTurn
        self.hub = hub
        self.synthesize = synthesize  # (text, wav path) -> wav path
        self.progress = progress      # told after every finished step (Watchdog.kick)
        self.tmp = tempfile.mkdtemp(prefix="call-")
        self.files = itertools.count()
        self.session, self.resume = str(uuid.uuid4()), False  # the Mini's own Claude
        self.target = None                                     # or a desktop session
        self.desk_sessions = set()

    def run(self):
        try:
            self.run_app("answer")
            self.run_app("set", "playback_terminators=#")
            self.target = self.hub.desk_target()
            if self.target is None:
                self.say(GREETING + DESK_FALLBACK)
            else:
                self.desk_sessions.add(str(self.target.get("session_id") or ""))  # the hub holds it from now on
                self.say(GREETING)
                if self.target.get("title") != desk_gateway.PHONE_SESSION:
                    self.say(f"现在连的是 {self.target.get('title')}。")
            while True:
                text = self.listen()
                if text is None:
                    self.say(NOBODY)
                    break
                log(f"heard: {text}")
                if is_goodbye(text):
                    self.say(BYE)
                    break
                self.say(speakable(self.ask(text)))
            self.run_app("hangup")
        except esl.Hangup as exc:
            log(f"call over: {exc}")
        finally:
            for session in self.desk_sessions:
                self.hub.desk_end(session)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self):
        return os.path.join(self.tmp, f"{next(self.files)}.wav")

    def run_app(self, app, arg=""):
        event = self.call.execute(app, arg)
        self.progress()
        return event

    def say(self, text):
        self.run_app("playback", self.synthesize(text, self.path()))

    def listen(self):
        """What the caller says next, or None after PATIENCE silent recordings."""
        self.run_app("playback", BEEP)
        for _ in range(PATIENCE):
            wav = self.path()
            self.run_app("record", f"{wav} {RECORD_LIMIT} {SILENCE_LEVEL} {SILENCE_SECONDS}")
            text = self.transcribe(wav) if os.path.exists(wav) else ""
            self.progress()
            if text:
                return text
        return None

    def ask(self, text):
        desk = self.target is not None
        if desk:
            turn = DeskTurn(self.hub, self.target, text)
        else:
            turn = self.start_turn(text, self.session, self.resume)
        announced = set()
        try:
            while not turn.done():
                if turn.expired():
                    turn.stop()
                    if not desk:
                        self.session, self.resume = str(uuid.uuid4()), False
                    return getattr(turn, "too_slow", TOO_SLOW)
                asking = turn.session if desk else self.session
                for req in self.hub.pending(asking):
                    if req.get("id") not in announced:
                        announced.add(req.get("id"))
                        self.approve(req, asking)
                self.run_app("playback", TICK)
        finally:  # hung up, or anything else went wrong: do not leave Claude running
            if not turn.done():
                turn.stop()
        if desk:
            self.desk_sessions.add(turn.session)
            answer = turn.answer()
            answer = answer if answer is not None else (turn.failure or CLAUDE_FAILED)
            log(f"{self.target.get('title')}: {answer[:200]}")
            return answer
        answer = turn.answer()
        if answer is None:
            self.session, self.resume = str(uuid.uuid4()), False  # the next turn starts clean
            return CLAUDE_FAILED
        self.resume = True
        log(f"claude: {answer[:200]}")
        return answer

    def approve(self, req, session):
        var = "approval_" + re.sub(r"[^0-9a-z]", "", str(req.get("id")))
        text, by_ear = describe_request(req)
        prompt = self.synthesize(text + (KEYS if by_ear else SCREEN_ONLY), self.path())
        keys = ("1", "2") if by_ear else ("2",)
        done = self.run_app("play_and_get_digits", f"1 1 1 {KEYPAD_TIMEOUT_MS} # {prompt} silence_stream://250 "
                                                   f"{var} ^[{''.join(keys)}]$ 2000")
        digit = done.get(f"variable_{var}", "")
        if digit not in keys:
            if any(r.get("id") == req.get("id") for r in self.hub.pending(session)):
                self.say(NO_KEY)  # still open; if it is gone, the screen answered it meanwhile
            return
        decision = "allow" if digit == "1" else "deny"
        recorded = self.hub.decide(req.get("id"), session, decision)
        log(f"keypad {req.get('id')}: {decision}{'' if recorded else ' (too late)'}")
        self.say(("已允许。" if decision == "allow" else "已拒绝。") if recorded else ALREADY_ANSWERED)


class CallServer(socketserver.ThreadingTCPServer):
    """Takes the calls FreeSWITCH connects here, one at a time."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, on_call, watchdog):
        self.on_call = on_call
        self.watchdog = watchdog
        self.busy = threading.Lock()
        super().__init__(address, CallHandler)


class CallHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        if not server.busy.acquire(blocking=False):
            log("a second call while busy: turned away")
            return  # closing the socket ends that call
        server.watchdog.start()
        try:
            self.request.settimeout(SOCKET_TIMEOUT)
            call = esl.Call(self.request)
            caller = call.variables.get("Caller-Caller-ID-Number", "?")
            # set from the directory entry the caller authenticated as; the dialplan checks it too
            if call.variables.get("variable_claude_caller") != "true":
                log(f"call from {caller} refused: only the desk phone may talk to Claude")
                return
            log(f"call from {caller}")
            server.on_call(call)
        except (esl.Hangup, esl.ESLError, OSError) as exc:
            log(f"call ended: {exc!r}")
        finally:
            server.watchdog.stop()
            server.busy.release()


def main():
    key = render.load_secrets(os.path.join(HERE, "secrets.env"))["HUB_HOOK_PASSWORD"]
    hub = HubClient(HUB_URL, key)
    os.makedirs(WORKSPACE, exist_ok=True)
    transcriber = speech.Transcriber(MODEL_DIR)
    transcriber.warm_up()

    watchdog = Watchdog()

    def start_turn(text, session, resume):
        return ClaudeTurn(claude_argv(session, resume), text, env=claude_env())

    def on_call(call):
        Conversation(call, transcriber, start_turn, hub, lambda text, path: speech.synthesize(text, path, VOICE),
                     progress=watchdog.kick).run()

    def on_term(signum, frame):  # launchctl stop / kickstart -k
        ClaudeTurn.kill_all()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    server = CallServer(LISTEN, on_call, watchdog)
    log(f"voice-agent on {LISTEN[0]}:{LISTEN[1]}, Claude works in {WORKSPACE}")
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

"""voice_agent: the conversation loop with a fake call, fake Whisper, fake Claude and fake hub,
plus the real pieces it talks to (claude subprocess handling, phone-hub's local routes)."""
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time

import pytest

import esl
import voice_agent as va


class FakeCall:
    def __init__(self, digits=(), hang_up_at=None):
        self.log = []
        self.digits = list(digits)
        self.hang_up_at = hang_up_at  # (app, nth) at which the caller hangs up

    def execute(self, app, arg=""):
        self.log.append((app, arg))
        if self.hang_up_at and self.hang_up_at[0] == app and self.count(app) == self.hang_up_at[1]:
            raise esl.Hangup("caller hung up")
        if app == "record":
            open(arg.split()[0], "wb").close()
        if app == "play_and_get_digits":
            var = arg.split()[7]
            return {f"variable_{var}": self.digits.pop(0) if self.digits else ""}
        return {}

    def count(self, app):
        return sum(1 for a, _ in self.log if a == app)


class FakeTurn:
    def __init__(self, answer, polls=1, expired=False):
        self.result_text, self.polls, self.is_expired = answer, polls, expired
        self.stopped = False

    def done(self):
        self.polls -= 1
        return self.polls < 0

    def expired(self):
        return self.is_expired

    def stop(self):
        self.stopped = True

    def answer(self):
        return self.result_text


class FakeHub:
    def __init__(self, pending=(), ok=True, target=None, sends=(), replies=()):
        self.script = list(pending)  # what each pending() poll returns, last one repeats
        self.decisions = []
        self.ok = ok
        self.target = target          # the desk session a call talks to (None: the Mini's Claude)
        self.sends, self.replies = list(sends), list(replies)
        self.sent, self.ended = [], []

    def desk_target(self):
        return self.target

    def desk_send(self, host_id, text):
        self.sent.append((host_id, text))
        return self.sends.pop(0) if self.sends else {"ok": True, "offset": 5, "session_id": "s-desk"}

    def desk_reply(self, host_id, offset, sent):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def desk_end(self, session_id):
        self.ended.append(session_id)

    def pending(self, session):
        return self.script.pop(0) if len(self.script) > 1 else (self.script[0] if self.script else [])

    def decide(self, request_id, session, decision):
        self.decisions.append((request_id, session, decision))
        return self.ok


def converse(heard, turns, call=None, hub=None):
    """Run one call; heard: what Whisper returns per recording; turns: FakeTurns in order."""
    call, hub, said, asked = call or FakeCall(), hub or FakeHub(), [], []
    turns = list(turns)

    def start_turn(text, session, resume):
        asked.append((text, session, resume))
        return turns.pop(0)

    heard, progress = list(heard), []
    conversation = va.Conversation(call, lambda path: heard.pop(0) if heard else "", start_turn, hub,
                                   lambda text, path: said.append(text) or path,
                                   progress=lambda: progress.append(len(call.log)))
    try:
        conversation.run()
    finally:
        assert not os.path.exists(conversation.tmp)  # recordings and audio are gone after the call
    # the watchdog hears about every finished step (each app and each transcription)
    assert len(progress) >= len(call.log) - (1 if call.log and call.log[-1][0] in ("hangup", "playback") else 0)
    return call, said, asked


def test_a_question_is_answered_and_goodbye_ends_the_call():
    call, said, asked = converse(["今天几号", "好的，再见。"], [FakeTurn("今天是十月二十七号。")])
    assert said == [va.GREETING + va.DESK_FALLBACK, "今天是十月二十七号。", "好的，再见。"]
    assert [q for q, _, _ in asked] == ["今天几号"]
    assert call.log[:2] == [("answer", ""), ("set", "playback_terminators=#")]
    assert call.log[-1] == ("hangup", "")
    record = next(arg for app, arg in call.log if app == "record").split()
    assert record[1:] == [str(va.RECORD_LIMIT), str(va.SILENCE_LEVEL), str(va.SILENCE_SECONDS)]


def test_follow_ups_resume_the_same_claude_session():
    _, _, asked = converse(["几点了", "那明天呢", "拜拜"], [FakeTurn("三点。"), FakeTurn("明天也是。")])
    (_, first, resume1), (_, second, resume2) = asked
    assert first == second and (resume1, resume2) == (False, True)


def test_a_failed_answer_is_reported_and_the_next_turn_starts_fresh():
    _, said, asked = converse(["几点了", "再问一次", "再见"], [FakeTurn(None), FakeTurn("三点。")])
    assert said[1] == va.CLAUDE_FAILED and said[2] == "三点。"
    (_, first, _), (_, second, resume) = asked
    assert first != second and resume is False


def test_silence_hangs_up_after_a_while():
    call, said, asked = converse([], [])
    assert call.count("record") == va.PATIENCE and asked == []
    assert said[-1] == va.NOBODY and call.log[-1] == ("hangup", "")


def test_ticks_play_while_claude_works():
    call, _, _ = converse(["查一下", "再见"], [FakeTurn("好了。", polls=3)])
    assert [arg for app, arg in call.log if app == "playback"].count(va.TICK) == 3


@pytest.mark.parametrize("digit, decision, reply", [("1", "allow", "已允许。"), ("2", "deny", "已拒绝。")])
def test_a_permission_request_is_read_out_and_answered_with_the_keypad(digit, decision, reply):
    req = {"id": "ab12cd34", "tool_name": "Bash", "text": "$ uptime\n看看开机多久",
           "command": "uptime", "description": "看看开机多久"}
    hub = FakeHub(pending=[[req]])  # still listed on later polls: announced only once
    call, said, asked = converse(["开机多久了", "再见"], [FakeTurn("十天了。", polls=3)],
                                 call=FakeCall(digits=[digit]), hub=hub)
    session = asked[0][1]
    assert hub.decisions == [("ab12cd34", session, decision)]
    assert said[1] == "Claude 想运行命令：uptime。看看开机多久。按 1 允许，按 2 拒绝。"
    assert said[2] == reply and said[3] == "十天了。"
    prompt = next(arg for app, arg in call.log if app == "play_and_get_digits").split()
    assert prompt[:5] == ["1", "1", "1", str(va.KEYPAD_TIMEOUT_MS), "#"] and prompt[7] == "approval_ab12cd34"


def test_no_key_leaves_the_request_to_the_screen_and_a_late_key_is_reported():
    req = {"id": "ab12cd34", "tool_name": "Bash", "text": "$ uptime", "command": "uptime", "description": None}
    _, said, _ = converse(["做点事", "再见"], [FakeTurn("好。", polls=2)], hub=FakeHub(pending=[[req]]))
    assert said[1] == "Claude 想运行命令：uptime。" + va.KEYS and said[2] == va.NO_KEY
    hub = FakeHub(pending=[[req]], ok=False)
    _, said, _ = converse(["做点事", "再见"], [FakeTurn("好。", polls=2)], call=FakeCall(digits=["1"]), hub=hub)
    assert said[2] == va.ALREADY_ANSWERED


def test_a_request_answered_on_the_screen_is_not_chased_on_the_keypad():
    req = {"id": "ab12cd34", "tool_name": "Bash", "text": "$ uptime", "command": "uptime", "description": None}
    hub = FakeHub(pending=[[req], []])  # gone by the time the keypad gives up: the screen answered
    _, said, _ = converse(["做点事", "再见"], [FakeTurn("好。", polls=2)], hub=hub)
    assert va.NO_KEY not in said and hub.decisions == []


def test_the_greeting_is_short():
    # mo found the first greeting (7.2 s of instructions) too long, then picked a 4.4 s one;
    # measure what is actually heard
    import wave
    import speech
    path = speech.synthesize(va.GREETING, os.path.join(tempfile.mkdtemp(), "hi.wav"), va.VOICE)
    with wave.open(path) as w:
        assert w.getnframes() / w.getframerate() < 5


def test_the_call_prompt_steers_claude_to_what_the_phone_can_approve():
    # lookups through tools that need no approval; commands short enough to read out and allow
    # by ear; descriptions in Chinese, since they are read out too
    for words in ("WebSearch", "WebFetch", str(va.SPEAKABLE_COMMAND), "description", "中文"):
        assert words in va.VOICE_PROMPT


@pytest.mark.parametrize("req", [
    {"id": "a1", "tool_name": "Bash", "text": "$ x", "command": "x" * 80, "description": "很长"},
    {"id": "a1", "tool_name": "Bash", "text": "$ x", "command": "echo hi\nrm -rf ~/x", "description": "两行"},
    {"id": "a1", "tool_name": "mcp__x__y", "text": "{}", "command": None, "description": None},
])
def test_the_keypad_cannot_allow_what_was_not_read_out_in_full(req):
    call = FakeCall(digits=["1"])
    hub = FakeHub(pending=[[req]])
    _, said, _ = converse(["做点事", "再见"], [FakeTurn("好。", polls=2)], call=call, hub=hub)
    assert said[1].endswith(va.SCREEN_ONLY) and hub.decisions == []
    prompt = next(arg for app, arg in call.log if app == "play_and_get_digits").split()
    assert re.match(prompt[8], "2") and not re.match(prompt[8], "1")  # FreeSWITCH rejects 1 too
    hub = FakeHub(pending=[[req]])
    converse(["做点事", "再见"], [FakeTurn("好。", polls=2)], call=FakeCall(digits=["2"]), hub=hub)
    assert [d for _, _, d in hub.decisions] == ["deny"]


def test_hanging_up_while_claude_works_stops_it():
    turn = FakeTurn("不会念到这里。", polls=5)
    call, said, _ = converse(["查一下"], [turn], call=FakeCall(hang_up_at=("playback", 3)))
    assert turn.stopped and said == [va.GREETING + va.DESK_FALLBACK] and call.log[-1][0] == "playback"


def test_any_failure_while_claude_works_stops_it():
    class Broken(FakeCall):
        def execute(self, app, arg=""):
            if arg == va.TICK:
                raise esl.ESLError("playback: -ERR")
            return super().execute(app, arg)

    turn = FakeTurn("不会念到这里。", polls=5)
    with pytest.raises(esl.ESLError):
        converse(["查一下"], [turn], call=Broken())
    assert turn.stopped


def test_a_turn_that_takes_too_long_is_stopped():
    turn = FakeTurn(None, polls=5, expired=True)
    _, said, _ = converse(["很难的问题", "再见"], [turn])
    assert turn.stopped and said[1] == va.TOO_SLOW


# --- desk sessions (Claude desktop app on the Studio) ----------------------------------------
DESK = {"id": "local_d", "session_id": "s-desk", "title": "电话", "status": "idle"}
WORKING = {"landed": True, "texts": ["我查一下。"], "ended": False, "status": "busy"}


def test_a_call_talks_to_the_desk_session_and_reads_its_final_answer():
    done = {"landed": True, "texts": ["我查一下。", "现在三点。"], "ended": True, "status": "idle"}
    hub = FakeHub(target=DESK, replies=[WORKING, WORKING, done])
    call, said, asked = converse(["几点了", "再见"], [], hub=hub)
    assert hub.sent == [("local_d", va.PHONE_PREFIX + "几点了")] and asked == []
    assert said == [va.GREETING, "现在三点。", va.BYE] and hub.ended == ["s-desk"]
    assert [arg for app, arg in call.log if app == "playback"].count(va.TICK) == 2


def test_a_session_picked_on_the_phone_is_named_first():
    hub = FakeHub(target=dict(DESK, title="demo app"),
                  replies=[{"landed": True, "texts": ["好。"], "ended": True, "status": "idle"}])
    _, said, _ = converse(["跑一下测试", "再见"], [], hub=hub)
    assert said[:3] == [va.GREETING, "现在连的是 demo app。", "好。"]


@pytest.mark.parametrize("error, words", [
    ("busy", va.DESK_BUSY),
    ("ABORT: the window is not showing that session", va.DESK_NOT_SHOWN),
    ("ABORT: the screen is locked", va.DESK_LOCKED),
    ("ABORT: the prompt box holds an unsent draft", va.DESK_DRAFT),
    ("unclear title", va.DESK_UNCLEAR),
    ("titles unknown", va.DESK_UNCHECKED),
    ("UNKNOWN: the desktop app did not answer in time", va.DESK_UNSURE),
    ("relay: timed out", va.DESK_UNREACHABLE),
])
def test_why_a_desk_session_did_not_take_the_words_is_said(error, words):
    hub = FakeHub(target=DESK, sends=[{"ok": False, "error": error}], replies=[WORKING])
    _, said, _ = converse(["几点了", "再见"], [], hub=hub)
    assert said[1] == words


def test_words_that_never_reach_the_session_are_reported(monkeypatch):
    monkeypatch.setattr(va, "LAND_TIMEOUT", 0)
    hub = FakeHub(target=DESK, replies=[{"landed": False, "texts": [], "ended": False, "status": "idle"}])
    _, said, _ = converse(["几点了", "再见"], [], hub=hub)
    assert said[1] == va.DESK_NOT_LANDED


def test_hanging_up_during_the_first_desk_turn_still_ends_the_call_on_the_session():
    hub = FakeHub(target=DESK, replies=[WORKING])
    converse(["几点了"], [], call=FakeCall(hang_up_at=("playback", 3)), hub=hub)  # at the first tick
    assert hub.ended == ["s-desk"]


def test_keypad_approvals_follow_the_desk_session():
    req = {"id": "ab12cd34", "tool_name": "Bash", "text": "$ uptime", "command": "uptime", "description": None}
    hub = FakeHub(target=DESK, pending=[[req]], replies=[WORKING, {"landed": True, "texts": ["十天。"],
                                                                     "ended": True, "status": "idle"}])
    converse(["开机多久了", "再见"], [], call=FakeCall(digits=["1"]), hub=hub)
    assert hub.decisions == [("ab12cd34", "s-desk", "allow")]


def test_without_a_desk_session_the_minis_claude_answers():
    _, said, asked = converse(["几点了", "再见"], [FakeTurn("三点。")], hub=FakeHub(target=None))
    assert said[:2] == [va.GREETING + va.DESK_FALLBACK, "三点。"] and len(asked) == 1


# --- words ------------------------------------------------------------------------------
def test_answers_are_made_speakable():
    assert va.speakable("**好的**，见 https://example.com/x?y=1 和 `ls`") == "好的，见 （链接） 和 ls"
    assert va.speakable("看代码：\n```python\nprint(1)\n```\n完") == "看代码： （代码略） 完"
    long = "第一句话。" * 100
    spoken = va.speakable(long)
    assert len(spoken) <= va.MAX_SPOKEN + 20 and spoken.endswith(va.TRUNCATED)
    assert spoken[: -len(va.TRUNCATED)].endswith("。")


@pytest.mark.parametrize("text, bye", [("再见", True), ("好的，拜拜！", True), ("那我挂了啊", True),
                                       ("没事了，再见", True), ("帮我给小明发消息说再见", False),
                                       ("今天天气怎么样", False), ("电话挂了吗", False)])
def test_goodbye_means_a_short_goodbye(text, bye):
    assert va.is_goodbye(text) is bye


def test_what_is_read_out_for_a_request():
    short = {"tool_name": "Bash", "command": "uptime", "description": "看看开机多久"}
    assert va.describe_request(short) == ("Claude 想运行命令：uptime。看看开机多久。", True)
    assert va.describe_request({**short, "command": "x" * 80}) == ("Claude 想运行一条长命令。", False)
    assert va.describe_request({"tool_name": "WebFetch", "text": "{}"}) == ("Claude 想使用 WebFetch 工具。", False)


# --- claude -----------------------------------------------------------------------------
def test_claude_command_line():
    first, later = va.claude_argv("sid-1", False), va.claude_argv("sid-1", True)
    assert first[:4] == [va.CLAUDE, "-p", "--output-format", "json"]
    assert first[-2:] == ["--session-id", "sid-1"] and later[-2:] == ["--resume", "sid-1"]
    assert first[first.index("--settings") + 1] == va.SESSION_SETTINGS and "--strict-mcp-config" in first
    assert first[first.index("--append-system-prompt") + 1] == va.VOICE_PROMPT


def test_call_session_settings_route_side_effects_to_the_phone():
    with open(va.SESSION_SETTINGS) as f:
        settings = json.load(f)
    assert {"Bash", "Write", "Edit"} <= set(settings["permissions"]["ask"])
    # the call's Claude may not read the keys that sign approvals or its own login
    for secret in ("~/cisco-phone/secrets.env", "~/cisco-phone/claude.token", "~/.claude/phone-hub.token"):
        assert f"Read({secret})" in settings["permissions"]["deny"]
    assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False  # sandboxed Bash still asks
    hook = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert "PHONE_HUB_CALL=1" in hook["command"] and "PHONE_HUB_URL=http://127.0.0.1:8090" in hook["command"]
    script = hook["command"].split()[-1]
    assert script.endswith("/claude_hooks/phone_hook.py") and hook["timeout"] > 45


def fake_claude(tmp, output, code=0):
    """A stand-in claude: records its stdin and arguments, prints output."""
    path = os.path.join(tmp, "claude")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport sys, json\n"
                f"open({os.path.join(tmp, 'stdin')!r}, 'w').write(sys.stdin.read())\n"
                f"open({os.path.join(tmp, 'argv')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                f"sys.stdout.write({output!r})\nsys.exit({code})\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def run_turn(output, code=0, env=None):
    tmp = tempfile.mkdtemp()
    turn = va.ClaudeTurn([fake_claude(tmp, output, code), "-p"], "你好吗", env=env, cwd=tmp)
    while not turn.done():
        time.sleep(0.01)
    return turn.answer(), tmp


def test_claude_turn_sends_the_text_on_stdin_and_reads_the_result():
    answer, tmp = run_turn(json.dumps({"type": "result", "is_error": False, "result": "很好。"}))
    assert answer == "很好。" and open(os.path.join(tmp, "stdin")).read() == "你好吗"


@pytest.mark.parametrize("output, code", [
    (json.dumps({"is_error": True, "result": "Not logged in"}), 1),
    ("not json", 0),
    (json.dumps({"result": 42}), 0),
])
def test_claude_failures_give_no_answer(output, code):
    assert run_turn(output, code)[0] is None


def test_claude_token_file_is_used_only_when_it_holds_a_token(tmp_path):
    token = tmp_path / "claude.token"
    assert va.claude_env(str(token), {"HOME": "/h"}) == {"HOME": "/h"}
    token.write_text("sk-ant-oat01-abc\n")
    assert va.claude_env(str(token), {"HOME": "/h"}) == {"HOME": "/h", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abc"}
    token.write_text("garbage")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in va.claude_env(str(token), {})


def test_stopping_a_turn_kills_what_claude_started():
    tmp = tempfile.mkdtemp()
    pidfile = os.path.join(tmp, "child.pid")
    path = os.path.join(tmp, "claude-with-child")
    with open(path, "w") as f:  # like claude running a Bash command that holds stdout open
        f.write(f"#!{sys.executable}\nimport subprocess, time\n"
                f"p = subprocess.Popen(['sleep', '60'])\nopen({pidfile!r}, 'w').write(str(p.pid))\ntime.sleep(60)\n")
    os.chmod(path, 0o755)
    turn = va.ClaudeTurn([path], "x", cwd=tmp)
    for _ in range(200):
        if os.path.exists(pidfile) and open(pidfile).read():
            break
        time.sleep(0.02)
    child = int(open(pidfile).read())
    started = time.time()
    turn.stop()
    assert turn.done() and time.time() - started < 8
    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        raise AssertionError("the child of claude outlived the call")


def test_an_agent_that_exits_takes_running_claudes_with_it(monkeypatch):
    # Claude runs in its own process group, so launchd's clean-up of the agent's group would not
    # reach it: the watchdog restart (and SIGTERM) must end it first
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "slow")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    os.chmod(path, 0o755)
    turn = va.ClaudeTurn([path], "x", cwd=tmp)
    exits = []
    monkeypatch.setattr(va.os, "_exit", exits.append)
    va.Watchdog._restart()
    assert exits == [70]
    for _ in range(100):
        if turn.done():
            break
        time.sleep(0.05)
    assert turn.done() and turn not in va.ClaudeTurn.running


def test_stopping_a_turn_kills_claude():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "slow")
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    os.chmod(path, 0o755)
    turn = va.ClaudeTurn([path], "x", cwd=tmp, timeout=0)
    assert turn.expired() and not turn.done()
    started = time.time()
    turn.stop()
    assert turn.done() and time.time() - started < 5


# --- phone-hub's local routes, for real ------------------------------------------------------
def test_hub_client_lists_and_answers_this_calls_requests(monkeypatch):
    pytest.importorskip("PIL")
    import phone_hub
    monkeypatch.setattr(phone_hub, "PHONE_IP", "127.0.0.1")
    hub = phone_hub.Hub(lambda url: False, wait_seconds=5)
    server = phone_hub.HubServer(("127.0.0.1", 0), phone_hub.make_handler(hub, "k", phone_hub.PushAuth()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        client = va.HubClient(url, "k")
        payload = {"session_id": "call-9", "tool_name": "Bash", "tool_input": {"command": "uptime"},
                   "phone_call": True}
        result = {}
        t = threading.Thread(target=lambda: result.update(d=hub.request_permission(payload)))
        t.start()
        for _ in range(100):
            reqs = client.pending("call-9")
            if reqs:
                break
            time.sleep(0.02)
        assert [r["text"] for r in reqs] == ["$ uptime"]
        assert client.decide(reqs[0]["id"], "call-9", "deny") is True
        t.join(5)
        assert result["d"] == "deny"
        assert va.HubClient(url, "wrong-key").pending("call-9") == []  # refused, and never raises
        assert va.HubClient("http://127.0.0.1:9", "k").decide("x", "call-9", "allow") is False
    finally:
        server.shutdown()


# --- who may call, one call at a time, never stuck ------------------------------------------
DESK_PHONE = b"variable_claude_caller: true\n"


def connect_as_freeswitch(address, variables=DESK_PHONE):
    """Play FreeSWITCH's side of the handshake; returns the socket."""
    sock = socket.create_connection(address, timeout=5)
    reader = sock.makefile("rb")
    assert reader.readline() == b"connect\n"  # esl.Call introduces itself
    sock.sendall(b"Content-Type: command/reply\nReply-Text: +OK\nUnique-ID: u1\n" + variables + b"\n")
    assert reader.readline() == b"\n" and reader.readline() == b"myevents\n"
    sock.sendall(b"Content-Type: command/reply\nReply-Text: +OK\n\n")
    return sock


def serve(on_call, watchdog=None):
    server = va.CallServer(("127.0.0.1", 0), on_call, watchdog or va.Watchdog(limit=60, on_stall=lambda: None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_second_call_is_turned_away_while_one_is_in_progress():
    entered, release = threading.Event(), threading.Event()
    server = serve(lambda call: entered.set() or release.wait(5))
    try:
        connect_as_freeswitch(server.server_address)
        assert entered.wait(5)
        second = socket.create_connection(server.server_address, timeout=5)
        assert second.recv(100) == b""  # closed without a word: FreeSWITCH ends that call
    finally:
        release.set()
        server.shutdown()


@pytest.mark.parametrize("variables", [b"", b"variable_claude_caller: false\n",
                                       b"Caller-Caller-ID-Number: 101\n"])
def test_only_the_desk_phone_gets_through(variables):
    calls = []
    server = serve(calls.append)
    try:
        sock = connect_as_freeswitch(server.server_address, variables)
        assert sock.recv(100) == b"" and calls == []  # hung up on, Claude never started
        connect_as_freeswitch(server.server_address)
        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        assert len(calls) == 1
    finally:
        server.shutdown()


def test_watchdog_restarts_a_stuck_call_but_not_a_busy_or_idle_agent():
    stalled = threading.Event()
    dog = va.Watchdog(limit=0.3, on_stall=stalled.set)
    assert not stalled.wait(0.6)  # no call: nothing to watch
    dog.start()
    for _ in range(8):  # a call that keeps making progress
        time.sleep(0.1)
        dog.kick()
    assert not stalled.is_set()
    assert stalled.wait(2)  # ...and one that stops (hung Whisper, a completion that never comes)
    dog.stop()


def test_a_call_that_ends_stops_the_watchdog():
    stalled = threading.Event()
    dog = va.Watchdog(limit=0.3, on_stall=stalled.set)
    server = serve(lambda call: None, watchdog=dog)
    try:
        sock = connect_as_freeswitch(server.server_address)
        assert sock.recv(100) == b""
        assert not stalled.wait(1)
    finally:
        server.shutdown()

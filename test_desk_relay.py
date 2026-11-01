"""desk_relay against registry and transcript files shaped like Claude Code's own
(~/.claude/sessions/<pid>.json and ~/.claude/projects/<dir>/<session>.jsonl, as seen on the Studio)."""
import json
import os
import tempfile
import threading
import time
import urllib.error

import pytest

import desk_relay as dr
import hubauth

KEY = "relay-key"
HOST = "local_f41cc4d7-bb00-4732-b75b-c3f51f34dea0"
SID = "3e99685e-b749-451e-a065-cea9935a8317"


def registry_entry(pid, host=HOST, sid=SID, name="电话", status="idle", updated=1793084865478,
                   entrypoint="claude-desktop"):
    return {"pid": pid, "sessionId": sid, "cwd": "/Users/x", "startedAt": 1793083817464, "kind": "interactive",
            "entrypoint": entrypoint, "hostSessionId": host, "pidDomain": "darwin",
            "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock", "name": name, "nameSource": "user",
            "status": status, "updatedAt": updated, "peerFeatures": ["notify_idle"]}


def user(text, **extra):
    return {"type": "user", "timestamp": "2026-10-27T07:07:37.000Z", "origin": {"kind": "human"},
            "promptSource": "sdk", "entrypoint": "claude-desktop", "message": {"role": "user", "content": text},
            **extra}


def assistant(*texts):
    return {"type": "assistant", "timestamp": "2026-10-27T07:07:43.000Z", "entrypoint": "claude-desktop",
            "message": {"role": "assistant", "content": [{"type": "text", "text": t} for t in texts]}}


def tool_use():
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": ""}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}


def tool_result():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]}}


STOP = {"type": "system", "subtype": "stop_hook_summary", "message": {"role": None, "content": []}}


@pytest.fixture
def home():
    root = tempfile.mkdtemp()
    reg, proj = os.path.join(root, "sessions"), os.path.join(root, "projects", "-Users-x")
    os.makedirs(reg)
    os.makedirs(proj)
    os.makedirs(os.path.join(root, "app", "account", "org"))  # the desktop app's own session records
    return reg, os.path.join(root, "projects"), os.path.join(proj, SID + ".jsonl")


def app_record(home, host, title, archived=False):
    root = os.path.join(os.path.dirname(home[1]), "app", "account", "org")
    with open(os.path.join(root, host + ".json"), "w") as f:
        json.dump({"sessionId": host, "cliSessionId": SID, "title": title, "isArchived": archived}, f,
                  ensure_ascii=False)


def write_registry(reg, *entries):
    for e in entries:
        with open(os.path.join(reg, f"{e['pid']}.json"), "w") as f:
            json.dump(e, f, ensure_ascii=False)


def write_transcript(path, *records, mode="w"):
    with open(path, mode) as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --- sessions -----------------------------------------------------------------------------
def test_lists_live_desktop_sessions_newest_first(home):
    reg, _, _ = home
    write_registry(reg,
                   registry_entry(11, host="local_aa", name="旧会话", updated=1),
                   registry_entry(12, host="local_bb", name="新会话", status="busy", updated=9),
                   registry_entry(13, host="local_cc", name="死进程", updated=5),
                   registry_entry(14, host="local_dd", entrypoint="sdk-cli", updated=7),
                   {"pid": 15, "sessionId": SID, "entrypoint": "claude-desktop"})  # no hostSessionId
    with open(os.path.join(reg, "16.json"), "w") as f:
        f.write("{half written")
    sessions = dr.live_sessions(reg, alive=lambda pid: pid != 13)
    assert [(s["id"], s["title"], s["status"]) for s in sessions] == [("local_bb", "新会话", "busy"),
                                                                        ("local_aa", "旧会话", "idle")]


def test_transcript_lookup_takes_only_session_ids(home):
    _, projects, path = home
    write_transcript(path, user("hi"))
    assert dr.transcript(SID, projects) == path
    assert dr.transcript("../../etc/passwd", projects) is None
    assert dr.transcript("*", projects) is None


# --- reading a session ----------------------------------------------------------------------
def test_recent_turns_keep_what_was_said_and_skip_tool_traffic(home):
    _, _, path = home
    write_transcript(path, user("第一句"), assistant("好的。"), user("<command-name>/clear</command-name>"),
                     user("跑一下测试"), tool_use(), tool_result(), assistant("", "测试全过了。"),
                     user("ignored", isMeta=True), STOP)
    turns = dr.recent_turns(path, limit=3)
    assert turns == [{"role": "assistant", "text": "好的。"}, {"role": "user", "text": "跑一下测试"},
                     {"role": "assistant", "text": "测试全过了。"}]


def test_reply_waits_for_our_message_then_its_answer_and_the_end_of_the_turn(home):
    _, _, path = home
    write_transcript(path, user("旧问题"), assistant("旧回答"), STOP)
    offset = os.path.getsize(path)
    assert dr.reply_since(path, offset, "（电话）几点了") == {"landed": False, "texts": [], "ended": False}
    write_transcript(path, user("（电话）几点了"), tool_use(), tool_result(), assistant("我查一下。"), mode="a")
    assert dr.reply_since(path, offset, "（电话）几点了") == {"landed": True, "texts": ["我查一下。"], "ended": False}
    write_transcript(path, assistant("三点。"), STOP, mode="a")
    assert dr.reply_since(path, offset, "（电话）几点了") == {"landed": True, "texts": ["我查一下。", "三点。"],
                                                           "ended": True}


def test_a_newer_message_typed_at_the_computer_ends_our_wait(home):
    _, _, path = home
    write_transcript(path, user("（电话）几点了"), assistant("我查一下。"), user("先别查了"))
    assert dr.reply_since(path, 0, "（电话）几点了") == {"landed": True, "texts": ["我查一下。"], "ended": True}


def test_reply_stops_at_the_end_of_our_turn(home):
    _, _, path = home
    write_transcript(path, user("问"), assistant("答。"), STOP, user("另一件事"), assistant("另一个回答。"))
    assert dr.reply_since(path, 0, "问") == {"landed": True, "texts": ["答。"], "ended": True}
    write_transcript(path, user("问"), assistant("先看看。"), user("别看了"), assistant("好的。"))
    assert dr.reply_since(path, 0, "问") == {"landed": True, "texts": ["先看看。"], "ended": True}


def test_only_our_whole_message_counts_as_landed(home):
    _, _, path = home
    write_transcript(path, user("帮我把（电话语音）几点了这句记下来"), assistant("好。"))
    assert dr.reply_since(path, 0, "（电话语音）几点了")["landed"] is False


def test_a_line_still_being_written_is_skipped(home):
    _, _, path = home
    write_transcript(path, user("问"))
    with open(path, "a") as f:
        f.write('{"type": "assistant", "message": {"con')
    assert dr.reply_since(path, 0, "问")["landed"] is True


# --- sending ------------------------------------------------------------------------------------
class FakeDesk:
    def __init__(self, outcome="SENT"):
        self.outcome, self.typed = outcome, []

    def ax(self, host_id, title, text, hints):
        self.typed.append((host_id, title, text, hints))
        return self.outcome, ({"anchor": ["seen-at", len(self.typed)]} if self.outcome == "SENT" else None)


def relay(home, desk, **entry):
    reg, projects, path = home
    write_registry(reg, registry_entry(os.getpid(), **entry))
    write_transcript(path, user("旧"), assistant("旧回答"), STOP)
    app_record(home, entry.get("host", HOST), entry.get("name", "电话"))
    return dr.Relay(registry=reg, projects=projects, ax=desk.ax,
                    app_sessions=os.path.join(os.path.dirname(projects), "app"))


def test_send_types_through_the_accessibility_script_and_remembers_where(home):
    desk = FakeDesk()
    r = relay(home, desk)
    size = os.path.getsize(home[2])
    result = r.send(HOST, "（电话）几点了")
    assert result == {"ok": True, "error": "", "offset": size, "session_id": SID, "title": "电话"}
    r.send(HOST, "再问一句")
    assert desk.typed == [(HOST, "电话", "（电话）几点了", None),
                          (HOST, "电话", "再问一句", {"anchor": ["seen-at", 1]})]


@pytest.mark.parametrize("entry, sid, text, error", [
    ({"status": "busy"}, HOST, "几点了", "busy"),
    ({}, "local_nope", "几点了", "no such session"),
    ({}, HOST, "   ", "empty"),
    ({}, HOST, "x" * 5000, "too long"),
])
def test_send_refuses_without_touching_the_app(home, entry, sid, text, error):
    desk = FakeDesk()
    result = relay(home, desk, **entry).send(sid, text)
    assert result["ok"] is False and result["error"] == error and desk.typed == []


def test_an_aborted_accessibility_run_is_reported(home):
    desk = FakeDesk("ABORT: the window is not showing that session")
    result = relay(home, desk).send(HOST, "几点了")
    assert result["ok"] is False and result["error"] == "ABORT: the window is not showing that session"


def test_send_needs_a_title_that_names_one_live_session(home):
    reg, _, _ = home
    desk = FakeDesk()
    r = relay(home, desk)
    write_registry(reg, registry_entry(os.getppid(), host="local_twin", sid="9" * 8 + SID[8:]))  # also 电话
    assert r.send(HOST, "几点了")["error"] == "unclear title"
    r = relay(home, desk, name="  ")
    assert r.send(HOST, "几点了")["error"] == "unclear title" and desk.typed == []


def test_a_session_that_is_not_running_counts_for_the_title_too(home):
    desk = FakeDesk()
    r = relay(home, desk)
    assert r.send(HOST, "几点了")["ok"] is True
    app_record(home, "local_old", "电话", archived=True)  # could still be open in some window
    assert r.send(HOST, "几点了")["error"] == "unclear title" and len(desk.typed) == 1


def test_titles_that_cannot_all_be_read_are_not_called_unique(home, monkeypatch):
    monkeypatch.setattr(dr, "RECORD_RETRY", 0)
    desk = FakeDesk()
    r = relay(home, desk)
    app_dir = os.path.join(r.app_sessions, "account", "org")
    with open(os.path.join(app_dir, "local_half.json"), "w") as f:
        f.write('{"sessionId": "local_half", "tit')  # the app is writing it, or it broke
    assert r.send(HOST, "几点了")["error"] == "titles unknown"
    os.remove(os.path.join(app_dir, "local_half.json"))
    os.remove(os.path.join(app_dir, HOST + ".json"))  # the target has no record: the store is not the one we know
    assert r.send(HOST, "几点了")["error"] == "titles unknown"
    r.app_sessions = os.path.join(r.app_sessions, "moved")
    assert r.send(HOST, "几点了")["error"] == "titles unknown" and desk.typed == []


def test_a_send_that_waited_too_long_for_the_lock_is_refused(home, monkeypatch):
    monkeypatch.setattr(dr, "LOCK_WAIT", 0.2)
    desk = FakeDesk()
    r = relay(home, desk)
    with r.lock:  # a send from an earlier call is still typing
        result = r.send(HOST, "几点了")
    assert result["error"] == "busy" and desk.typed == []


def test_an_uncertain_send_counts_once_the_words_reach_the_session(home, monkeypatch):
    monkeypatch.setattr(dr, "LAND_WAIT", 0.3)
    _, _, path = home
    r = relay(home, FakeDesk())

    def pressed_then_cut_off(host_id, title, text, hints):
        write_transcript(path, user(text), mode="a")
        return "UNKNOWN: the desktop app did not answer in time", None

    r.ax = pressed_then_cut_off
    assert r.send(HOST, "几点了")["ok"] is True
    r.ax = lambda *args: ("UNKNOWN: the desktop app did not answer in time", None)
    result = r.send(HOST, "几点了")  # the earlier one is before this send's offset
    assert result["ok"] is False and result["error"].startswith("UNKNOWN")


def test_the_session_is_checked_again_once_the_lock_is_free(home):
    reg, _, _ = home
    desk = FakeDesk()
    r = relay(home, desk)
    result = {}
    with r.lock:  # another send is typing; when it is done the session is busy
        t = threading.Thread(target=lambda: result.update(r.send(HOST, "几点了")))
        t.start()
        time.sleep(0.2)
        write_registry(reg, registry_entry(os.getpid(), status="busy"))
    t.join(5)
    assert result["error"] == "busy" and desk.typed == []


def test_reply_reports_the_session_status_too(home):
    desk = FakeDesk()
    r = relay(home, desk, status="busy")
    offset = os.path.getsize(home[2])
    write_transcript(home[2], user("几点了"), assistant("三点。"), STOP, mode="a")
    assert r.reply(HOST, offset, "几点了") == {"landed": True, "texts": ["三点。"], "ended": True, "status": "busy"}
    assert r.reply(HOST, 10 ** 9, "几点了")["error"] == "bad offset"


# --- HTTP ------------------------------------------------------------------------------------------
@pytest.fixture
def server(home, monkeypatch):
    desk = FakeDesk()
    r = relay(home, desk)
    monkeypatch.setattr(dr, "CLIENTS", {"127.0.0.1"})
    srv = dr.RelayServer(("127.0.0.1", 0), dr.make_handler(r, KEY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", desk, monkeypatch
    srv.shutdown()


def test_http_routes_need_the_key_and_the_mini(server):
    base, desk, monkeypatch = server
    ask = lambda path, payload: hubauth.post(base, KEY, path, payload, signed_answer=True)  # noqa: E731
    assert [s["id"] for s in ask("/sessions", {})["sessions"]] == [HOST]
    assert ask("/view", {"id": HOST})["turns"][-1] == {"role": "assistant", "text": "旧回答"}
    assert ask("/send", {"id": HOST, "text": "几点了"})["ok"] is True
    with pytest.raises(urllib.error.HTTPError) as err:
        hubauth.post(base, "wrong", "/send", {"id": HOST, "text": "rm -rf"})
    assert err.value.code == 401 and len(desk.typed) == 1
    monkeypatch.setattr(dr, "CLIENTS", {"192.168.1.10"})
    with pytest.raises(urllib.error.HTTPError) as err:
        hubauth.post(base, KEY, "/sessions", {})
    assert err.value.code == 403


# --- bringing a session to the front (GO on the phone) -------------------------------------------
def focus_relay(home, outcome="OPENED"):
    reg, projects, _ = home
    write_registry(reg, registry_entry(os.getpid()))
    opened = []
    r = dr.Relay(registry=reg, projects=projects, focus=lambda host: opened.append(host) or outcome)
    return r, opened


def test_focus_opens_the_session_that_runs_the_transcript(home):
    r, opened = focus_relay(home)
    assert r.focus(SID) == {"ok": True, "error": ""}
    assert opened == [HOST]


def test_focus_refuses_an_unknown_session_without_touching_the_app(home):
    r, opened = focus_relay(home)
    assert r.focus("nope") == {"ok": False, "error": "no such session"}
    assert r.focus("") == {"ok": False, "error": "no such session"} and opened == []


def test_focus_reports_a_locked_screen(home):
    r, _ = focus_relay(home, outcome="LOCKED")
    assert r.focus(SID) == {"ok": False, "error": "locked"}


def test_focus_waits_for_no_send_and_switches_nothing_while_one_runs(home):
    r, opened = focus_relay(home)
    r.lock.acquire()  # a voice send is filling another session's prompt box
    try:
        started = time.time()
        assert r.focus(SID) == {"ok": False, "error": "busy"}
        assert opened == [] and time.time() - started < dr.FOCUS_LOCK_WAIT + 1
    finally:
        r.lock.release()

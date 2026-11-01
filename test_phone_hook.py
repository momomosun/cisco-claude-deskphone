"""claude_hooks/phone_hook.py run as Claude Code would: JSON on stdin, decision on stdout."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import phone_hub

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_hooks", "phone_hook.py")
KEY = "tok"


@pytest.fixture
def fake_hub():
    """Stands in for phone_hub: checks the request signature and answers with config['decision']."""
    seen = []
    config = {"decision": None, "sign_with": KEY, "raw": None, "answers": None, "answers_signed": None,
              "redirect": None, "hold": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(config["hold"])  # the phone takes its time
            ts, nonce = self.headers.get("X-Hub-Time"), self.headers.get("X-Hub-Nonce")
            valid = self.headers.get("X-Hub-Signature") == phone_hub.sign(KEY, ts, nonce, self.path, body)
            seen.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body), "valid": valid})
            if self.path == "/hook/next-step":
                r = config["redirect"]
                sig = phone_hub.sign(config["sign_with"], nonce, "redirect" if r else "keep")
                data = json.dumps({"redirect": r, "sig": sig}).encode()
            elif config["raw"] is not None:
                data = config["raw"]
            elif config["answers"] is not None:  # a question answered on the phone
                signed = config["answers_signed"]
                sig = (phone_hub.sign(KEY, nonce, "allow", phone_hub.canonical(signed)) if signed is not None
                       else phone_hub.sign(KEY, nonce, "allow"))
                data = json.dumps({"decision": "allow", "answers": config["answers"], "sig": sig}).encode()
            else:
                d = config["decision"]
                data = json.dumps({"decision": d, "sig": phone_hub.sign(config["sign_with"], nonce, str(d))}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen, config
    server.shutdown()


def run_hook(payload, hub_url, key=KEY, raw_stdin=None, extra_env=None):
    token_file = os.path.join(tempfile.mkdtemp(), "token")
    if key is not None:
        with open(token_file, "w") as f:
            f.write(key + "\n")
    env = {k: v for k, v in os.environ.items() if k != "PHONE_HUB_CALL"}
    env.update(PHONE_HUB_URL=hub_url, PHONE_HUB_TOKEN_FILE=token_file, PHONE_HUB_APP_SESSIONS=tempfile.mkdtemp())
    env.update(extra_env or {})
    started = time.time()
    proc = subprocess.run([sys.executable, HOOK], input=raw_stdin if raw_stdin is not None else json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=20)
    return proc, time.time() - started


PERMISSION = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/x",
              "transcript_path": "/private/transcript.jsonl", "tool_name": "Bash",
              "tool_input": {"command": "ls", "description": "list"}}


@pytest.mark.parametrize("verdict", ["allow", "deny"])
def test_signed_phone_verdict_becomes_the_hook_decision(fake_hub, verdict):
    url, seen, config = fake_hub
    config["decision"] = verdict
    proc, _ = run_hook(PERMISSION, url)
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == verdict
    req = seen[0]
    assert req["path"] == "/hook/permission" and req["valid"]
    assert "transcript_path" not in req["body"]
    assert KEY not in json.dumps(req["headers"])  # the key itself never goes over the wire


@pytest.mark.parametrize("raw", [
    b'{"decision": "allow"}',                    # unsigned
    b'{"decision": "allow", "sig": "deadbeef"}',  # wrong signature
    b'["allow"]', b"null", b"not json",
])
def test_forged_or_malformed_answers_are_ignored(fake_hub, raw):
    url, _, config = fake_hub
    config["raw"] = raw
    proc, _ = run_hook(PERMISSION, url)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


def test_answer_signed_with_another_key_is_ignored(fake_hub):
    url, _, config = fake_hub
    config.update(decision="allow", sign_with="someone-else")
    proc, _ = run_hook(PERMISSION, url)
    assert proc.stdout == ""


def test_no_phone_answer_leaves_the_normal_dialog(fake_hub):
    url, _, _ = fake_hub
    proc, _ = run_hook(PERMISSION, url)
    assert proc.returncode == 0 and proc.stdout == ""


def test_a_call_session_marks_its_requests_for_the_phone(fake_hub):
    url, seen, config = fake_hub
    config["decision"] = "allow"
    proc, _ = run_hook(PERMISSION, url, extra_env={"PHONE_HUB_CALL": "1"})
    assert json.loads(proc.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert seen[0]["body"]["phone_call"] is True and seen[0]["valid"]
    run_hook({**PERMISSION, "phone_call": True}, url)  # Claude's own payload cannot set it
    assert "phone_call" not in seen[1]["body"]


def test_large_inputs_are_trimmed_and_flagged(fake_hub):
    url, seen, _ = fake_hub
    run_hook({**PERMISSION, "tool_input": {"command": "y" * 5000}}, url)
    body = seen[0]["body"]
    assert body["truncated"] is True and len(body["tool_input"]["command"]) <= 2001


@pytest.mark.parametrize("stdin", ["null", "[]", "not json", ""])
def test_bad_stdin_exits_quietly(fake_hub, stdin):
    url, seen, _ = fake_hub
    proc, _ = run_hook(None, url, raw_stdin=stdin)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == "" and seen == []


def test_hub_down_or_no_key_exits_quietly_and_fast():
    proc, took = run_hook(PERMISSION, "http://127.0.0.1:9")
    assert proc.returncode == 0 and proc.stdout == "" and took < 5
    proc, _ = run_hook(PERMISSION, "http://127.0.0.1:9", key=None)
    assert proc.returncode == 0 and proc.stdout == ""


def test_other_events_are_forwarded_without_output(fake_hub):
    url, seen, config = fake_hub
    config["decision"] = "allow"  # must be ignored for non-permission events
    proc, _ = run_hook({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/x",
                        "last_assistant_message": "done"}, url)
    assert proc.stdout == "" and seen[0]["path"] == "/hook/event" and seen[0]["valid"]


ANSWERS = {"用哪个方案？": "乙", "要哪些功能？": ["A", "C"]}
QUESTION = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/x", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "用哪个方案？", "header": "方案", "multiSelect": False,
                                          "options": [{"label": "甲"}, {"label": "乙"}]}]}}


def test_answers_from_the_phone_become_the_questions_answers(fake_hub):
    url, seen, config = fake_hub
    config.update(answers=ANSWERS, answers_signed=ANSWERS)
    proc, _ = run_hook(QUESTION, url)
    # updatedInput replaces the whole tool input: the questions as asked, plus the answers
    assert json.loads(proc.stdout) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "allow", "updatedInput": {**QUESTION["tool_input"], "answers": ANSWERS}}}}


@pytest.mark.parametrize("signed", [{"用哪个方案？": "甲", "要哪些功能？": ["A", "C"]}, None])
def test_answers_the_signature_does_not_cover_are_ignored(fake_hub, signed):
    url, seen, config = fake_hub
    config.update(answers=ANSWERS, answers_signed=signed)  # changed on the way, or signed as a bare allow
    proc, _ = run_hook(QUESTION, url)
    assert proc.stdout == ""



NEXT_STEP = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/x", "tool_name": "mcp__ccd_turn__wrap_up",
             "tool_input": {"recap": "做完了。", "ask": "要提交吗？", "actions": [{"label": "提交"}]}}


def test_away_a_next_step_question_becomes_a_phone_question(fake_hub):
    url, seen, config = fake_hub
    config["redirect"] = True
    proc, _ = run_hook(NEXT_STEP, url)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "AskUserQuestion" in out["permissionDecisionReason"]  # what to ask with instead
    assert seen[0]["path"] == "/hook/next-step" and seen[0]["valid"]


def test_the_next_step_box_stays_otherwise(fake_hub):
    url, seen, config = fake_hub
    config["redirect"] = False
    assert run_hook(NEXT_STEP, url)[0].stdout == ""  # at the desk
    config.update(redirect=True, sign_with="someone-else")
    assert run_hook(NEXT_STEP, url)[0].stdout == ""  # an answer the hub did not sign
    proc, _ = run_hook({**NEXT_STEP, "tool_input": {"recap": "做完了。"}}, url)
    assert proc.stdout == "" and len(seen) == 2  # nothing asked: the hub is not even called
    proc, took = run_hook(NEXT_STEP, "http://127.0.0.1:9")
    assert proc.returncode == 0 and proc.stdout == "" and took < 5


def test_the_hook_waits_for_the_phone_as_long_as_the_hub_does():
    spec = importlib.util.spec_from_file_location("phone_hook", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook.HUB_WAIT >= phone_hub.WAIT_SECONDS


def tool_use(ident, tool_input=PERMISSION["tool_input"]):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": "Bash", "input": tool_input}]}}


def tool_result(ident):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": "ok"}]}}


def append_later(path, delay, entry):
    def append():
        time.sleep(delay)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    threading.Thread(target=append, daemon=True).start()


def test_answered_on_the_computer_first_the_hook_lets_go(fake_hub, tmp_path):
    # Claude Code takes the computer's answer but leaves the hook running: the hook sees the
    # answer in the transcript and exits, so the hub takes the request off the phone
    url, seen, config = fake_hub
    config["hold"] = 10  # the phone has not answered
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in (
        tool_use("old"), tool_result("old"),  # the same call made and answered before
        tool_use("other", {"command": "pwd"}), tool_use("ours"))))
    append_later(transcript, 0.3, tool_result("other"))  # another call's answer is not ours
    append_later(transcript, 2.0, tool_result("ours"))
    proc, took = run_hook({**PERMISSION, "transcript_path": str(transcript)}, url)
    assert proc.returncode == 0 and proc.stdout == "" and 2.0 <= took < 5


def test_the_call_is_recognised_with_the_defaults_claude_code_fills_in(fake_hub, tmp_path):
    url, seen, config = fake_hub
    config["hold"] = 10
    transcript = tmp_path / "transcript.jsonl"
    asked = {"questions": [{"question": "选哪个？", "header": "选", "options": [{"label": "甲"}, {"label": "乙"}]}]}
    entry = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": asked}]}}
    transcript.write_text(json.dumps(entry) + "\n")
    append_later(transcript, 1.0, tool_result("q1"))
    checked = {"questions": [{**asked["questions"][0], "multiSelect": False}]}  # as the hook gets it
    proc, took = run_hook({**QUESTION, "tool_input": checked, "transcript_path": str(transcript)}, url)
    assert proc.stdout == "" and 1.0 <= took < 4


def app_records(tmp_path, *records):
    """The desktop app's session records: (cliSessionId, title) pairs."""
    for i, (cli, title) in enumerate(records):
        d = tmp_path / "acct" / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"local_{i}.json").write_text(json.dumps({"sessionId": f"local_{i}", "cliSessionId": cli, "title": title}))
    return str(tmp_path)


def on_screen(tmp_path, records, shown):
    """Env for a hook run where the computer shows the sessions titled `shown` (raw: printed as is)."""
    script = tmp_path / "shown.py"
    script.write_text(f"import sys\nopen(sys.argv[0] + '.args', 'w').write(' '.join(sys.argv[1:]))\nprint({shown!r})\n")
    return {"PHONE_HUB_APP_SESSIONS": app_records(tmp_path, *records),
            "PHONE_HUB_SHOWN_CMD": json.dumps([sys.executable, str(script)])}


NEXT_STEP = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "mcp__ccd_turn__wrap_up",
             "tool_input": {"recap": "done", "ask": "Ship it?"}}


@pytest.mark.parametrize("payload", [PERMISSION, NEXT_STEP], ids=["permission", "next-step"])
def test_the_session_on_screen_stays_on_the_computer(fake_hub, tmp_path, payload):
    url, seen, config = fake_hub
    config["decision"], config["redirect"] = "allow", True
    env = on_screen(tmp_path, [("s1", "Phone work"), ("s2", "Other")], '["Phone work"]')
    proc, _ = run_hook(payload, url, extra_env=env)
    assert proc.stdout == "" and seen == []
    assert (tmp_path / "shown.py.args").read_text() == "180"  # mo must have used it in the last 3 min


@pytest.mark.parametrize("records,shown", [
    ([("s1", "Phone work"), ("s2", "Other")], '["Other"]'),  # another session is on screen
    ([("s1", "Phone work"), ("s2", "Phone work")], '["Phone work"]'),  # same title: which one is shown?
    ([("s2", "Other")], '["Other"]'),  # no desktop session runs this one
    ([("s1", "")], '[""]'),  # untitled
    ([("s1", "Phone work")], "not json"),  # the check failed
    ([("s1", "Phone work")], "[]"),  # away from the desk, Claude not in front, or locked
])
def test_other_sessions_still_go_to_the_phone(fake_hub, tmp_path, records, shown):
    url, seen, config = fake_hub
    config["decision"] = "allow"
    proc, _ = run_hook(PERMISSION, url, extra_env=on_screen(tmp_path, records, shown))
    assert len(seen) == 1
    assert json.loads(proc.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_a_call_session_goes_to_the_phone_even_on_screen(fake_hub, tmp_path):
    url, seen, config = fake_hub
    config["decision"] = "allow"
    env = on_screen(tmp_path, [("s1", "Phone work")], '["Phone work"]')
    proc, _ = run_hook(PERMISSION, url, extra_env={**env, "PHONE_HUB_CALL": "1"})
    assert len(seen) == 1 and seen[0]["body"]["phone_call"] is True


STOPPED = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/x", "last_assistant_message": "done"}


@pytest.mark.parametrize("shown, looked_at", [('["Phone work"]', True), ('["Other"]', False), ("[]", False)])
def test_a_finished_turn_says_its_title_and_whether_it_is_on_screen(fake_hub, tmp_path, shown, looked_at):
    url, seen, _ = fake_hub
    env = on_screen(tmp_path, [("s1", "Phone work"), ("s2", "Other")], shown)
    proc, _ = run_hook(STOPPED, url, extra_env=env)
    body = seen[0]["body"]
    assert proc.stdout == "" and seen[0]["path"] == "/hook/event"
    assert body["title"] == "Phone work" and body.get("on_screen", False) is looked_at

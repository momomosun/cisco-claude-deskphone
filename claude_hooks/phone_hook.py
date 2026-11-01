#!/usr/bin/env python3
"""Claude Code hook that forwards to phone-hub on the Mac Mini (see phone_hub.py).

Configured in ~/.claude/settings.json for PermissionRequest, Notification and Stop, and
PreToolUse of mcp__ccd_turn__wrap_up: a question left in the desktop app's next-step box is
denied in AWAY mode, telling the session to ask it with AskUserQuestion, which reaches the phone.
PermissionRequest waits for an Allow/Deny pressed on the desk phone and prints the
decision (and gives up as soon as the session's transcript shows the call answered on the
computer); for AskUserQuestion the phone's answers come back too and go in as the tool's
"answers" (updatedInput), so the question is not asked again on the computer. Requests are HMAC-signed with the shared key in the token file (the key itself is
never sent), and an answer counts only if its signature matches this request's nonce, so a
forged or replayed "allow" is ignored. Every failure (hub down, no key, bad answer, anything
unexpected) exits 0 with no output: Claude Code then shows its normal permission dialog.
With PHONE_HUB_CALL=1 (the Claude that voice_agent runs for a phone call) requests are marked
phone_call, so the hub waits for the phone's keypad or screen whatever its mode.
A permission request or next-step question from the session mo is looking at on the computer
(shown_sessions.js: Claude in front, used in the last AT_DESK_IDLE seconds) does not go to the
phone at all: the computer's own dialog or box takes it. Other sessions still go to the phone.
A Stop (the session finished its turn) carries the session's title and, when mo is looking at it,
on_screen: the hub puts the others up on the phone with GO, which brings that session to the front.
"""
import glob
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

HUB = os.environ.get("PHONE_HUB_URL", "http://192.168.1.10:8090")
TOKEN_FILE = os.path.expanduser(os.environ.get("PHONE_HUB_TOKEN_FILE", "~/.claude/phone-hub.token"))
APP_SESSIONS = os.path.expanduser(os.environ.get(
    "PHONE_HUB_APP_SESSIONS", "~/Library/Application Support/Claude/claude-code-sessions"))
SHOWN_CMD = (json.loads(os.environ["PHONE_HUB_SHOWN_CMD"]) if "PHONE_HUB_SHOWN_CMD" in os.environ else
             ["/usr/bin/osascript", "-l", "JavaScript",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "shown_sessions.js")])
AT_DESK_IDLE = 180  # seconds without keyboard or mouse after which mo counts as away from the desk
SHOWN_TIMEOUT = 4  # up to 3 s when Claude has to rebuild its accessibility tree; next-step and Stop hooks have 5 and 10 s
HUB_WAIT = 600  # phone_hub.WAIT_SECONDS; settings.json gives this hook 660 s
NEXT_STEP_REASON = ("用户现在不在电脑前（话机是离开模式），看不到下一步框里的提问。请改用 AskUserQuestion 问这个问题："
                    "它会推到用户的话机上响铃，用户在话机上点选（每题 2-4 个简短选项）。问完再调用 wrap_up，不带 ask 和 actions。")
TRANSCRIPT_TAIL = 8 << 20  # enough of the transcript's end to hold the call just made
TRANSCRIPT_POLL = 1.0
MAX_TEXT = 2000
MAX_ANSWER = 65536
FORWARDED = ("hook_event_name", "session_id", "cwd", "tool_name", "tool_input",
             "notification_type", "message", "last_assistant_message")


def sign(key, *parts):
    """Same construction as phone_hub.sign."""
    mac = hmac.new(key.encode(), digestmod=hashlib.sha256)
    for i, part in enumerate(parts):
        if i:
            mac.update(b"\n")
        mac.update(part if isinstance(part, bytes) else str(part).encode())
    return mac.hexdigest()


def canonical(answers):
    """Same as phone_hub.canonical: the answers as the signature covers them."""
    return json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def well_formed(answers):
    return isinstance(answers, dict) and all(
        isinstance(q, str) and (isinstance(a, str) or isinstance(a, list) and all(isinstance(x, str) for x in a))
        for q, a in answers.items())


def trim(value, flags):
    if isinstance(value, str):
        if len(value) <= MAX_TEXT:
            return value
        flags["truncated"] = True
        return value[:MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {k: trim(v, flags) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 50:
            flags["truncated"] = True
        return [trim(v, flags) for v in value[:50]]
    return value


def contained(part, whole):
    """part is whole, give or take dict keys whole has besides (defaults Claude Code fills in)."""
    if isinstance(part, dict) and isinstance(whole, dict):
        return all(k in whole and contained(v, whole[k]) for k, v in part.items())
    if isinstance(part, list) and isinstance(whole, list):
        return len(part) == len(whole) and all(contained(a, b) for a, b in zip(part, whole))
    return part == whole


def transcript_items(line, name, tool_input):
    """("use", id) for each call of name with tool_input in a transcript line, ("result", id) for
    each tool result."""
    if b'"tool_use"' not in line and b'"tool_result"' not in line:
        return
    try:
        entry = json.loads(line)
    except ValueError:
        return
    message = entry.get("message") if isinstance(entry, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    for c in content if isinstance(content, list) else ():
        if not isinstance(c, dict):
            continue
        if c.get("type") == "tool_use" and c.get("name") == name and (
                contained(c.get("input"), tool_input) or contained(tool_input, c.get("input"))):
            yield "use", c.get("id")
        elif c.get("type") == "tool_result":
            yield "result", c.get("tool_use_id")


def watch_transcript(payload):
    """Exit once the session's transcript shows this call answered: Claude Code takes an answer
    given on the computer but leaves this hook waiting, and the hub takes the request off the
    phone when the hook hangs up. (The input carries no tool_use_id: the call is found by tool
    name and input; those already answered when the hook starts are earlier calls.)"""
    try:
        path = payload.get("transcript_path")
        if not (isinstance(path, str) and os.path.isfile(path)):
            return
        name, tool_input = payload.get("tool_name"), payload.get("tool_input")
        waiting, rest, started = set(), b"", False
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TRANSCRIPT_TAIL))
            while True:
                lines = (rest + f.read()).split(b"\n")
                rest = lines.pop()  # a line still being written
                for line in lines:
                    for kind, ident in transcript_items(line, name, tool_input):
                        if kind == "use":
                            waiting.add(ident)
                        elif ident in waiting:
                            if started:
                                os._exit(0)
                            waiting.discard(ident)
                started = True
                time.sleep(TRANSCRIPT_POLL)
    except Exception:  # no watching: the phone keeps the request until it times out
        return


def app_title(session_id):
    """Title of the desktop session running this CLI session, or None when it has none, another
    session has the same one, or a record cannot be read (then no title is known to be unique)."""
    if not session_id:
        return None
    mine, titles = None, []
    for path in glob.glob(os.path.join(APP_SESSIONS, "*", "*", "local_*.json")):
        with open(path) as f:
            d = json.load(f)
        title = str(d.get("title") or "").strip()
        titles.append(title)
        if d.get("cliSessionId") == session_id:
            mine = title
    return mine if mine and titles.count(mine) == 1 else None


def shown_here(session_id):
    """Whether mo is looking at this session on the computer right now. Any doubt: no."""
    try:
        title = app_title(session_id)
        if not title:
            return False
        out = subprocess.run(SHOWN_CMD + [str(AT_DESK_IDLE)], capture_output=True, text=True,
                             timeout=SHOWN_TIMEOUT)
        shown = json.loads(out.stdout)
        return isinstance(shown, list) and title in shown
    except Exception:
        return False


def run():
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    with open(TOKEN_FILE) as f:
        key = f.read().strip()
    if not key:
        return
    is_permission = payload.get("hook_event_name") == "PermissionRequest"
    next_step = payload.get("hook_event_name") == "PreToolUse"  # wrap_up, by the settings.json matcher
    if next_step and not (isinstance(payload.get("tool_input"), dict) and payload["tool_input"].get("ask")):
        return  # no question in the box: nothing for the phone
    if ((is_permission or next_step) and os.environ.get("PHONE_HUB_CALL") != "1"
            and shown_here(payload.get("session_id"))):
        return  # mo is looking at this session: the computer's own dialog or box takes it
    flags = {}
    outgoing = trim({k: payload[k] for k in FORWARDED if k in payload}, flags)
    if flags:
        outgoing["truncated"] = True  # the hub must not offer Allow for something it cannot show
    if os.environ.get("PHONE_HUB_CALL") == "1":
        outgoing["phone_call"] = True
    elif payload.get("hook_event_name") == "Stop":
        try:
            outgoing["title"] = app_title(payload.get("session_id")) or ""
        except Exception:
            outgoing["title"] = ""
        if outgoing["title"] and shown_here(payload.get("session_id")):
            outgoing["on_screen"] = True
    body = json.dumps(outgoing).encode()
    if is_permission:
        threading.Thread(target=watch_transcript, args=(payload,), daemon=True).start()
    path = "/hook/permission" if is_permission else "/hook/next-step" if next_step else "/hook/event"
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    req = urllib.request.Request(HUB + path, data=body, headers={
        "Content-Type": "application/json", "X-Hub-Time": ts, "X-Hub-Nonce": nonce,
        "X-Hub-Signature": sign(key, ts, nonce, path, body)})
    with urllib.request.urlopen(req, timeout=HUB_WAIT + 15 if is_permission else 3) as resp:
        answer = json.loads(resp.read(MAX_ANSWER))
    if next_step:
        if (isinstance(answer, dict) and answer.get("redirect") is True
                and hmac.compare_digest(str(answer.get("sig", "")), sign(key, nonce, "redirect"))):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": NEXT_STEP_REASON}}))
        return
    if not is_permission or not isinstance(answer, dict):
        return
    behavior = answer.get("decision")
    if behavior not in ("allow", "deny"):
        return
    answers = answer.get("answers")
    signed = (nonce, behavior) if answers is None else (nonce, behavior, canonical(answers))
    if not hmac.compare_digest(str(answer.get("sig", "")), sign(key, *signed)):
        return
    decision = {"behavior": behavior}
    if answers is not None:  # updatedInput replaces the whole input: keep the questions as asked
        if behavior != "allow" or not well_formed(answers) or not isinstance(payload.get("tool_input"), dict):
            return
        decision["updatedInput"] = {**payload["tool_input"], "answers": answers}
    if behavior == "deny":
        decision["message"] = "Denied from the desk phone."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}))


def main():
    try:
        run()
    except Exception:  # a hook must never break or block Claude Code; no output = normal dialog
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

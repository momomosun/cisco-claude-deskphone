#!/bin/bash
# Sam's laptop: connect Claude Code to the phone-hub on his Mac mini, so permission prompts and
# questions come to the desk phone (102) in AWAY mode. Run from the unpacked folder:
#   bash install-hook.sh [Mac mini IP, default 192.168.1.30]
# Asks for the key install-hub.sh printed. Puts the hook in ~/.claude/phone-hook, the key in
# ~/.claude/phone-hub.token (600), and adds the hooks to ~/.claude/settings.json (backed up first;
# earlier phone_hook entries are replaced, nothing else is touched). Running it again is safe.
set -euo pipefail

HUB_IP="${1:-192.168.1.30}"
SRC="$(cd "$(dirname "$0")" && pwd)/hook"
DIR="$HOME/.claude/phone-hook"
TOKEN="$HOME/.claude/phone-hub.token"
SETTINGS="$HOME/.claude/settings.json"

read -rs -p "粘贴 Mac mini 上显示的密钥，然后回车： " KEY
echo
if [ -z "$KEY" ]; then
  echo "没有输入密钥。" >&2
  exit 1
fi

mkdir -p "$DIR"
cp "$SRC/phone_hook.py" "$SRC/shown_sessions.js" "$DIR/"
(umask 077; printf '%s\n' "$KEY" > "$TOKEN")
chmod 600 "$TOKEN"

COMMAND="PHONE_HUB_URL=http://$HUB_IP:8090 /usr/bin/python3 $DIR/phone_hook.py"
/usr/bin/python3 - "$SETTINGS" "$COMMAND" <<'PY'
import json, os, shutil, sys, time

path, command = sys.argv[1], sys.argv[2]
settings = {}
if os.path.exists(path):
    with open(path) as f:
        settings = json.load(f)  # a broken settings.json stops here, unchanged
    shutil.copy2(path, f"{path}.bak-phone-{time.strftime('%Y%m%d-%H%M%S')}")
hooks = settings.setdefault("hooks", {})

def ours(group):
    return any("phone_hook.py" in h.get("command", "") for h in group.get("hooks", []))

wanted = {"Notification": (None, 5), "Stop": (None, 5), "PermissionRequest": (None, 660),
          "PreToolUse": ("mcp__ccd_turn__wrap_up", 5)}
for event, (matcher, timeout) in wanted.items():
    groups = [g for g in hooks.get(event, []) if not ours(g)]
    group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher:
        group = {"matcher": matcher, **group}
    hooks[event] = groups + [group]

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
PY

CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' -X POST "http://$HUB_IP:8090/hook/event" || true)"
echo
if [ "$CODE" = "401" ]; then
  echo "装好了，Mac mini 上的 phone-hub 能连上（$HUB_IP:8090）。"
else
  echo "hook 装好了，但现在连不上 http://$HUB_IP:8090（返回 ${CODE:-无}）。"
  echo "请确认笔记本和 Mac mini 在同一个家里的网络上、Mac mini 开着、IP 没写错。"
fi
echo "重新打开 Claude Code（新开一个会话）后生效。"

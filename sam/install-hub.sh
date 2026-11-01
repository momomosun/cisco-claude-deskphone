#!/bin/bash
# Sam's Mac mini: install phone-hub for the Cisco 7970 on extension 102 (192.168.1.102), Claude Code
# only (no desk sessions, no front door). Run from the unpacked folder: bash install-hub.sh
# Installs into ~/phone-hub, starts it at login (launchd com.sam.phone-hub), prints the hub key
# for the laptop's install-hook.sh. Running it again updates the files and keeps the key.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/hub"
DIR="$HOME/phone-hub"
LABEL="com.sam.phone-hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PHONE_IP="192.168.1.102"
PORT=8090

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
if [ -z "$IP" ]; then
  echo "找不到这台 Mac mini 的局域网 IP（en0/en1 都没有地址）。请先连上家里的网络。" >&2
  exit 1
fi

mkdir -p "$DIR"
cp "$SRC"/* "$DIR"/
if [ ! -x "$DIR/.venv/bin/python" ]; then
  /usr/bin/python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --disable-pip-version-check pillow

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/phone_hub.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PHONE_IP</key><string>$PHONE_IP</string>
    <key>HUB_BASE_URL</key><string>http://$IP:$PORT</string>
    <key>HUB_PORT</key><string>$PORT</string>
    <key>HUB_DESK</key><string>0</string>
    <key>HUB_DOOR</key><string>0</string>
  </dict>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DIR/phone_hub.log</string>
  <key>StandardErrorPath</key><string>$DIR/phone_hub.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

for _ in $(seq 1 20); do
  grep -q "phone-hub on :$PORT" "$DIR/phone_hub.log" 2>/dev/null && break
  sleep 0.5
done
if ! grep -q "phone-hub on :$PORT" "$DIR/phone_hub.log" 2>/dev/null; then
  echo "phone-hub 没有起来，日志最后几行：" >&2
  tail -n 20 "$DIR/phone_hub.log" >&2 || true
  exit 1
fi

KEY="$(grep '^HUB_HOOK_PASSWORD=' "$DIR/secrets.env" | cut -d= -f2-)"
echo
echo "装好了。phone-hub 在 http://$IP:$PORT 运行，管话机 $PHONE_IP（分机 102）。"
echo
echo "1) 把这个 IP 告诉 mo：$IP"
echo "2) 在笔记本上运行 install-hook.sh 时，粘贴下面这把密钥（只给你自己的笔记本用，别发给别人）："
echo
echo "   $KEY"
echo

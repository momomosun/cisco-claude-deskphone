#!/bin/bash
# Builds ~/Applications/DeskRelay.app, a one-purpose applet that runs desk_relay.py (Mac Studio).
# desk_ax.js drives the Claude desktop app through the accessibility API, which macOS allows only
# to an app granted Accessibility (and Automation of System Events). launchd starts the applet,
# so the grant goes to it alone; granting /usr/bin/python3 would extend it to every launchd job
# run by that python. Rebuilding changes the applet's signature: grant it again afterwards.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/DeskRelay.app"

mkdir -p "$HOME/Applications"
rm -rf "$APP"
/usr/bin/osacompile -o "$APP" -e "do shell script \"cd '$HERE' && exec /usr/bin/python3 desk_relay.py >> desk_relay.log 2>&1\""
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.example.desk-relay" \
  -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist"
/usr/bin/codesign --force --sign - "$APP"
echo "built $APP"

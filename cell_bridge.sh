#!/bin/bash
# What ~/Applications/CellBridge.app runs on the Mac Mini (its ~/.baresip-cell/run.sh execs this):
# the iPhone's calls on the desk phone (cell_bridge.py). Everything here is a child of the applet, so
# the Microphone and Accessibility grants are CellBridge's alone. baresip's config and accounts
# (with extension 103's password) live in ~/.baresip-cell, outside git.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/.baresip-cell"

# The iPhone call takes the Mac's default devices, 2-channel ones only (11-02), and must not hear itself:
# two one-way BlackHole builds. Its sound goes into CallSpk 2ch and comes out of CallSpk 2ch Tap; the
# desk phone's goes into CallMic 2ch Feed and is the call's microphone, CallMic 2ch. (An aggregate of the
# two did not do: the call's voice processing splits it and picks an output of its own, 11-02.)
if ! "$CONF/bin/setaudio" "CallSpk 2ch" "CallMic 2ch"; then
  echo "$(date '+%F %T') setaudio failed: the call's sound would go to the wrong devices" >> "$HERE/cell_bridge.log"
  exit 1  # launchd starts it again (KeepAlive)
fi

# A baresip left from a run that died holds the SIP and control ports: the new one could not start.
/usr/bin/pkill -f "baresip -f $CONF" && sleep 1

/opt/homebrew/bin/baresip -f "$CONF" -c -e "/ausrc coreaudio,CallSpk 2ch Tap" -e "/auplay coreaudio,CallMic 2ch Feed" \
  > "$CONF/baresip.log" 2>&1 &

# No display sleep while this runs: on a dark (and then locked) screen the call banner cannot be pressed.
/usr/bin/caffeinate -d -w $$ &

cd "$HERE" && exec /usr/bin/python3 cell_bridge.py

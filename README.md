# cisco-claude-deskphone

Turn an old Cisco CP-7970G desk phone into a console for Claude Code: approve tool calls and answer
Claude's questions on the phone, get a ring when a desktop session finishes (GO brings it to the front),
browse and talk to desktop sessions, dial 300 to talk to Claude by voice, and see who is at the front door.

**This is one home setup published as-is, not a product.** It assumes macOS hosts (a Mac Mini running
FreeSWITCH, TFTP and the hub; a Mac with the Claude desktop app), a 7970G on Cisco's SIP firmware, and a
Chinese UI. Addresses, MACs, names, paths and the time zone are examples (192.168.1.x, `SEP001122334455`,
"Sam", `/Users/you`, Australia); before deploying, change them in `templates/` (phone and FreeSWITCH
configs), `render.py`, the `com.example.*.plist` files, `claude_hooks/phone_call_settings.json` (absolute hook
path), `claude_hooks/phone_hook.py`, `phone_hub.py` (`BASE_URL`, `HOOK_NET`), `desk_gateway.py` (`RELAY_URL`),
`desk_relay.py` (`CLIENTS`), `tftpd.py`, `ha_door.py`, `phone_notify.py`, `phone_screen.py` and `sam/`
(`grep -rn 192.168.1. .` finds the addresses). Passwords and keys live in an untracked `secrets.env`
(see `render.py`). **Cisco firmware is not included**: you need your own copy of the SIP 9.2(1) load.
The orange asterisk is drawn for this repo; Claude is a trademark of Anthropic, and this project is not
affiliated with Anthropic or Cisco.

Tests: `python -m pytest -q` (needs Pillow; the voice tests skip without MLX Whisper).

The notes below are the original operator's notes.

## Provisioning

Home provisioning for a Cisco CP-7970G (and later 7962G) on SIP, to pair with Home Assistant.

- `tftpd.py` — read-only TFTP server (stdlib Python), runs on the Mac Mini 192.168.1.10 via
  launchd `com.example.cisco-tftpd` (`~/Library/LaunchAgents`), serving `~/cisco-phone/tftpboot`, log `~/cisco-phone/tftpd.log`.
  Non-root bind to udp/69 works on macOS; the launchd python needs the macOS *Local Network* permission
  (granted on the Mini's screen 2026-10-27) or replies fail with `No route to host`.
- `tftpboot/SEP001122334455.cnf.xml` — 7970G config. Firmware `.sbn/.loads` are not in git
  (Cisco SIP 9.2(1) from firewall.cx, zip sha256 3fd6de88…81293cb, kept in `downloads/`).
- Router (iStoreOS 192.168.1.1): DHCP hosts `cisco7970` (00:11:22:33:44:55 → .101) and `ciscosam`
  (00:AA:BB:CC:DD:EE → .102) tagged `ciscophone`, option 150 → 192.168.1.10. Backups
  `/etc/config/dhcp.bak-<date>-<label>`. tftpd answers only the
  addresses in `TFTP_ALLOWED_NET` (comma-separated, set in `com.example.cisco-tftpd.plist`): add a new phone there.
- `tftpboot/SEP00AABBCCDDEE.cnf.xml` — Sam's 7970 (ext 102). It came with SCCP 9.3(1)SR3 (seen in its CDP);
  `loadInformation` SIP70.9-2-1S makes it flash itself to the same SIP load as 101. No Claude console on it.
- Deploy: `rsync -a --exclude downloads --exclude router-backup --exclude .git --exclude tftpd.log ./ you@192.168.1.10:cisco-phone/`
  then `launchctl kickstart -k gui/$(id -u)/com.example.cisco-tftpd` on the Mini.
- PBX: FreeSWITCH 1.11 (Homebrew) running natively on the Mini via launchd `com.example.freeswitch`,
  config `build/freeswitch/freeswitch.xml` rendered from `templates/freeswitch.xml.tmpl` by `render.py`
  (passwords in the Mini's `secrets.env`, chmod 600, never in git). Extensions: 101 = 7970G (Claude console), 102 = Sam's 7970 (SEP00AABBCCDDEE, .102);
  `*43` = echo test. The SIP profiles have `auto-restart` off: on 10-29 a moment's address change made
  FreeSWITCH restart them, the rebind to .10 failed and SIP stayed down (`sofia status` showed 0 profiles)
  until `sofia profile internal start` / `sofia profile ha start`. fs_cli: `fs_cli -P 8022 -p <ESL_PASSWORD>` (8021 is taken by a root process on the Mini).
  Asterisk in Docker Desktop did not work: Docker rewrites the SIP source to 192.168.65.1 and the 7970 only
  accepts SIP from its configured server IP.
- **macOS Local Network permission is required** for anything launchd starts on the Mini that sends to LAN
  devices (python3 for tftpd, freeswitch). Without it, sends fail with `No route to host` except plain replies
  to the peer's source port, so the 7970 (sends from an ephemeral port, expects replies on 5060) never registers.
  Granted on the Mini under System Settings > Privacy & Security > Local Network (2026-10-27).
- Remote phone restart: `fs_cli -P 8022 -p <ESL_PASSWORD> -x "sofia profile internal check_sync 101@192.168.1.10"`
  (Cisco service-control NOTIFY; needs the phone to be registered, since it carries its registration Call-ID).
- Tests (Studio): `.venv/bin/python -m pytest -q test_*.py`; on the Mini the voice tests run in `.venv-voice`
  (see below) and include a real Whisper check.

## Claude Code console on the phone (phone-hub)

- `phone_hub.py` (Mini, launchd `com.example.phone-hub`, `.venv` with Pillow, port 8090, log `phone_hub.log`).
  Claude Code hooks in `~/.claude/settings.json` on the Studio (PermissionRequest timeout 660 s; Notification,
  Stop and PreToolUse of `mcp__ccd_turn__wrap_up` 5 s) run `claude_hooks/phone_hook.py`, which signs requests (HMAC) with the key in
  `~/.claude/phone-hub.token` (= `HUB_HOOK_PASSWORD` in the Mini's `secrets.env`) and accepts only signed answers.
- Mode AWAY: permission requests are pushed to the 7970 (`/CGI/Execute`, basic auth `claude` / a one-time
  password the phone checks through `authenticationURL` = `/phone/auth`) and wait up to 10 minutes for
  Allow/Deny, while Claude Code shows its normal dialog too: the first answer counts. Claude Code takes an
  answer on the computer at once but leaves the hook waiting, so the hook watches the session's transcript
  (found by tool name and input: the hook input has no tool_use_id) and exits when the call has its result;
  the hub sees the connection close and takes the request off the phone (gone at the phone's next refresh).
  Mode DESK: hooks return immediately. Toggle with the phone's services button > Mode, or the Mode softkey on
  the panel.
- The session mo is looking at stays on the computer in either mode: a permission request or next-step
  question from it never reaches the hub when `claude_hooks/shown_sessions.js` (JXA, ~0.7 s) finds Claude
  in front, the screen unlocked, input in the last 180 s, and a Claude window whose web area is named
  "<that session's title> - Claude Code". The title comes from the app's records
  (`~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json`, `cliSessionId` = the hook's
  `session_id`) and must be unique there; any doubt sends the request to the phone as before. Calls
  (`PHONE_HUB_CALL=1`) always go to the phone.
- In AWAY mode a question a session leaves in the desktop app's next-step box (`wrap_up` with `ask`) is
  refused by the PreToolUse hook (`/hook/next-step`), whose reason tells the session to ask it with
  AskUserQuestion instead, which comes to the phone; the box is for the desk.
- Questions (AskUserQuestion) come to the phone the same way: one at a time, options numbered; touch an option
  or press its number (several allowed: toggle them, then Done). The answers go back through the hook, signed by
  the hub over the answers, as the tool's `updatedInput` = the questions as asked plus `answers` (question text
  -> label, or labels): it replaces the whole input, so `answers` alone fails Claude Code's schema check. Only
  1-4 plain choice questions of 2-4 options; with the desk relay a **GO** softkey opens the asking session on
  the Studio (as for a finished session) and leaves the question up, its footer saying what GO did; text/number questions, calls and anything cut short stay on the
  computer, and Allow/Deny never answer a question.
- While an approval waits on the phone it rings: `Play:ClaudeRing.raw` (fetched from the Mini's TFTP;
  `make_ring.py` writes it in Cisco's ring format) one second after each load of the approval page, i.e. the push
  and then the page's own refresh every 10 s, in the request's first 30 s only (then it waits quietly), and not
  once answered. A ring that reaches the phone while it is
  still loading a page stops the page from showing ("File Not Found"), hence the wait. Calls do not ring: the
  call reads requests out.
- Services button = menu (panel / pending / mode); idle screen (60 s) = status panel, refreshed every 20 s;
  the panel shows a pending approval instead of the status whenever one is waiting.
- The phone's Exit goes back, from its cache, to the last page that had not expired when it was loaded, and a
  waiting request's page reloads every 10 s: so every page of a request (the approval or question, the next
  question, "Answered"/"Allowed", "no longer open") is sent with `Expires` long past and stays out of the
  history; otherwise Exit after answering showed the question again. (Community notes say 0 or -1 may not
  count: it takes a real HTTP date.)
- Text is drawn as PNG (STHeiti Medium 15 px, white on black, chosen on the real phone) because the 7970
  font has no CJK. Cisco XML must be served as `text/xml` or the phone ignores it.
- An orange asterisk (`claude_icon.png`, a plain six-ray mark in #D97757 drawn for this repo, not Anthropic artwork)
  leads every picture page's title; menus are CiscoIPPhoneIconFileMenus whose title icon the 7970 puts at the
  right end of the title bar. The globe at the left of the status bar and of menu titles is the phone's own
  services mark: no service can change it.
- Seeing the phone's screen: on the Mini, `python3 phone_screen.py [/phone/page] out.bmp` opens a phone-hub page
  on the phone (optional) and saves a 320x234 screenshot, through the hub's loopback-only, signed `/local/show`
  (phone-hub pages only) and `/local/screenshot`; the phone checks a one-time password as for pushes.
  `/local/ring` rings once. The phone's own console log (clock one hour behind local time) is at
  `http://192.168.1.101/CGI/Java/Serviceability?adapter=device.statistics.consolelog` (files `/FS/cache/logN`).
- Most tools are pre-allowed in this Mac's permissions (`Bash(*)` etc.), so approvals mostly appear for
  MCP tools and prompts no mode auto-approves.

## Talk to Claude: dial 300 (voice-agent)

- `voice_agent.py` (Mini, launchd `com.example.voice-agent`, log `voice_agent.log`) listens on 127.0.0.1:8085;
  FreeSWITCH sends 300 there with `socket 127.0.0.1:8085 async full` and the agent drives the call (`esl.py`).
  Only the 7970 may call 300: user 101's directory entry sets `claude_caller=true` (applied once the caller
  has authenticated, so 200/HA or 102 cannot fake it); the dialplan and the agent both check it.
  Venv `.venv-voice` = Homebrew Python 3.12 + mlx, numpy, scipy, numba, tiktoken, ... and `mlx-whisper==0.4.3`
  installed `--no-deps` (its torch dependency is only for model conversion).
- Each turn: beep, record until 2 s of silence / 30 s / `#`, Whisper large-v3-turbo on the GPU (MLX, ~0.9 s per
  sentence; `models/whisper-large-v3-turbo` copied from the Studio's Hugging Face cache, not in git), then
  `claude -p` in `~/phone-claude` (one session per call: `--session-id`, then `--resume`) with
  `claude_hooks/phone_call_settings.json` and `--strict-mcp-config`, then `say -v Tingting` at 8 kHz (`#` skips).
  A short goodbye (再见/拜拜/挂了...) or hanging up ends the call; hanging up also stops Claude.
- The call's Claude must ask before Bash/Write/Edit, may not read the signing key, `secrets.env` or its token, and
  sandboxed Bash does not skip the question. Its hook marks requests `phone_call` and talks to the hub over
  127.0.0.1; the hub then waits for the phone whatever the mode, and the agent reads the request out. The keypad
  can allow (1) only a one-line command of at most 60 characters, read out whole; anything else is allowed on
  the screen, and 2 always denies. First answer wins. Write/Edit cannot be shown, so they are denied.
  `say` text is stripped of `[[...]]` embedded commands, which could otherwise silence part of a command.
- Hanging up or a 300 s turn kills Claude's whole process group (an approved command does not run on). A call
  that makes no progress for 240 s (hung Whisper, a FreeSWITCH completion that never comes) makes the agent
  exit, and launchd starts a fresh one.
- Login: if `claude.token` (output of `claude setup-token`, chmod 600, not in git) exists, the call's Claude
  gets it as `CLAUDE_CODE_OAUTH_TOKEN`; otherwise the Mini's own login is used.
- **`ProcessType` = `Interactive` in both launchd plists matters.** As a default (Standard) launchd job,
  macOS coalesced FreeSWITCH's timers (a 0.85 ms sleep took ~5 ms): its soft timer ticked every 48 ms instead
  of 20 and every sound FreeSWITCH played ran 2.4x slow. Check with `fs_cli ... -x "timer_test 20 100 soft"`
  (should print `Avg: 20.000ms`). Bridged calls (echo test, HA) did not show it: they only relay packets.
- Restart the agent only between calls (a restart drops the call in progress):
  `[ -z "$(lsof -nP -iTCP:8085 -sTCP:ESTABLISHED)" ] && launchctl kickstart -k gui/$(id -u)/com.example.voice-agent`
- Test without the phone: `fs_cli ... -x "load mod_loopback"`, then
  `originate {claude_caller=true,loopback_export=claude_caller}loopback/300/default playback:silence_stream://9000,playback:/tmp/q.wav,playback:silence_stream://20000 inline`.
  The calling side must keep streaming audio: a `sleep` there starves the loopback media clock.

## Messages from other programs (phone_notify.py)

Any program on the LAN can put a message on 101: `phone_notify.py` (stdlib only, copy it anywhere)
signs a POST to the hub's `/notify` with the hub key (`~/.claude/phone-hub.token`, or on the Mini
`HUB_HOOK_PASSWORD` in `secrets.env`; `PHONE_HUB_TOKEN_FILE` / `PHONE_HUB_URL` override). Exit 0 once
the hub has it, 1 otherwise. From Python: `phone_notify.notify(title, text, warning=True)`.

```bash
phone_notify.py --warn "crypto bot" "仓位亏损超过 5%"   # up on the phone now, rings once, stays until OK
phone_notify.py "备份" "今晚备份完成"                    # waits under Messages in the services menu
echo "long text" | phone_notify.py "title" -
```

A warning not yet OK'd takes the idle panel's place for up to an hour (otherwise the panel, which comes
back after 60 s without a key, would cover it); each ring is logged (`message <id>: ring ok`). Warnings show in either mode; titles are cut to 60 characters, text to 1000. The hub keeps the last
30 messages in memory (gone on a hub restart); Messages opens the newest, Older/Newer step through.
Verified on the phone 10-29 (screenshot). Home Assistant would need its own signing step (a shell
command calling this script on the Air, with a copy of the key) — not set up.

## Sam's phone with his own Claude Code (sam/)

Sam's 7970 (102) shows his own Claude Code, through a second phone-hub on his Mac mini (.30,
tailnet `sam-macmini`): `HUB_DESK=0 HUB_DOOR=0` (Claude Code only), `PHONE_IP=192.168.1.102`, its own
key in its own `secrets.env`. `sam/install-hub.sh` (Mac mini, launchd `com.sam.phone-hub`) and
`sam/install-hook.sh` (his laptop: hook, key, settings.json merge with a backup), guide in
`sam/README.md`. Package:

```bash
rm -rf dist/sam-phone-hub* && mkdir -p dist/sam-phone-hub/hub dist/sam-phone-hub/hook && cp phone_hub.py phone_render.py render.py hubauth.py desk_gateway.py ha_door.py claude_icon.png dist/sam-phone-hub/hub/ && cp claude_hooks/phone_hook.py claude_hooks/shown_sessions.js dist/sam-phone-hub/hook/ && cp sam/install-hub.sh sam/install-hook.sh sam/README.md dist/sam-phone-hub/ && (cd dist && zip -qr -X sam-phone-hub.zip sam-phone-hub)
```

Still to do on our side once his hub runs: point 102's `authenticationURL` / `servicesURL` / `idleURL`
(template `SEP00AABBCCDDEE.cnf.xml.tmpl`) at `http://<his Mac mini>:8090/phone/...` and resync 102.
Ring tones still come from our TFTP (102 is on its allow-list).

## Front door on the phone (ha_door)

- When the Blink doorbell (`binary_sensor.front_door_motion`, `camera.front_door`) or the front door camera
  (`..._2`) sees motion, phone-hub pushes that camera's picture to the phone (a wide CiscoIPPhoneImageFile,
  256-colour PNG with a caption) and plays `DoorChime.raw` (ding-dong, `make_ring.py`) once the picture is up;
  one alert a minute at most. Services > Front door looks now (Update, Other = the other camera).
- `ha_door.py` polls HA's REST API every 3 s with a long-lived access token made in HA for the hub, stored in
  the Mini's `~/cisco-phone/ha.token` (600, not in git); without it nothing is asked. The picture is a frame
  2 s into the camera's last clip: with clips on the Sync Module's USB stick, Blink's cloud thumbnail
  (`/api/camera_proxy/<camera>`) is only renewed on request and was 11 days old. The watch calls
  `blink.save_video` to write the clip to HA's media folder (`/media/<camera>.mp4` in the container, served at
  `/media/local/...` only with a login, unlike `/local`), fetches it and cuts the frame with ffmpeg
  (`/opt/homebrew/bin/ffmpeg` on the Mini); the thumbnail is the fallback.
  HA's Blink integration (a copy of HA's own in `custom_components/blink`, cloud polling through blinkpy, clips
  kept on the Sync Module's USB) refreshes from Blink's cloud only every 300 s, fixed in its coordinator: the
  `scan_interval` option only throttles blinkpy, and the coordinator forces past it. So the watch also calls
  `homeassistant.update_entity` on the doorbell's motion sensor every 60 s, and someone at the door shows up
  within about a minute. Try it from the Mini: signed `/local/door` (loopback only) shows the doorbell's
  picture and chimes.

## Desktop sessions on the phone (desk relay)

- Services > Desk sessions lists the Studio's Claude desktop sessions that have a live process, newest first
  (【忙】 = busy). Touching a row opens it (the list is a CiscoIPPhoneGraphicFileMenu: one TouchArea per row on
  screen), as do the Up/Down and Open softkeys; the navigation disc only moves in the phone's own text menus,
  whose font has no Chinese. Line keys 2 and 3 (right of the screen) page what is on screen: the list a screen
  at a time, a session's conversation as its Up/Down do; an approval or question they leave on screen, and
  anywhere else they page the list. They are cnf.xml line keys with featureID 20 (Service URI, labelled
  PgUp/PgDn on the phone's own screen) opening `/phone/desk/pageup|pagedown`, so phone-hub remembers the page
  the phone loaded last (`Hub.screen`); changing the keys needs a re-render and a `check_sync` restart. The
  phone puts a globe beside every Service URI key, so the list, a session's page, the status panel and the
  approval and question pages are `WindowMode="Wide"` pages (320 px) whose pictures cover that column and draw
  a telephone beside key 1 and an arrow beside keys 2 and 3 (`phone_render.KEY_Y`, measured on the phone);
  touching an arrow on the list or a session's page pages too. The menus and text pages are drawn by the
  phone and keep its globes. Open shows the session's last few turns a page at a time, from the last thing said
  to it; Up/Down (softkeys, or touching the upper/lower half) page back and on. Talk (= dial 300) then talks to
  that session for 10 minutes.
  Otherwise 300 talks to the session titled **电话** (keep it open in its own window); with no such session or
  no relay, the Mini's Claude answers as before.
- `desk_relay.py` on the Studio (launchd `com.example.desk-relay`, port 8091, log `desk_relay.log`) answers only the
  Mini and localhost, signed like the hooks (`hubauth.py`, same key). It reads Claude Code's own files
  (`~/.claude/sessions/<pid>.json`, transcripts under `~/.claude/projects`) and sends with `desk_ax.js`: through
  the accessibility API it fills the session's prompt box and presses Send (no keystrokes: Parallels steals the
  focus). The header button "<title>, rename session" proves which session a window shows; busy sessions and
  unsent drafts are refused; the session's `claude://` link is opened only when no window shows it, since that
  also switches the main window. The words arrive prefixed 「（电话语音）」; hanging up leaves the session working.
  Answers are signed for the request they answer (`X-Hub-Answer`), so the hub cannot be handed another session.
- **Sessions are told apart by title only**: the accessibility tree carries no session id. The relay refuses
  a title that is empty or that any other session shares, running or not: the desktop app keeps a record of
  every session in `~/Library/Application Support/Claude/claude-code-sessions/*/*/local_<id>.json` (title,
  archived flag, ...). It also checks afterwards that the words show up in the target's transcript, and the
  phone says so when they do not. A send waits at most 10 s behind another one, so nothing arrives late.
- **A session that finishes its turn comes up on 101** (the Stop hook, `claude_hooks/phone_hook.py`): title
  「完成：<session title>」 and its last reply (Markdown `**`/backticks left out), softkeys **GO** and **OK**.
  It rings once in AWAY mode and comes up silently in DESK mode; it holds the panel like a warning until OK or
  GO (an hour at most), a newer finish of the same session replaces it, and it waits behind an approval on
  screen. Not for the session mo is looking at on the computer (the hook's `shown_here` check sends
  `on_screen`) nor for a phone call (marked `phone_call`, or a desk session 300 is talking to). GO waits at
  most 2 s behind a voice send and otherwise says it is busy: opening a link switches the main window. **GO** asks the relay (`/focus`) to open that session's `claude://` link
  (`desk_focus.js`), which raises the desktop app and shows it, then the phone goes back to the panel. With the
  Studio locked nothing is opened and the notice comes back saying 「电脑锁屏了，没打开」 (a text page pushed to
  the phone does not show, so the note goes in the picture); press GO again once unlocked. Only a hub with the
  desk relay does this (not Sam's).
- phone-hub keeps the list cursor and the selection (`desk_gateway.py`) and passes the voice agent's
  `/local/desk/*` calls on; a desk session in a call sends its permission requests to the phone.
- **The Studio must be unlocked**: a locked screen hides every window from the accessibility API, so the relay
  refuses and the phone says so. A window covered by others counts as hidden to the app, which then shows each
  change one accessibility action late; `desk_ax.js` scrolls the header button into view first to catch up.
- Permissions: the relay runs inside `~/Applications/DeskRelay.app` (`desk_relay_app.sh` builds it, ad hoc signed,
  `com.example.desk-relay`), which alone holds Accessibility and Automation of System Events; granting
  `/usr/bin/python3` would extend that to every launchd job run by it. After granting, restart the service (the
  running process keeps the earlier refusal); after a rebuild, grant again.
  Restart: `launchctl kickstart -k gui/$(id -u)/com.example.desk-relay`.

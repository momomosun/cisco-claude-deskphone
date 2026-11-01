// osascript -l JavaScript shown_sessions.js MAX_IDLE — print, as a JSON list, the titles of the
// Claude desktop sessions mo is looking at right now: the session each Claude window shows,
// provided Claude is the frontmost app, the screen is not locked and the keyboard or mouse was
// used in the last MAX_IDLE seconds. Otherwise prints []. phone_hook.py then leaves a request
// from such a session to the computer's own dialog instead of the desk phone.
//
// A window's web area is named "<session title> - Claude Code" (the focused pane's title in a
// split view), and that name is already part of every inner element's specifier, so one
// entireContents() per window (about 0.7 s) is enough: no element is asked anything else.
// Claude (Electron) builds that inner tree only while asked to: with AXManualAccessibility off a
// window shows a dozen unnamed groups and no session at all (seen 10-30), so it is switched on
// first; the tree takes a second or so to come back, so an empty read is tried again for up to 2 s.

ObjC.import("CoreGraphics");

const SUFFIX = " - Claude Code";
const NAMED = /\.uiElements\.byName\(("(?:[^"\\]|\\.)*")\)/;

function run(argv) {
  const maxIdle = Number(argv[0]);
  const session = ObjC.deepUnwrap(ObjC.castRefToObject($.CGSessionCopyCurrentDictionary()));
  if (session.CGSSessionScreenIsLocked === true) return "[]";
  // HID system state (1), any input event (~0)
  if (!(maxIdle >= 0) || $.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF) > maxIdle) return "[]";
  const front = Application("System Events").applicationProcesses.whose({frontmost: true})();
  if (!front.length || front[0].name() !== "Claude") return "[]";
  try { front[0].attributes.byName("AXManualAccessibility").value = true; } catch (e) {}
  let titles = shown(front[0]);
  for (let tries = 0; tries < 4 && !titles.length; tries++) { delay(0.5); titles = shown(front[0]); }
  return JSON.stringify(titles);
}

function shown(proc) {
  const titles = [];
  for (const w of proc.windows()) {
    for (const el of w.entireContents()) {
      const m = NAMED.exec(Automation.getDisplayString(el));
      if (!m) continue;
      const name = JSON.parse(m[1]);
      if (name.endsWith(SUFFIX)) titles.push(name.slice(0, -SUFFIX.length).trim());
      break;  // the outermost named element is the web area; the rest lie inside it
    }
  }
  return titles;
}

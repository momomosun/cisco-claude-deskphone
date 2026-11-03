// desk_ax.js TITLE TEXT HOST_ID HINTS — put TEXT in the prompt box of the Claude desktop session
// titled TITLE and press its Send button, through the macOS accessibility API (no keystrokes, no
// focus change). Prints on the first line "SENT", "ABORT: <reason>" (having sent nothing, and
// having taken its text back out of the box) or "UNKNOWN: <error>" (pressing Send failed half
// way), and after SENT the paths it used, which desk_relay.py passes back as HINTS next time.
//
// The session's header has a button described "<title>, rename session" ("<title>，重命名会话" in the
// Chinese app, 11-02; LABELS has both) only where the session is shown, so finding it proves where
// the text goes; it is checked again just before Send.
// The session is looked for in every window first (it may sit in its own pop-out window): its
// claude:// link (HOST_ID) is opened only when it is nowhere on screen, because opening the link
// also switches the main window. A locked screen hides every window from the accessibility API,
// so nothing is tried then.
//
// Speed: asking System Events for each element's children and role costs one Apple Event per
// call (a 680-element window took 30 s). entireContents() returns every element in one event,
// and each element's specifier already names its class and path, so only candidate buttons and
// text areas are asked for their description; with HINTS, the elements at the remembered paths
// are checked first and nothing is scanned at all when they still fit.

ObjC.import("CoreGraphics");

// What the app calls its controls, English and Chinese.
const LABELS = { rename: [", rename session", "，重命名会话"], prompt: ["Prompt", "提示词"], send: ["Send", "发送"] };

function attr(el, name) {  // a failed read gives "", which matches no description we look for
  try { const v = el.attributes.byName(name).value(); return v == null ? "" : String(v); } catch (e) { return ""; }
}
function value(el) {  // the box's text: a failed read throws, so it never passes for an empty box
  const v = el.attributes.byName("AXValue").value();
  return v == null ? "" : String(v);
}

function locked() {
  const session = ObjC.deepUnwrap(ObjC.castRefToObject($.CGSessionCopyCurrentDictionary()));
  return session.CGSSessionScreenIsLocked === true;
}

// ['applicationProcesses["Claude"]', 'windows["Claude"]', 'groups[0]', ...] from the specifier
function steps(el) {
  const s = Automation.getDisplayString(el);
  const out = [], re = /\.(\w+)\.(?:at\((\d+)\)|byName\(("(?:[^"\\]|\\.)*")\))/g;
  let m;
  while ((m = re.exec(s))) out.push(m[1] + "[" + (m[2] !== undefined ? m[2] : m[3]) + "]");
  return out;
}

function build(path) {  // a specifier back from steps(); no Apple Event until it is used
  try {
    let spec = Application("System Events");
    for (const step of path) {
      const m = /^(\w+)\[(.*)\]$/.exec(step);
      spec = /^\d+$/.test(m[2]) ? spec[m[1]].at(Number(m[2])) : spec[m[1]].byName(JSON.parse(m[2]));
    }
    return spec;
  } catch (e) { return null; }
}

// A window covered by other windows counts as hidden to the app, and the accessibility API then
// shows each change only when the next action comes in: the box still holds the text just sent,
// and the Send button that appears with the text stays out of sight. flush() performs a harmless
// action (scrolling the always-visible header button into view) before anything is read.
const SEND_WAIT = 6;

function shared(a, b) { let n = 0; while (n < a.length && n < b.length && a[n] === b[n]) n++; return n; }
function kind(x) { const s = x.path[x.path.length - 1] || ""; return s.slice(0, s.indexOf("[")); }
function contents(win) { return win.entireContents().map(el => ({ el, path: steps(el) })); }
function hinted(path, test) {
  const el = path && build(path);
  return el && test(el) ? { el, path } : null;
}
function flush(anchor) {  // a failure throws: what is read next could not be trusted
  anchor.el.actions.byName("AXScrollToVisible").perform();
}
// After an abort: take our text, and only our text, back out of the box, while the window still
// shows the session. True when our text is no longer there.
function clear(box, anchor, isAnchor, text) {
  try {
    for (let tries = 0; tries < 3; tries++) {
      flush(anchor);
      const now = value(box.el).trim();
      if (!now.includes(text.trim())) return true;
      if (now !== text.trim() || !isAnchor(anchor.el)) return false;  // someone's words, or another session
      box.el.attributes.byName("AXValue").value = "";
      delay(0.3);
    }
    flush(anchor);
    return !value(box.el).includes(text.trim());
  } catch (e) { return false; }
}

function run(argv) {
  const [title, text, host, hintText] = argv;
  if (!title || !text || !/^local_[0-9a-f-]{36}$/.test(host || "")) return "ABORT: usage: desk_ax.js TITLE TEXT HOST_ID [HINTS]";
  if (locked()) return "ABORT: the screen is locked";
  let hints = {};
  try { hints = JSON.parse(hintText || "{}") || {}; } catch (e) {}
  const proc = Application("System Events").processes.byName("Claude");
  try { proc.attributes.byName("AXManualAccessibility").value = true; } catch (e) {}  // Electron builds its tree
  const anchorTexts = LABELS.rename.map(suffix => title + suffix);
  const isAnchor = el => anchorTexts.includes(attr(el, "AXDescription"));

  let anchor = hinted(hints.anchor, isAnchor), all = null;
  const scan = () => {
    for (const win of proc.windows()) {
      const found = contents(win);
      const a = found.find(x => kind(x) === "buttons" && isAnchor(x.el));
      if (a) { all = found; return a; }
    }
    return null;
  };
  if (!anchor) anchor = scan();
  if (!anchor) {  // not on screen anywhere: open it (this switches the main window)
    const app = Application.currentApplication();
    app.includeStandardAdditions = true;
    app.doShellScript("/usr/bin/open -g 'claude://claude.ai/epitaxy/" + host + "'");
    for (let tries = 0; tries < 6 && !anchor; tries++) { delay(0.5); anchor = scan(); }
  }
  if (!anchor) return "ABORT: the window is not showing that session";

  const isBox = el => LABELS.prompt.includes(attr(el, "AXDescription"));
  let box = hints.anchor && shared(hints.anchor, anchor.path) === anchor.path.length ? hinted(hints.box, isBox) : null;
  if (!box) {
    const pane = all || contents(build(anchor.path.slice(0, 2)));
    const boxes = pane.filter(x => kind(x) === "textAreas" && isBox(x.el));
    if (!boxes.length) return "ABORT: no prompt box";
    box = boxes.reduce((best, x) => shared(x.path, anchor.path) > shared(best.path, anchor.path) ? x : best);
  }
  flush(anchor);
  if (!isAnchor(anchor.el)) return "ABORT: the window switched away";
  if (value(box.el).trim()) return "ABORT: the prompt box holds an unsent draft";
  const abort = reason => "ABORT: " + reason +
    (clear(box, anchor, isAnchor, text) ? "" : " (and the text may still be in the box)");
  let pressing = false;
  try {
    box.el.attributes.byName("AXValue").value = text;
    // the Send button appears once the box holds text: the remembered one, or look near the box
    const isSend = el => LABELS.send.includes(attr(el, "AXDescription"));
    const near = x => kind(x) === "buttons" && shared(x.path, box.path) >= box.path.length - 6;
    let send = null;
    for (const start = Date.now(); !send && Date.now() - start < SEND_WAIT * 1000;) {
      delay(0.3);
      flush(anchor);
      send = hinted(hints.send, isSend);
      if (!send || !near(send)) send = contents(build(box.path.slice(0, 2))).filter(near).find(x => isSend(x.el)) || null;
    }
    if (!isAnchor(anchor.el)) return abort("the window switched away");
    if (!send) return abort("no Send button");
    if (value(box.el).trim() !== text.trim()) return abort("the text did not stick");
    if (locked()) return abort("the screen is locked");
    pressing = true;
    send.el.actions.byName("AXPress").perform();
    return "SENT\n" + JSON.stringify({ anchor: anchor.path, box: box.path, send: send.path });
  } catch (e) {  // the press itself may have gone through: the relay looks for the words
    return pressing ? "UNKNOWN: " + e : abort(String(e));
  }
}

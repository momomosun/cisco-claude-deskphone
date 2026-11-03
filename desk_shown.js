// desk_shown.js — the titles of the sessions the Claude desktop app shows now, one per line, the
// main window's first. Each shown session is a web area named "<title> - Claude Code", and that
// name is part of every element specifier under it, so one entireContents() per window finds them
// without asking any element anything (asking each button for its description took 8 s). Prints
// nothing when the screen is locked or the app is not running.
ObjC.import("CoreGraphics");

const AREA = /\.uiElements\.byName\("((?:[^"\\]|\\.)*) - Claude Code"\)/;

function locked() {
  const d = ObjC.deepUnwrap($.CGSessionCopyCurrentDictionary());
  return !d || d.CGSSessionScreenIsLocked === true;
}

function run() {
  if (locked()) return "";
  const proc = Application("System Events").processes.byName("Claude");
  try { proc.attributes.byName("AXManualAccessibility").value = true; } catch (e) { return ""; }
  const main = [], others = [];
  for (const win of proc.windows()) {
    let isMain = false;
    try { isMain = win.attributes.byName("AXMain").value() === true; } catch (e) {}
    const titles = isMain ? main : others;
    for (const el of win.entireContents()) {
      const m = AREA.exec(Automation.getDisplayString(el));
      let title = null;
      if (m) try { title = JSON.parse('"' + m[1] + '"'); } catch (e) { title = m[1]; }  // an odd escape: as written
      if (title && !titles.includes(title)) titles.push(title);
    }
  }
  return main.concat(others.filter(t => !main.includes(t))).join("\n");
}

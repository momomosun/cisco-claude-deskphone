// osascript -l JavaScript cell_confirm.js call|hangup|dump|find|cancel — what cell_bridge.py does on screen.
// Placing a call through the iPhone (open tel:...) puts a banner in Notification Center ("<name>,
// 点按以呼叫" with a 呼叫 button) that waits for a click; a call in progress has its own controls there.
//   call    press that banner's 呼叫 button if it shows now: prints CALLED or NO_BANNER (cell_bridge.py
//           asks again until it shows, and stops asking once the desk phone hangs up)
//   hangup  press the end button of the call in progress: prints ENDED or NO_BUTTON
//   cancel  close the call banners still waiting (取消): before a new number is dialled, so a banner
//           left from an earlier call is not the one pressed; and when the desk phone hung up first
//   dump    the buttons in those places, to find a control not known yet (buttons only: the texts
//           of other notifications stay out of the log)
// The banner sits a dozen groups deep. Needs Accessibility for the app that runs it (CellBridge).

const END_BUTTONS = ["结束", "挂断", "结束通话", "End", "End Call"];

function walk(el, depth, visit) {
  if (depth > 25) return null;
  let kids = [];
  try { kids = el.uiElements(); } catch (e) { return null; }
  for (const k of kids) {
    const hit = visit(k, kids);
    if (hit) return hit;
    const deeper = walk(k, depth + 1, visit);
    if (deeper) return deeper;
  }
  return null;
}

function describe(el) {
  let role = "", desc = "";
  try { role = el.role(); } catch (e) {}
  try { desc = el.description() || ""; } catch (e) {}
  return [role, desc];
}

// Where a call's banner and controls may be: the banner is Notification Center's; the controls of a
// call in progress were not there (11-02), so the call's window and the menu bar are searched too.
const PLACES = ["NotificationCenter", "FaceTime", "ControlCenter", "MenuBarAgent", "SystemUIServer"];

function windows() {
  const out = [];
  for (const name of PLACES) {
    try { for (const w of Application("System Events").processes.byName(name).windows()) out.push([name, w]); } catch (e) {}
  }
  return out;
}

// The button named NAME (呼叫 or 取消) of a banner that says 点按以呼叫 (its siblings), so no other
// notification is pressed.
function bannerButton(name) {
  for (const [, w] of windows()) {
    const hit = walk(w, 0, (k, siblings) => {
      const [role, desc] = describe(k);
      if (role !== "AXButton" || desc !== name) return null;
      return siblings.some((s) => describe(s)[1].endsWith("点按以呼叫")) ? k : null;
    });
    if (hit) return hit;
  }
  return null;
}
function callButton() { return bannerButton("呼叫"); }

function endButton() {
  for (const [, w] of windows()) {
    const hit = walk(w, 0, (k) => {
      const [role, desc] = describe(k);
      return role === "AXButton" && END_BUTTONS.indexOf(desc) >= 0 ? k : null;
    });
    if (hit) return hit;
  }
  return null;
}

function run(argv) {
  if (argv[0] === "call") {
    const b = callButton();
    if (!b) return "NO_BANNER";
    b.actions.byName("AXPress").perform();
    return "CALLED";
  }
  if (argv[0] === "find") return callButton() ? "FOUND" : "NOT_FOUND";  // checking only, nothing pressed
  if (argv[0] === "cancel") {  // close every call banner left waiting, without calling: CANCELLED or NO_BANNER
    let closed = 0;
    for (let b = bannerButton("取消"); b && closed < 5; b = bannerButton("取消")) {
      b.actions.byName("AXPress").perform();
      closed++;
      delay(0.3);
    }
    return closed ? "CANCELLED" : "NO_BANNER";
  }
  if (argv[0] === "hangup") {
    const b = endButton();
    if (b) { b.actions.byName("AXPress").perform(); return "ENDED"; }
    return "NO_BUTTON";
  }
  const out = [];
  for (const [name, w] of windows()) {
    let title = "";
    try { title = w.name() || ""; } catch (e) {}
    out.push("== " + name + " | " + title);
    walk(w, 0, (k) => {
      const [role, desc] = describe(k);
      if (role === "AXButton" && desc) out.push(role + " | " + desc);
      return null;
    });
  }
  return out.join("\n");
}

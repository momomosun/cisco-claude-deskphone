// desk_focus.js HOST_ID — bring the Claude desktop session HOST_ID (local_...) to the front by
// opening its claude:// link, which raises the app and shows that session. Prints OPENED, or
// LOCKED having done nothing: behind the lock screen the link would only switch the main window.
ObjC.import("CoreGraphics");

function run(argv) {
  const host = argv[0] || "";
  if (!/^local_[0-9a-f-]{36}$/.test(host)) return "ABORT: bad session id";
  const session = ObjC.deepUnwrap(ObjC.castRefToObject($.CGSessionCopyCurrentDictionary()));
  if (session.CGSSessionScreenIsLocked === true) return "LOCKED";
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  app.doShellScript("/usr/bin/open 'claude://claude.ai/epitaxy/" + host + "'");
  return "OPENED";
}

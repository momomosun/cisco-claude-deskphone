import http.server
import json
import threading
import time

import pytest

import hubauth
import phone_hub


def test_same_construction_as_the_hub_and_the_hook():
    assert hubauth.sign("k", "1", "n", "/p", b"{}") == phone_hub.sign("k", "1", "n", "/p", b"{}")


def test_verify_accepts_a_signed_request_once():
    nonces = hubauth.NonceCache()
    body = b'{"a": 1}'
    headers = hubauth.signed_headers("k", "/x", body)
    assert hubauth.verify("k", headers, "/x", body, nonces) is True
    assert hubauth.verify("k", headers, "/x", body, nonces) is False  # replay


def test_verify_rejects_wrong_key_path_body_or_stale_time():
    body = b"{}"
    headers = hubauth.signed_headers("k", "/x", body)
    assert not hubauth.verify("other", headers, "/x", body, hubauth.NonceCache())
    assert not hubauth.verify("k", headers, "/y", body, hubauth.NonceCache())
    assert not hubauth.verify("k", headers, "/x", b"{ }", hubauth.NonceCache())
    stale = dict(headers, **{"X-Hub-Time": str(int(time.time()) - 3600)})
    stale["X-Hub-Signature"] = hubauth.sign("k", stale["X-Hub-Time"], stale["X-Hub-Nonce"], "/x", body)
    assert not hubauth.verify("k", stale, "/x", body, hubauth.NonceCache())
    assert not hubauth.verify("k", {}, "/x", body, hubauth.NonceCache())


def answering(headers_for):
    """A server answering every POST with {"ok": true} and headers_for(request nonce, body)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            for name, value in headers_for(self.headers["X-Hub-Nonce"], body).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_signed_answer_must_answer_this_request():
    server, base = answering(lambda nonce, body: {"X-Hub-Answer": hubauth.sign("k", nonce, body)})
    try:
        assert hubauth.post(base, "k", "/x", {}, signed_answer=True) == {"ok": True}
    finally:
        server.shutdown()
    for forged in (lambda nonce, body: {},
                   lambda nonce, body: {"X-Hub-Answer": hubauth.sign("other", nonce, body)},
                   lambda nonce, body: {"X-Hub-Answer": hubauth.sign("k", "an older nonce", body)},
                   lambda nonce, body: {"X-Hub-Answer": hubauth.sign("k", nonce, body + b" ")}):
        server, base = answering(forged)
        try:
            with pytest.raises(ValueError):
                hubauth.post(base, "k", "/x", {}, signed_answer=True)
            assert hubauth.post(base, "k", "/x", {}) == {"ok": True}  # the hub's own answers are not signed
        finally:
            server.shutdown()

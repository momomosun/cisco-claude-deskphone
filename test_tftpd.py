"""End-to-end tests: start tftpd.py on a high port and fetch over real UDP."""
import hashlib
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 16969
LAST_OACK = {}


def rrq(name, opts=None, port=PORT, opcode=1, mode="octet"):
    """Minimal TFTP client. Returns (bytes, None) or (None, error_code).

    The options the server acknowledged are left in LAST_OACK.
    """
    LAST_OACK.clear()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    payload = struct.pack("!H", opcode) + name.encode() + b"\0" + mode.encode() + b"\0"
    for k, v in (opts or {}).items():
        payload += k.encode() + b"\0" + str(v).encode() + b"\0"
    sock.sendto(payload, ("127.0.0.1", port))
    blksize, data, expected = 512, b"", 1
    while True:
        packet, server = sock.recvfrom(70000)
        op = struct.unpack("!H", packet[:2])[0]
        if op == 5:
            return None, struct.unpack("!H", packet[2:4])[0]
        if op == 6:
            fields = packet[2:].split(b"\0")[:-1]
            negotiated = {k.decode(): v.decode() for k, v in zip(fields[::2], fields[1::2])}
            LAST_OACK.update(negotiated)
            blksize = int(negotiated.get("blksize", 512))
            sock.sendto(struct.pack("!HH", 4, 0), server)
            continue
        block = struct.unpack("!H", packet[2:4])[0]
        if block == expected % 65536:
            data += packet[4:]
            expected += 1
        sock.sendto(struct.pack("!HH", 4, block), server)
        if len(packet) - 4 < blksize:
            return data, None


@pytest.fixture(scope="module")
def server():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "sub"))
    big = os.urandom(512 * 300 + 17)
    with open(os.path.join(root, "big.sbn"), "wb") as f:
        f.write(big)
    with open(os.path.join(root, "exact.bin"), "wb") as f:
        f.write(b"x" * 1024)
    with open(os.path.join(root, "sub", "a.txt"), "wb") as f:
        f.write(b"nested")
    rollover = os.urandom(8 * 65540 + 3)  # > 65535 blocks at blksize 8
    with open(os.path.join(root, "rollover.bin"), "wb") as f:
        f.write(rollover)
    secret = os.path.join(os.path.dirname(root), "outside-secret.txt")
    with open(secret, "w") as f:
        f.write("nope")
    os.symlink(secret, os.path.join(root, "link-out.txt"))
    env = dict(os.environ, TFTP_ALLOWED_NET="10.9.9.9/32, 127.0.0.0/8")  # each phone, comma-separated
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tftpd.py"), root, str(PORT)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(0.5)
    yield {"big": big, "rollover": rollover}
    proc.terminate()
    proc.wait()
    os.remove(secret)


def test_default_blocksize_transfer(server):
    data, err = rrq("big.sbn")
    assert err is None
    assert hashlib.sha256(data).digest() == hashlib.sha256(server["big"]).digest()


def test_blksize_and_tsize_negotiation(server):
    data, err = rrq("big.sbn", {"blksize": 1400, "tsize": 0})
    assert err is None and data == server["big"]
    assert LAST_OACK == {"blksize": "1400", "tsize": str(len(server["big"]))}


def test_block_number_rolls_over_past_65535(server):
    data, err = rrq("rollover.bin", {"blksize": 8})
    assert err is None and data == server["rollover"]


def test_blksize_below_minimum_is_not_acknowledged(server):
    data, err = rrq("big.sbn", {"blksize": 1, "tsize": 0})
    assert err is None and data == server["big"]
    assert "blksize" not in LAST_OACK


def test_timeout_above_cap_is_not_acknowledged(server):
    data, err = rrq("exact.bin", {"timeout": 200, "tsize": 0})
    assert err is None and "timeout" not in LAST_OACK


def test_netascii_mode_refused(server):
    assert rrq("exact.bin", mode="netascii") == (None, 4)


def test_symlink_pointing_outside_root_refused(server):
    assert rrq("link-out.txt") == (None, 1)


def test_file_exact_multiple_of_blocksize_ends_with_empty_block(server):
    data, err = rrq("exact.bin")
    assert err is None and data == b"x" * 1024


def test_leading_slash_and_subdirectory(server):
    assert rrq("/sub/a.txt") == (b"nested", None)


def test_missing_file_is_not_found(server):
    assert rrq("SEP000000000000.cnf.xml") == (None, 1)


@pytest.mark.parametrize("name", ["../outside-secret.txt", "sub/../../outside-secret.txt", "sub\\..\\x"])
def test_path_traversal_refused(server, name):
    assert rrq(name) == (None, 1)


def test_unacknowledged_block_is_retransmitted(server):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(4)
    sock.sendto(struct.pack("!H", 1) + b"exact.bin\0octet\0", ("127.0.0.1", PORT))
    first, server_addr = sock.recvfrom(1000)
    again, _ = sock.recvfrom(1000)  # no ACK sent: the server must resend block 1
    assert first == again and struct.unpack("!HH", first[:4]) == (3, 1)
    sock.sendto(struct.pack("!HH", 5, 0) + b"stop\0", server_addr)


def test_write_request_refused(server):
    assert rrq("upload.bin", opcode=2) == (None, 2)


def test_a_client_on_no_listed_network_gets_no_answer(tmp_path):
    with open(tmp_path / "f.txt", "w") as f:
        f.write("x")
    env = dict(os.environ, TFTP_ALLOWED_NET="10.9.9.9/32,10.9.9.10/32")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tftpd.py"), str(tmp_path), str(PORT + 1)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        time.sleep(0.5)
        with pytest.raises(socket.timeout):
            rrq("f.txt", port=PORT + 1)
    finally:
        proc.terminate()
        proc.wait()

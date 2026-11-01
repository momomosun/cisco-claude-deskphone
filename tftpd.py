#!/usr/bin/env python3
"""Read-only TFTP server for provisioning Cisco IP phones.

Serves files from one directory, answers RRQ only (WRQ is refused), supports
the blksize/tsize/timeout options (RFC 2347/2348/2349), and only talks to
clients inside ALLOWED_NETS (TFTP_ALLOWED_NET: one network, or several
separated by commas). Every request is logged so the phone's boot
sequence (which files it asks for) can be followed in the log.

Usage: tftpd.py <root_dir> [port]
"""
import ipaddress
import os
import socket
import struct
import sys
import threading
import time

ALLOWED_NETS = [ipaddress.ip_network(n.strip())
                for n in os.environ.get("TFTP_ALLOWED_NET", "192.168.1.0/24").split(",") if n.strip()]
DEFAULT_BLKSIZE = 512
MAX_BLKSIZE = 65464
DEFAULT_TIMEOUT = 2.0
MAX_TIMEOUT = 10
MAX_RETRIES = 5
MAX_TRANSFERS = 16  # concurrent transfers; extra requests get "server busy"

OP_RRQ, OP_WRQ, OP_DATA, OP_ACK, OP_ERROR, OP_OACK = 1, 2, 3, 4, 5, 6
ERR_UNDEFINED, ERR_NOT_FOUND, ERR_ACCESS, ERR_ILLEGAL = 0, 1, 2, 4


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def error_packet(code, msg):
    return struct.pack("!HH", OP_ERROR, code) + msg.encode() + b"\0"


def parse_request(packet):
    """Return (opcode, filename, mode, options) or raise ValueError."""
    if len(packet) < 4:
        raise ValueError("short packet")
    opcode = struct.unpack("!H", packet[:2])[0]
    fields = packet[2:].split(b"\0")
    if len(fields) < 3:
        raise ValueError("malformed request")
    filename = fields[0].decode("ascii", "replace")
    mode = fields[1].decode("ascii", "replace").lower()
    opts = {}
    rest = fields[2:-1]
    for i in range(0, len(rest) - 1, 2):
        opts[rest[i].decode("ascii", "replace").lower()] = rest[i + 1].decode("ascii", "replace")
    return opcode, filename, mode, opts


def resolve_path(root, filename):
    """Map a requested name to a file inside root, or None if it escapes/missing."""
    name = filename.lstrip("/")
    if not name or "\\" in name or any(part in ("", ".", "..") for part in name.split("/")):
        return None
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
        return None
    return path


def negotiate(opts, file_size):
    """Return (blksize, timeout, oack_options) for the options we accept."""
    blksize, timeout, accepted = DEFAULT_BLKSIZE, DEFAULT_TIMEOUT, {}
    if "blksize" in opts:
        try:
            requested = int(opts["blksize"])
            # RFC 2348: never answer with more than was asked; below 8 is invalid, ignore it
            if requested >= 8:
                blksize = min(MAX_BLKSIZE, requested)
                accepted["blksize"] = str(blksize)
        except ValueError:
            pass
    if "timeout" in opts:
        try:
            value = int(opts["timeout"])
            if 1 <= value <= MAX_TIMEOUT:
                timeout = float(value)
                accepted["timeout"] = str(value)
        except ValueError:
            pass
    if "tsize" in opts:
        accepted["tsize"] = str(file_size)
    return blksize, timeout, accepted


def send_and_wait_ack(sock, packet, client, block, timeout):
    """Send packet until the client ACKs `block`. Return True on success."""
    for _ in range(MAX_RETRIES):
        sock.sendto(packet, client)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(4 + MAX_BLKSIZE)
            except socket.timeout:
                break
            if addr != client or len(data) < 4:
                continue
            opcode, number = struct.unpack("!HH", data[:4])
            if opcode == OP_ERROR:
                return False
            if opcode == OP_ACK and number == block:
                return True
    return False


def serve_file(client, path, filename, opts):
    size = os.path.getsize(path)
    blksize, timeout, accepted = negotiate(opts, size)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    started = time.monotonic()
    try:
        if accepted:
            oack = struct.pack("!H", OP_OACK) + b"".join(
                k.encode() + b"\0" + v.encode() + b"\0" for k, v in accepted.items())
            if not send_and_wait_ack(sock, oack, client, 0, timeout):
                log(f"{client[0]} {filename}: no ACK for OACK, aborted")
                return
        with open(path, "rb") as f:
            block = 0
            while True:
                chunk = f.read(blksize)
                block = (block + 1) % 65536
                packet = struct.pack("!HH", OP_DATA, block) + chunk
                if not send_and_wait_ack(sock, packet, client, block, timeout):
                    log(f"{client[0]} {filename}: transfer timed out")
                    return
                if len(chunk) < blksize:
                    break
        log(f"{client[0]} {filename}: sent {size} bytes in {time.monotonic() - started:.1f}s (blksize {blksize})")
    finally:
        sock.close()


def handle(packet, client, root):
    try:
        opcode, filename, mode, opts = parse_request(packet)
    except ValueError as exc:
        log(f"{client[0]} bad packet: {exc}")
        return
    reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if opcode != OP_RRQ:
            log(f"{client[0]} refused opcode {opcode} for {filename!r}")
            reply.sendto(error_packet(ERR_ACCESS, "read-only server"), client)
            return
        if mode != "octet":  # netascii would need CR/LF conversion; phones use octet
            reply.sendto(error_packet(ERR_ILLEGAL, "only octet mode supported"), client)
            return
        path = resolve_path(root, filename)
        if path is None:
            log(f"{client[0]} {filename}: not found")
            reply.sendto(error_packet(ERR_NOT_FOUND, "file not found"), client)
            return
    finally:
        reply.close()
    log(f"{client[0]} RRQ {filename} {opts or ''}")
    serve_file(client, path, filename, opts)


def run_slot(slots, packet, client, root):
    try:
        handle(packet, client, root)
    except OSError as exc:
        log(f"{client[0]} transfer failed: {exc}")
    finally:
        slots.release()


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: tftpd.py <root_dir> [port]")
    root = os.path.realpath(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 69
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    slots = threading.BoundedSemaphore(MAX_TRANSFERS)
    log(f"serving {root} on udp/{port}, clients {', '.join(map(str, ALLOWED_NETS))}")
    while True:
        packet, client = sock.recvfrom(1024)
        if not any(ipaddress.ip_address(client[0]) in net for net in ALLOWED_NETS):
            log(f"{client[0]} ignored (not a provisioned phone)")
            continue
        if not slots.acquire(blocking=False):
            log(f"{client[0]} busy ({MAX_TRANSFERS} transfers running)")
            try:
                sock.sendto(error_packet(ERR_UNDEFINED, "server busy"), client)
            except OSError as exc:
                log(f"{client[0]} could not send busy reply: {exc}")
            continue
        try:
            threading.Thread(target=run_slot, args=(slots, packet, client, root), daemon=True).start()
        except RuntimeError as exc:
            slots.release()
            log(f"{client[0]} could not start transfer: {exc}")


if __name__ == "__main__":
    main()

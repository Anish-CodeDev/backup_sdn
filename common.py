"""Shared wire protocol + hashing helpers used by client.py and server.py.

Frame layout:  [4B header_len][4B payload_len][JSON header][raw payload bytes]
"""
import hashlib
import json
import struct

_FRAME = struct.Struct("!II")
MAX_HDR = 1 << 20          # 1 MB   sanity limits against garbage data
MAX_PAYLOAD = 64 << 20     # 64 MB


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(min(n - len(buf), 65536))
        if not part:
            raise ConnectionError("peer closed the connection")
        buf += part
    return bytes(buf)


def send_msg(sock, header, payload=b""):
    h = json.dumps(header).encode()
    sock.sendall(_FRAME.pack(len(h), len(payload)) + h + payload)


def recv_msg(sock):
    hl, pl = _FRAME.unpack(recv_exact(sock, _FRAME.size))
    if hl > MAX_HDR or pl > MAX_PAYLOAD:
        raise ConnectionError("frame too large / corrupt stream")
    header = json.loads(recv_exact(sock, hl))
    payload = recv_exact(sock, pl) if pl else b""
    return header, payload


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

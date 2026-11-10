#!/usr/bin/env python3
"""Backup server: receives chunks over TCP, verifies SHA-256, ACK/NACKs,
persists verified chunks (so transfers can resume), reassembles + verifies
the whole file on DONE.

Usage:  python3 server.py --port 5000 --dir /tmp/backup_h3
"""
import argparse
import hmac
import os
import shutil
import socket
import threading
import time

from common import recv_msg, send_msg, sha256_bytes, sha256_file
import hashlib


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BackupServer:
    def __init__(self, host, port, base, idle_timeout):
        self.host, self.port, self.base = host, port, base
        self.idle_timeout = idle_timeout
        os.makedirs(base, exist_ok=True)

    # ---- paths ----------------------------------------------------------
    def parts_dir(self, fid):
        return os.path.join(self.base, fid + ".parts")

    def chunk_path(self, fid, n):
        return os.path.join(self.parts_dir(fid), f"{n:08d}.chunk")

    def final_path(self, fid):
        return os.path.join(self.base, fid)

    # ---- main loop ------------------------------------------------------
    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(16)
        log(f"server listening on {self.host}:{self.port}, storing in {self.base}")
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()

    def handle(self, conn, addr):
        log(f"connection from {addr[0]}:{addr[1]}")
        conn.settimeout(self.idle_timeout)
        meta = None
        try:
            while True:
                hdr, payload = recv_msg(conn)
                t = hdr.get("type")
                if t == "INIT":
                    meta = hdr
                    self.on_init(conn, meta)
                elif t == "CHUNK" and meta:
                    self.on_chunk(conn, meta, hdr, payload)
                elif t == "DONE" and meta:
                    self.on_done(conn, meta)
                    return
                else:
                    send_msg(conn, {"type": "ERROR", "reason": "bad message order"})
        except (OSError, ValueError) as e:      # includes timeouts/resets
            log(f"connection {addr[0]}:{addr[1]} ended: {e!r}")
        finally:
            conn.close()

    # ---- handlers -------------------------------------------------------
    def on_init(self, conn, meta):
        fid = os.path.basename(meta["file_id"])
        final = self.final_path(fid)
        if (os.path.exists(final) and os.path.getsize(final) == meta["file_size"]
                and sha256_file(final) == meta["file_sha256"]):
            log(f"INIT {fid}: already complete")
            send_msg(conn, {"type": "INIT_OK", "have": list(range(meta["total_chunks"])),
                            "complete": True})
            return
        os.makedirs(self.parts_dir(fid), exist_ok=True)
        have = sorted(int(f.split(".")[0]) for f in os.listdir(self.parts_dir(fid))
                      if f.endswith(".chunk"))
        log(f"INIT {fid}: {meta['total_chunks']} chunks, already have {len(have)}")
        send_msg(conn, {"type": "INIT_OK", "have": have, "complete": False})

    def on_chunk(self, conn, meta, hdr, payload):
        fid, n = os.path.basename(meta["file_id"]), hdr["chunk_no"]
        ok = len(payload) == hdr["size"] and hmac.compare_digest(
            sha256_bytes(payload), hdr["sha256"])
        if not ok:
            log(f"chunk {n}: CHECKSUM MISMATCH -> NACK")
            send_msg(conn, {"type": "NACK", "chunk_no": n, "reason": "checksum"})
            return
        os.makedirs(self.parts_dir(fid), exist_ok=True)
        path = self.chunk_path(fid, n)
        tmp = path + ".tmp%d" % threading.get_ident()
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)                    # atomic + idempotent
        send_msg(conn, {"type": "ACK", "chunk_no": n})

    def on_done(self, conn, meta):
        fid, total = os.path.basename(meta["file_id"]), meta["total_chunks"]
        missing = [n for n in range(total) if not os.path.exists(self.chunk_path(fid, n))]
        if missing:
            log(f"DONE {fid}: missing chunks {missing[:10]}")
            send_msg(conn, {"type": "DONE_FAIL", "reason": "missing", "missing": missing[:50]})
            return
        final, tmp, h = self.final_path(fid), self.final_path(fid) + ".tmp", hashlib.sha256()
        with open(tmp, "wb") as out:
            for n in range(total):
                with open(self.chunk_path(fid, n), "rb") as c:
                    data = c.read()
                h.update(data)
                out.write(data)
        if h.hexdigest() == meta["file_sha256"]:
            os.replace(tmp, final)
            shutil.rmtree(self.parts_dir(fid), ignore_errors=True)
            log(f"DONE {fid}: whole-file SHA-256 verified OK")
            send_msg(conn, {"type": "DONE_OK"})
        else:
            os.remove(tmp)
            shutil.rmtree(self.parts_dir(fid), ignore_errors=True)
            log(f"DONE {fid}: whole-file hash MISMATCH")
            send_msg(conn, {"type": "DONE_FAIL", "reason": "file hash mismatch"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--dir", required=True, help="storage directory (unique per server!)")
    ap.add_argument("--idle-timeout", type=float, default=60)
    a = ap.parse_args()
    BackupServer(a.host, a.port, a.dir, a.idle_timeout).serve()

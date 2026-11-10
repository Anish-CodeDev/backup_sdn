#!/usr/bin/env python3
"""Backup client: splits a file into chunks, sends each with a SHA-256,
handles ACK/NACK, and on timeout/connection loss reconnects and resumes from
the chunks the server already has.  Replicates to every server given
(one thread per server).

Usage: python3 client.py --servers 10.0.0.3 10.0.0.4 --file test.bin
"""
import argparse
import json
import math
import os
import socket
import threading
import time

from common import recv_msg, send_msg, sha256_bytes, sha256_file


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def transfer(server, args, file_sha, stats):
    path, fid = args.file, os.path.basename(args.file)
    size = os.path.getsize(path)
    total = max(1, math.ceil(size / args.chunk_size))
    corrupt = set(args.corrupt_chunks)
    stats.update(server=server, file=fid, size=size, chunks=total, nacks=0,
                 reconnects=0, ack_times=[], success=False, error=None)
    stats["start"] = time.time()
    f = open(path, "rb")
    try:
        while stats["reconnects"] <= args.max_reconnects:
            sock = None
            try:
                sock = socket.create_connection((server, args.port), timeout=args.timeout)
                sock.settimeout(args.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                send_msg(sock, {"type": "INIT", "file_id": fid, "file_size": size,
                                "chunk_size": args.chunk_size, "total_chunks": total,
                                "file_sha256": file_sha})
                reply, _ = recv_msg(sock)
                have = set(reply["have"])
                if stats["reconnects"]:
                    log(f"[{server}] resumed: server already has {len(have)}/{total} chunks")
                if reply.get("complete"):
                    stats["success"] = True
                    break
                for n in range(total):
                    if n in have:
                        continue
                    f.seek(n * args.chunk_size)
                    data = f.read(args.chunk_size)
                    digest = sha256_bytes(data)           # hash of the GOOD data
                    for attempt in range(args.max_retries):
                        payload = data
                        if n in corrupt and attempt == 0:  # demo: corrupt 1st attempt
                            payload = bytes([data[0] ^ 0xFF]) + data[1:]
                        send_msg(sock, {"type": "CHUNK", "chunk_no": n, "size": len(data),
                                        "sha256": digest}, payload)
                        reply, _ = recv_msg(sock)
                        if reply["type"] == "ACK" and reply["chunk_no"] == n:
                            stats["ack_times"].append(time.time())
                            break
                        stats["nacks"] += 1
                        log(f"[{server}] NACK chunk {n} (attempt {attempt + 1}) -> resending")
                    else:
                        raise RuntimeError(f"chunk {n} failed {args.max_retries} times")
                send_msg(sock, {"type": "DONE"})
                reply, _ = recv_msg(sock)
                if reply["type"] == "DONE_OK":
                    stats["success"] = True
                else:
                    stats["error"] = str(reply)
                break
            except OSError as e:          # timeout, reset, unreachable, ...
                stats["reconnects"] += 1
                log(f"[{server}] connection problem ({e!r}); reconnect #{stats['reconnects']}")
                time.sleep(args.reconnect_delay)
            finally:
                if sock:
                    sock.close()
        else:
            stats["error"] = "too many reconnects"
    except Exception as e:
        stats["error"] = repr(e)
    finally:
        f.close()
        stats["end"] = time.time()
        dur = stats["end"] - stats["start"]
        stats["duration_s"] = dur
        stats["throughput_mbps"] = stats["size"] * 8 / dur / 1e6 if dur > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", nargs="+", required=True)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--file", required=True)
    ap.add_argument("--chunk-size", type=int, default=256 * 1024)
    ap.add_argument("--timeout", type=float, default=5, help="socket timeout (s)")
    ap.add_argument("--max-retries", type=int, default=5, help="resends per chunk on NACK")
    ap.add_argument("--max-reconnects", type=int, default=20)
    ap.add_argument("--reconnect-delay", type=float, default=0.5)
    ap.add_argument("--corrupt-chunks", type=lambda s: [int(x) for x in s.split(",") if x],
                    default=[], help="e.g. 2,7 : corrupt these chunks once to demo NACK")
    ap.add_argument("--result", help="write JSON stats here")
    args = ap.parse_args()

    file_sha = sha256_file(args.file)          # whole-file hash, computed before timing
    log(f"file {args.file} sha256={file_sha[:16]}... -> {args.servers}")
    results = [dict() for _ in args.servers]
    threads = [threading.Thread(target=transfer, args=(s, args, file_sha, r))
               for s, r in zip(args.servers, results)]
    for t in threads: t.start()
    for t in threads: t.join()

    for r in results:
        log(f"[{r['server']}] success={r['success']} time={r['duration_s']:.2f}s "
            f"throughput={r['throughput_mbps']:.2f} Mbit/s nacks={r['nacks']} "
            f"reconnects={r['reconnects']} error={r['error']}")
    if args.result:
        with open(args.result, "w") as fh:
            json.dump(results, fh)
    raise SystemExit(0 if all(r["success"] for r in results) else 1)


if __name__ == "__main__":
    main()

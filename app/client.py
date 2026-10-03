import argparse
import hashlib
import os
import socket

from protocol import *

MAX_RETRIES = 5


def send_file(path, host, port, corrupt_chunk=None):
    size = os.path.getsize(path)
    total = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
    name = os.path.basename(path)

    sock = socket.create_connection((host, port), timeout=5)
    print(f"Connected to {host}:{port}, sending {name} ({size} bytes, {total} chunks)")

    send_msg(sock, INIT, name, 0, total)
    if recv_msg(sock)["type"] != ACK:
        raise RuntimeError("server rejected INIT")

    whole = hashlib.sha256()
    with open(path, "rb") as f:
        for i in range(total):
            data = f.read(CHUNK_SIZE)
            whole.update(data)
            good_digest = sha256(data)

            for attempt in range(1, MAX_RETRIES + 1):
                to_send = data
                # Demo: flip one byte on the first attempt, keeping the original checksum
                if corrupt_chunk == i and attempt == 1 and data:
                    to_send = bytes([data[0] ^ 0xFF]) + data[1:]
                    print(f"  (deliberately corrupting chunk {i})")

                send_msg(sock, CHUNK, name, i, total, to_send, digest=good_digest)
                try:
                    reply = recv_msg(sock)
                except socket.timeout:
                    print(f"  chunk {i}: timeout, retry {attempt}")
                    continue
                if reply["type"] == ACK:
                    break
                print(f"  chunk {i}: NACK received, retransmitting (attempt {attempt})")
            else:
                raise RuntimeError(f"chunk {i} failed after {MAX_RETRIES} attempts")

    send_msg(sock, DONE, name, total, total, b"", digest=whole.digest())
    final = recv_msg(sock)
    sock.close()
    if final["type"] == ACK:
        print("Transfer complete: whole-file checksum verified by server")
    else:
        print("Transfer FAILED: whole-file checksum mismatch")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--corrupt", type=int, default=None, help="chunk number to corrupt once")
    a = p.parse_args()
    send_file(a.file, a.host, a.port, a.corrupt)

import argparse
import hashlib
import os
import socket
import threading

from protocol import *

OUT_DIR = "received_files"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.digest()


def handle_client(conn, addr):
    print(f"[+] Connection from {addr[0]}:{addr[1]}")
    conn.settimeout(60)
    f = None
    path = None
    try:
        while True:
            msg = recv_msg(conn)
            t = msg["type"]

            if t == INIT:
                os.makedirs(OUT_DIR, exist_ok=True)
                name = f"{addr[0]}_{os.path.basename(msg['filename'])}"
                path = os.path.join(OUT_DIR, name)
                if f:
                    f.close()
                f = open(path, "wb")
                print(f"[{addr[0]}] INIT {name}, {msg['total']} chunks")
                send_msg(conn, ACK, msg["filename"], 0, msg["total"])

            elif t == CHUNK:
                n = msg["chunk_no"]
                if f is None or sha256(msg["payload"]) != msg["digest"]:
                    print(f"[{addr[0]}] chunk {n}: BAD CHECKSUM -> NACK")
                    send_msg(conn, NACK, chunk_no=n)
                else:
                    f.seek(n * CHUNK_SIZE)
                    f.write(msg["payload"])
                    print(f"[{addr[0]}] chunk {n + 1}/{msg['total']} OK -> ACK")
                    send_msg(conn, ACK, chunk_no=n)

            elif t == DONE:
                f.close()
                f = None
                ok = file_sha256(path) == msg["digest"]
                print(f"[{addr[0]}] DONE, whole-file checksum {'MATCH' if ok else 'MISMATCH'}")
                send_msg(conn, ACK if ok else NACK)
                break
    except (ConnectionError, socket.timeout, OSError) as e:
        print(f"[!] {addr[0]}: connection error: {e}")
    finally:
        if f:
            f.close()
        conn.close()
        print(f"[-] Closed {addr[0]}:{addr[1]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(5)
    print(f"Backup server listening on {args.host}:{args.port}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()

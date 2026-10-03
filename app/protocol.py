import hashlib
import struct

# Message types
INIT, CHUNK, ACK, NACK, DONE = 1, 2, 3, 4, 5
NAMES = {1: "INIT", 2: "CHUNK", 3: "ACK", 4: "NACK", 5: "DONE"}

# Header: type, filename length, chunk number, total chunks, payload length, sha256
HEADER_FMT = "!BHIII32s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHUNK_SIZE = 64 * 1024  # 64 KB


def sha256(data):
    return hashlib.sha256(data).digest()


def recv_exact(sock, n):
    """TCP is a byte stream, so keep reading until exactly n bytes arrive."""
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("connection closed by peer")
        buf += part
    return buf


def send_msg(sock, msg_type, filename="", chunk_no=0, total=0, payload=b"", digest=None):
    name = filename.encode()
    if digest is None:
        digest = sha256(payload)
    header = struct.pack(HEADER_FMT, msg_type, len(name), chunk_no, total, len(payload), digest)
    sock.sendall(header + name + payload)


def recv_msg(sock):
    header = recv_exact(sock, HEADER_SIZE)
    msg_type, name_len, chunk_no, total, plen, digest = struct.unpack(HEADER_FMT, header)
    name = recv_exact(sock, name_len).decode() if name_len else ""
    payload = recv_exact(sock, plen) if plen else b""
    return {"type": msg_type, "filename": name, "chunk_no": chunk_no,
            "total": total, "payload": payload, "digest": digest}

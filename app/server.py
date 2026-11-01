import argparse
import hashlib
import json
import os
import re
import socket
import threading

from protocol import *

OUT_DIR = "received_files"


def file_sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)

    return digest.digest()


def transfer_directory(transfer_id):
    if not transfer_id:
        raise ValueError("empty transfer ID")

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", transfer_id):
        raise ValueError("invalid transfer ID")

    return os.path.join(OUT_DIR, transfer_id)


def metadata_path(transfer_dir):
    return os.path.join(transfer_dir, "metadata.json")


def chunks_directory(transfer_dir):
    return os.path.join(transfer_dir, "chunks")


def chunk_path(transfer_dir, chunk_no):
    return os.path.join(
        chunks_directory(transfer_dir),
        f"{chunk_no:08d}.part",
    )


def save_metadata(transfer_dir, metadata):
    temporary_path = metadata_path(transfer_dir) + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as file_obj:
        json.dump(metadata, file_obj, indent=2)

    os.replace(temporary_path, metadata_path(transfer_dir))


def load_metadata(transfer_dir):
    with open(metadata_path(transfer_dir), "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def send_ack(conn, message, transfer_id):
    send_msg(
        conn,
        ACK,
        filename=message.get("filename", ""),
        chunk_no=message.get("chunk_no", 0),
        total=message.get("total", 0),
        transfer_id=transfer_id,
    )


def handle_client(conn, addr):
    print(f"[+] Connection from {addr[0]}:{addr[1]}")

    conn.settimeout(60)

    current_transfer_dir = None
    current_metadata = None

    try:
        while True:
            message = recv_msg(conn)
            message_type = message["type"]
            transfer_id = message["transfer_id"]

            if message_type == INIT:
                try:
                    current_transfer_dir = transfer_directory(transfer_id)
                except ValueError as error:
                    print(f"[!] Invalid transfer ID: {error}")

                    send_msg(
                        conn,
                        NACK,
                        transfer_id=transfer_id,
                    )
                    break

                os.makedirs(
                    chunks_directory(current_transfer_dir),
                    exist_ok=True,
                )

                metadata_file = metadata_path(current_transfer_dir)

                if os.path.exists(metadata_file):
                    current_metadata = load_metadata(current_transfer_dir)

                    if (
                        current_metadata["filename"]
                        != os.path.basename(message["filename"])
                        or current_metadata["total_chunks"]
                        != message["total"]
                    ):
                        print(
                            f"[!] Transfer metadata mismatch for "
                            f"{transfer_id}"
                        )

                        send_msg(
                            conn,
                            NACK,
                            transfer_id=transfer_id,
                        )
                        break

                    print(
                        f"[{addr[0]}] Resuming transfer "
                        f"{transfer_id}"
                    )
                else:
                    current_metadata = {
                        "transfer_id": transfer_id,
                        "filename": os.path.basename(message["filename"]),
                        "total_chunks": message["total"],
                        "client_address": addr[0],
                    }

                    save_metadata(
                        current_transfer_dir,
                        current_metadata,
                    )

                    print(
                        f"[{addr[0]}] New transfer "
                        f"{transfer_id}: "
                        f"{current_metadata['filename']}"
                    )

                send_ack(conn, message, transfer_id)

            elif message_type == QUERY_MISSING:
                if current_transfer_dir is None or current_metadata is None:
                    send_msg(
                        conn,
                        NACK,
                        transfer_id=transfer_id,
                    )
                    continue

                total_chunks = current_metadata["total_chunks"]
                missing = []

                for chunk_no in range(total_chunks):
                    if not os.path.exists(
                        chunk_path(current_transfer_dir, chunk_no)
                    ):
                        missing.append(chunk_no)

                print(
                    f"[{addr[0]}] Transfer {transfer_id}: "
                    f"{len(missing)} missing chunks"
                )

                send_msg(
                    conn,
                    MISSING,
                    payload=json.dumps(missing).encode(),
                    transfer_id=transfer_id,
                )

            elif message_type == CHUNK:
                if current_transfer_dir is None:
                    send_msg(
                        conn,
                        NACK,
                        chunk_no=message["chunk_no"],
                        transfer_id=transfer_id,
                    )
                    continue

                chunk_no = message["chunk_no"]
                total_chunks = message["total"]

                if chunk_no < 0 or chunk_no >= total_chunks:
                    print(f"[!] Invalid chunk number: {chunk_no}")

                    send_msg(
                        conn,
                        NACK,
                        chunk_no=chunk_no,
                        transfer_id=transfer_id,
                    )
                    continue

                received_digest = sha256(message["payload"])

                if received_digest != message["digest"]:
                    print(
                        f"[{addr[0]}] Chunk {chunk_no}: "
                        "checksum failed"
                    )

                    send_msg(
                        conn,
                        NACK,
                        chunk_no=chunk_no,
                        transfer_id=transfer_id,
                    )
                    continue

                destination = chunk_path(
                    current_transfer_dir,
                    chunk_no,
                )
                temporary_destination = destination + ".tmp"

                with open(temporary_destination, "wb") as file_obj:
                    file_obj.write(message["payload"])
                    file_obj.flush()
                    os.fsync(file_obj.fileno())

                os.replace(temporary_destination, destination)

                print(
                    f"[{addr[0]}] Chunk {chunk_no + 1}/"
                    f"{total_chunks}: stored"
                )

                send_msg(
                    conn,
                    ACK,
                    chunk_no=chunk_no,
                    transfer_id=transfer_id,
                )

            elif message_type == DONE:
                if current_transfer_dir is None or current_metadata is None:
                    send_msg(
                        conn,
                        NACK,
                        transfer_id=transfer_id,
                    )
                    break

                total_chunks = current_metadata["total_chunks"]
                missing = [
                    chunk_no
                    for chunk_no in range(total_chunks)
                    if not os.path.exists(
                        chunk_path(current_transfer_dir, chunk_no)
                    )
                ]

                if missing:
                    print(
                        f"[{addr[0]}] Cannot finish {transfer_id}; "
                        f"{len(missing)} chunks are missing"
                    )

                    send_msg(
                        conn,
                        NACK,
                        payload=json.dumps(missing).encode(),
                        transfer_id=transfer_id,
                    )
                    continue

                final_path = os.path.join(
                    current_transfer_dir,
                    "final_file",
                )
                temporary_final_path = final_path + ".tmp"

                with open(temporary_final_path, "wb") as output:
                    for chunk_no in range(total_chunks):
                        with open(
                            chunk_path(
                                current_transfer_dir,
                                chunk_no,
                            ),
                            "rb",
                        ) as chunk_file:
                            while True:
                                block = chunk_file.read(1024 * 1024)

                                if not block:
                                    break

                                output.write(block)

                    output.flush()
                    os.fsync(output.fileno())

                calculated_digest = file_sha256(
                    temporary_final_path
                )

                if calculated_digest != message["digest"]:
                    print(
                        f"[{addr[0]}] Transfer {transfer_id}: "
                        "whole-file checksum mismatch"
                    )

                    os.remove(temporary_final_path)

                    send_msg(
                        conn,
                        NACK,
                        transfer_id=transfer_id,
                    )
                    continue

                os.replace(temporary_final_path, final_path)

                print(
                    f"[{addr[0]}] Transfer {transfer_id}: "
                    "complete and verified"
                )

                send_msg(
                    conn,
                    ACK,
                    transfer_id=transfer_id,
                )
                break

            else:
                print(
                    f"[!] Unknown message type {message_type} "
                    f"from {addr[0]}"
                )

                send_msg(
                    conn,
                    NACK,
                    transfer_id=transfer_id,
                )

    except (ConnectionError, socket.timeout, OSError, ValueError) as error:
        print(
            f"[!] {addr[0]} connection ended: {error}"
        )

    finally:
        conn.close()
        print(f"[-] Closed {addr[0]}:{addr[1]}")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)

    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    server_socket = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )
    server_socket.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1,
    )
    server_socket.bind((args.host, args.port))
    server_socket.listen(5)

    print(
        f"Backup server listening on "
        f"{args.host}:{args.port}"
    )

    try:
        while True:
            conn, addr = server_socket.accept()

            thread = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True,
            )
            thread.start()

    except KeyboardInterrupt:
        print("\nServer stopped")

    finally:
        server_socket.close()


if __name__ == "__main__":
    main()

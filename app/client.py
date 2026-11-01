import argparse
import hashlib
import json
import os
import socket
import time
import uuid

from protocol import *

MAX_CHUNK_RETRIES = 5
RECONNECT_DELAY = 1
MAX_RECONNECTS = 30


def calculate_file_sha256(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)

    return digest.digest()


def connect_and_initialize(
    path,
    host,
    port,
    transfer_id,
    total,
):
    sock = socket.create_connection(
        (host, port),
        timeout=5,
    )
    sock.settimeout(10)

    filename = os.path.basename(path)

    send_msg(
        sock,
        INIT,
        filename=filename,
        total=total,
        transfer_id=transfer_id,
    )

    response = recv_msg(sock)

    if response["type"] != ACK:
        sock.close()
        raise RuntimeError("server rejected INIT")

    send_msg(
        sock,
        QUERY_MISSING,
        filename=filename,
        total=total,
        transfer_id=transfer_id,
    )

    response = recv_msg(sock)

    if response["type"] != MISSING:
        sock.close()
        raise RuntimeError("server did not return missing chunks")

    missing = json.loads(
        response["payload"].decode("utf-8")
    )

    return sock, missing


def send_file(
    path,
    host,
    port,
    corrupt_chunk=None,
    transfer_id=None,
    stop_after=None,
):
    if transfer_id is None:
        transfer_id = uuid.uuid4().hex

    size = os.path.getsize(path)
    total = max(
        1,
        (size + CHUNK_SIZE - 1) // CHUNK_SIZE,
    )
    filename = os.path.basename(path)
    whole_digest = calculate_file_sha256(path)

    print(
        f"Transfer ID: {transfer_id}"
    )
    print(
        f"File: {filename}, size: {size} bytes, "
        f"chunks: {total}"
    )

    reconnect_count = 0
    corruption_used = False
    chunks_sent_this_run = 0

    while reconnect_count <= MAX_RECONNECTS:
        sock = None

        try:
            sock, missing = connect_and_initialize(
                path,
                host,
                port,
                transfer_id,
                total,
            )

            print(
                f"Server reports {len(missing)} missing chunks"
            )

            with open(path, "rb") as file_obj:
                for chunk_no in missing:
                    file_obj.seek(chunk_no * CHUNK_SIZE)
                    data = file_obj.read(CHUNK_SIZE)

                    good_digest = sha256(data)

                    for attempt in range(
                        1,
                        MAX_CHUNK_RETRIES + 1,
                    ):
                        payload = data

                        if (
                            corrupt_chunk == chunk_no
                            and not corruption_used
                            and attempt == 1
                            and data
                        ):
                            payload = (
                                bytes([data[0] ^ 0xFF])
                                + data[1:]
                            )
                            corruption_used = True

                            print(
                                f"Deliberately corrupting "
                                f"chunk {chunk_no}"
                            )

                        send_msg(
                            sock,
                            CHUNK,
                            filename=filename,
                            chunk_no=chunk_no,
                            total=total,
                            payload=payload,
                            digest=good_digest,
                            transfer_id=transfer_id,
                        )

                        response = recv_msg(sock)

                        if response["type"] == ACK:
                            print(
                                f"Chunk {chunk_no + 1}/{total} "
                                "ACK"
                            )
                            break

                        if response["type"] != NACK:
                            raise ConnectionError(
                                "unexpected chunk response"
                            )

                        print(
                            f"Chunk {chunk_no}: NACK; "
                            f"retry {attempt}/"
                            f"{MAX_CHUNK_RETRIES}"
                        )
                    else:
                        raise RuntimeError(
                            f"chunk {chunk_no} failed after "
                            f"{MAX_CHUNK_RETRIES} attempts"
                        )

                    chunks_sent_this_run += 1

                    if (
                        stop_after is not None
                        and chunks_sent_this_run >= stop_after
                    ):
                        print(
                            "Stopping intentionally for "
                            "resume testing"
                        )
                        sock.close()
                        return

            send_msg(
                sock,
                DONE,
                filename=filename,
                chunk_no=total,
                total=total,
                digest=whole_digest,
                transfer_id=transfer_id,
            )

            response = recv_msg(sock)

            if response["type"] == ACK:
                print(
                    "Transfer complete; "
                    "whole-file checksum verified"
                )
                sock.close()
                return

            print(
                "Server did not confirm completion. "
                "The next attempt will query missing chunks."
            )

        except (
            ConnectionError,
            TimeoutError,
            socket.timeout,
            OSError,
        ) as error:
            reconnect_count += 1

            print(
                f"Connection lost: {error}"
            )
            print(
                f"Reconnecting "
                f"({reconnect_count}/{MAX_RECONNECTS})..."
            )

            if reconnect_count > MAX_RECONNECTS:
                raise RuntimeError(
                    "maximum reconnect attempts exceeded"
                ) from error

            time.sleep(RECONNECT_DELAY)

        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    raise RuntimeError("transfer failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--corrupt",
        type=int,
        default=None,
        help="chunk number to corrupt once",
    )
    parser.add_argument(
        "--transfer-id",
        default=None,
        help="ID used to resume a transfer",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        help="stop after this many chunks; testing only",
    )

    args = parser.parse_args()

    send_file(
        args.file,
        args.host,
        args.port,
        args.corrupt,
        args.transfer_id,
        args.stop_after,
    )

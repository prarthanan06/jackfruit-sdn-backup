import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from protocol import CHUNK, recv_msg, send_msg


def test_message_round_trip_with_transfer_id():
    left, right = socket.socketpair()

    try:
        send_msg(
            left,
            CHUNK,
            filename="test.bin",
            chunk_no=3,
            total=10,
            payload=b"hello",
            transfer_id="test-001",
        )

        message = recv_msg(right)

        assert message["type"] == CHUNK
        assert message["transfer_id"] == "test-001"
        assert message["filename"] == "test.bin"
        assert message["chunk_no"] == 3
        assert message["total"] == 10
        assert message["payload"] == b"hello"

    finally:
        left.close()
        right.close()

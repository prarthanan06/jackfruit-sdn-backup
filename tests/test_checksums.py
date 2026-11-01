import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from protocol import sha256


def test_same_data_has_same_checksum():
    data = b"jackfruit backup test"
    assert sha256(data) == hashlib.sha256(data).digest()


def test_changed_data_has_different_checksum():
    assert sha256(b"original") != sha256(b"changed")

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.crypto import CryptoError, decrypt_file, encrypt_file, generate_key


def test_generate_key_is_valid_fernet_key() -> None:
    key = generate_key()
    assert isinstance(key, str)
    assert len(key) == 44


def test_round_trip(tmp_path: Path) -> None:
    plaintext = b"hello world\x00\x01binary"
    src = tmp_path / "in.bin"
    enc = tmp_path / "out.enc"
    dec = tmp_path / "out.bin"
    src.write_bytes(plaintext)

    key = generate_key()
    encrypt_file(src, enc, key)
    decrypt_file(enc, dec, key)

    assert dec.read_bytes() == plaintext
    assert enc.read_bytes() != plaintext


def test_wrong_key_fails(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    enc = tmp_path / "out.enc"
    dec = tmp_path / "out.bin"
    src.write_bytes(b"payload")

    encrypt_file(src, enc, generate_key())

    with pytest.raises(CryptoError):
        decrypt_file(enc, dec, generate_key())


def test_invalid_key_string_raises(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    src.write_bytes(b"payload")
    with pytest.raises(CryptoError):
        encrypt_file(src, tmp_path / "out.enc", "not-a-valid-fernet-key")


def test_atomic_write_no_partial_on_failure(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    dst = tmp_path / "out.enc"
    src.write_bytes(b"payload")
    key = generate_key()

    real_replace = __import__("os").replace

    def boom(_a: str, _b: str) -> None:
        raise OSError("simulated failure")

    with patch("src.crypto.os.replace", side_effect=boom):
        with pytest.raises(OSError):
            encrypt_file(src, dst, key)

    assert not dst.exists()
    tmp_sibling = dst.with_suffix(dst.suffix + ".tmp")
    assert not tmp_sibling.exists()
    assert real_replace is __import__("os").replace

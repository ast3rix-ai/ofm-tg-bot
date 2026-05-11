from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """Raised when encryption or decryption fails."""


def generate_key() -> str:
    """Generate a fresh Fernet key.

    Returns:
        A urlsafe-base64-encoded 32-byte Fernet key as an ASCII string.
    """
    return Fernet.generate_key().decode("ascii")


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"Invalid Fernet key: {exc}") from exc


def _atomic_write(dst: Path, payload: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def encrypt_file(src: Path, dst: Path, key: str) -> None:
    """Encrypt `src` to `dst` using a Fernet key.

    The destination is written atomically — a `.tmp` sibling is fully written
    and fsync'd, then renamed into place. The temp file is removed on failure.

    Args:
        src: Plaintext input path.
        dst: Encrypted output path.
        key: Fernet key (urlsafe-base64, 32 bytes).

    Raises:
        CryptoError: If the key is invalid.
        FileNotFoundError: If `src` does not exist.
    """
    plaintext = src.read_bytes()
    token = _fernet(key).encrypt(plaintext)
    _atomic_write(dst, token)


def decrypt_file(src: Path, dst: Path, key: str) -> None:
    """Decrypt `src` to `dst` using a Fernet key.

    Args:
        src: Encrypted input path.
        dst: Plaintext output path.
        key: Fernet key matching the one used at encryption time.

    Raises:
        CryptoError: If the key is invalid or the ciphertext does not verify.
        FileNotFoundError: If `src` does not exist.
    """
    ciphertext = src.read_bytes()
    try:
        plaintext = _fernet(key).decrypt(ciphertext)
    except InvalidToken as exc:
        raise CryptoError("Failed to decrypt session — wrong key or corrupt file.") from exc
    _atomic_write(dst, plaintext)

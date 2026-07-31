"""Envelope cifrati per il volume di scambio agent↔gateway.

Formato: magic + lunghezza header + header JSON canonico + ciphertext + tag GCM.
La chiave AES-256 è derivata con X25519+HKDF da una chiave effimera per file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"CLX1"
CHUNK = 1024 * 1024


def public_b64(key: X25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def public_from_b64(value: str) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(value.encode("ascii")))


def _derive(private: X25519PrivateKey, public: X25519PublicKey, nonce: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce,
                info=b"clodia-transfer-v1").derive(private.exchange(public))


def encrypt_file(src: Path, dest: Path, *, recipient: str, sender: str,
                 recipient_key: X25519PublicKey) -> dict:
    size = src.stat().st_size
    digest = hashlib.sha256()
    with src.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    ephemeral = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    header = {
        "v": 1, "sender": sender, "recipient": recipient,
        "timestamp": int(time.time()), "size": size, "sha256": digest.hexdigest(),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ephemeral_pub": public_b64(ephemeral.public_key()),
    }
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encryptor = Cipher(algorithms.AES(_derive(ephemeral, recipient_key, nonce)),
                       modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header_bytes)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source, dest.open("wb") as target:
        target.write(MAGIC)
        target.write(struct.pack(">I", len(header_bytes)))
        target.write(header_bytes)
        for chunk in iter(lambda: source.read(CHUNK), b""):
            target.write(encryptor.update(chunk))
        target.write(encryptor.finalize())
        target.write(encryptor.tag)
    return header


def decrypt_file(src: Path, dest: Path, *, recipient: str,
                 private_key: X25519PrivateKey, max_bytes: int,
                 max_age_seconds: int = 900) -> dict:
    total = src.stat().st_size
    with src.open("rb") as source:
        if source.read(4) != MAGIC:
            raise ValueError("envelope non cifrato o formato non valido")
        raw_len = source.read(4)
        if len(raw_len) != 4:
            raise ValueError("header envelope troncato")
        header_len = struct.unpack(">I", raw_len)[0]
        if header_len <= 0 or header_len > 64 * 1024:
            raise ValueError("dimensione header non valida")
        header_bytes = source.read(header_len)
        header = json.loads(header_bytes)
        if header.get("recipient") != recipient:
            raise PermissionError("envelope destinato a un altro recipient")
        timestamp = int(header.get("timestamp", 0))
        if timestamp <= 0 or abs(int(time.time()) - timestamp) > max_age_seconds:
            raise ValueError("envelope scaduto")
        clear_size = int(header.get("size", -1))
        if clear_size < 0 or clear_size > max_bytes:
            raise ValueError(f"file oltre il limite di {max_bytes} byte")
        cipher_size = total - 8 - header_len - 16
        if cipher_size != clear_size:
            raise ValueError("dimensione ciphertext non coerente")
        source.seek(total - 16)
        tag = source.read(16)
        source.seek(8 + header_len)
        nonce = base64.urlsafe_b64decode(header["nonce"].encode("ascii"))
        ephemeral = public_from_b64(header["ephemeral_pub"])
        decryptor = Cipher(algorithms.AES(_derive(private_key, ephemeral, nonce)),
                           modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header_bytes)
        digest = hashlib.sha256()
        remaining = cipher_size
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as target:
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("ciphertext troncato")
                remaining -= len(chunk)
                clear = decryptor.update(chunk)
                digest.update(clear)
                target.write(clear)
            tail = decryptor.finalize()
            digest.update(tail)
            target.write(tail)
    if digest.hexdigest() != header.get("sha256"):
        dest.unlink(missing_ok=True)
        raise ValueError("hash del contenuto non valido")
    return header

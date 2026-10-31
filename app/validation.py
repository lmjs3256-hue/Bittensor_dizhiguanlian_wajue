"""Strict, shareable-address format validation (no private-key handling)."""

from __future__ import annotations

import hashlib


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PREFIX = b"SS58PRE"


def _decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + ALPHABET.index(char)
        except ValueError as exc:
            raise ValueError("address contains non-base58 characters") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def is_valid_ss58(value: str) -> bool:
    if not isinstance(value, str) or not 35 <= len(value) <= 50:
        return False
    try:
        raw = _decode(value)
    except ValueError:
        return False
    prefix_length = 1 if raw and raw[0] < 64 else 2
    if len(raw) != prefix_length + 32 + 2:
        return False
    expected = hashlib.blake2b(PREFIX + raw[:-2], digest_size=64).digest()[:2]
    return raw[-2:] == expected

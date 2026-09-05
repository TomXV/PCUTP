"""CRC-32/ISO-HDLC helpers (section 11). Hex is upper case, 8 digits."""

import zlib


def crc32(data: bytes, seed: int = 0) -> int:
    return zlib.crc32(data, seed) & 0xFFFFFFFF


def crc32_hex(data: bytes, seed: int = 0) -> str:
    return f"{crc32(data, seed):08X}"


def to_hex(value: int) -> str:
    return f"{value & 0xFFFFFFFF:08X}"


def parse_hex(text: str) -> int:
    """Parse an 8-digit CRC field; raises ValueError on anything else."""
    if len(text) != 8 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"bad CRC32 field: {text!r}")
    return int(text, 16)

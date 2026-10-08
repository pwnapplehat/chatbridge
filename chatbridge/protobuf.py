"""Tiny protobuf wire-format codec (varint and length-delimited fields only) for Cursor's conversation state."""

from __future__ import annotations


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_bytes(number: int, payload: bytes) -> bytes:
    """A length-delimited field (wire type 2)."""
    return encode_varint(number << 3 | 2) + encode_varint(len(payload)) + payload


def field_varint(number: int, value: int) -> bytes:
    """A varint field (wire type 0)."""
    return encode_varint(number << 3) + encode_varint(value)


def decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def parse_fields(buf: bytes) -> list[tuple[int, int, bytes | int]] | None:
    """[(field number, wire type, value)] for a clean message, or None if `buf` is not valid protobuf."""
    pos, out = 0, []
    try:
        while pos < len(buf):
            key, pos = decode_varint(buf, pos)
            number, wire = key >> 3, key & 7
            if number == 0:
                return None
            value: bytes | int
            if wire == 0:
                value, pos = decode_varint(buf, pos)
            elif wire == 2:
                length, pos = decode_varint(buf, pos)
                if pos + length > len(buf):
                    return None
                value, pos = buf[pos : pos + length], pos + length
            elif wire == 1:
                value, pos = buf[pos : pos + 8], pos + 8
            elif wire == 5:
                value, pos = buf[pos : pos + 4], pos + 4
            else:
                return None
            out.append((number, wire, value))
    except IndexError:
        return None
    return out

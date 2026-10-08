"""Minimal protobuf wire-format dumper (no schema) used to study Cursor's agent-state blobs. Read-only research tool."""

from __future__ import annotations


def varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def parse(buf: bytes) -> list[tuple[int, int, object]] | None:
    """[(field, wiretype, value)] or None if buf is not a clean protobuf message."""
    pos, out = 0, []
    try:
        while pos < len(buf):
            key, pos = varint(buf, pos)
            field, wire = key >> 3, key & 7
            if field == 0:
                return None
            if wire == 0:
                value, pos = varint(buf, pos)
            elif wire == 2:
                length, pos = varint(buf, pos)
                if pos + length > len(buf):
                    return None
                value, pos = buf[pos : pos + length], pos + length
            elif wire == 1:
                value, pos = buf[pos : pos + 8], pos + 8
            elif wire == 5:
                value, pos = buf[pos : pos + 4], pos + 4
            else:
                return None
            out.append((field, wire, value))
    except IndexError:
        return None
    return out


def show(buf: bytes, depth: int = 0, max_depth: int = 4) -> list[str]:
    lines: list[str] = []
    fields = parse(buf)
    if fields is None:
        return [f"{'  ' * depth}<not protobuf: {len(buf)} bytes>"]
    for field, wire, value in fields:
        pad = "  " * depth
        if wire == 2 and isinstance(value, bytes):
            nested = parse(value) if depth < max_depth and value else None
            if len(value) == 32:
                lines.append(f"{pad}{field}: hash {value.hex()[:16]}…")
            elif nested:
                lines.append(f"{pad}{field}: message ({len(value)}B)")
                lines.extend(show(value, depth + 1, max_depth))
            else:
                try:
                    text = value.decode("utf-8")
                    lines.append(f"{pad}{field}: str {text[:70]!r}")
                except UnicodeDecodeError:
                    lines.append(f"{pad}{field}: bytes {len(value)}B {value[:12].hex()}")
        else:
            lines.append(f"{pad}{field}: {'varint' if wire == 0 else 'fixed'} {value if wire == 0 else bytes(value).hex()}")
    return lines

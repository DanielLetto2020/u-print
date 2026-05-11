#!/usr/bin/env python3
"""Tiny pure-Python ``.po`` → ``.mo`` compiler.

Avoids the system-package dependency on the full ``gettext`` toolchain. Only
handles features we actually use: singular messages, UTF-8, comments, and a
header msgstr block. No plurals, no contexts, no fuzzy entries.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path


def _unescape(s: str) -> str:
    """Undo PO escape sequences (\\n, \\t, \\", \\\\)."""
    return (
        s.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\"", '"').replace(r"\\", "\\")
    )


def _parse_po(text: str) -> dict[str, str]:
    """Return a {msgid: msgstr} dict. Empty msgid (header) is included."""
    entries: dict[str, str] = {}
    msgid: list[str] = []
    msgstr: list[str] = []
    state: str | None = None

    def flush() -> None:
        nonlocal msgid, msgstr, state
        if msgid is not None and state is not None:
            entries["".join(msgid)] = "".join(msgstr)
        msgid, msgstr, state = [], [], None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("msgid "):
            if state is not None:
                flush()
            state = "msgid"
            m = re.match(r'msgid\s+"(.*)"\s*$', line)
            if m:
                msgid.append(_unescape(m.group(1)))
            continue
        if line.startswith("msgstr "):
            state = "msgstr"
            m = re.match(r'msgstr\s+"(.*)"\s*$', line)
            if m:
                msgstr.append(_unescape(m.group(1)))
            continue
        m = re.match(r'"(.*)"\s*$', line)
        if m:
            piece = _unescape(m.group(1))
            if state == "msgid":
                msgid.append(piece)
            elif state == "msgstr":
                msgstr.append(piece)
    flush()
    return entries


def compile_po(po_path: Path, mo_path: Path) -> None:
    """Write a binary .mo file equivalent to ``po_path``."""
    entries = _parse_po(po_path.read_text(encoding="utf-8"))
    # Sort by msgid as required by the MO format
    items = sorted(entries.items(), key=lambda kv: kv[0].encode("utf-8"))
    keys = [k.encode("utf-8") for k, _ in items]
    values = [v.encode("utf-8") for _, v in items]
    n = len(items)
    header_size = 7 * 4
    table_offset_keys = header_size
    table_offset_values = table_offset_keys + 8 * n
    strings_offset = table_offset_values + 8 * n

    key_offsets: list[tuple[int, int]] = []
    value_offsets: list[tuple[int, int]] = []
    blob = b""
    offset = strings_offset
    for k in keys:
        key_offsets.append((len(k), offset))
        blob += k + b"\x00"
        offset += len(k) + 1
    for v in values:
        value_offsets.append((len(v), offset))
        blob += v + b"\x00"
        offset += len(v) + 1

    out = struct.pack(
        "Iiiiiii",
        0x950412DE,            # magic
        0,                     # version
        n,                     # number of strings
        table_offset_keys,     # offset of key table
        table_offset_values,   # offset of value table
        0,                     # hash size
        0,                     # hash offset
    )
    for length, off in key_offsets:
        out += struct.pack("ii", length, off)
    for length, off in value_offsets:
        out += struct.pack("ii", length, off)
    out += blob

    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(out)


def main() -> int:
    here = Path(__file__).resolve().parent
    count = 0
    for po in here.rglob("photoprint.po"):
        mo = po.with_suffix(".mo")
        compile_po(po, mo)
        print(f"  {mo.relative_to(here)}")
        count += 1
    if count == 0:
        print("No .po files found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

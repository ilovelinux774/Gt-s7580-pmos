"""Small helpers shared by pack-boot.py tests.

``minimal_dtb`` builds a structurally valid flattened device tree with a root
``model`` property, which is enough for every check this repository does on an
appended DTB.
"""

from __future__ import annotations

import struct


def minimal_dtb(model: bytes = b"Test Board") -> bytes:
    strings = b"model\x00"

    def prop(name_off: int, value: bytes) -> bytes:
        body = struct.pack(">II", len(value), name_off) + value
        body += b"\x00" * (-len(body) % 4)
        return struct.pack(">I", 3) + body

    struct_block = (
        struct.pack(">I", 1)
        + b"\x00\x00\x00\x00"
        + prop(0, model + b"\x00")
        + struct.pack(">I", 2)
        + struct.pack(">I", 9)
    )
    header_size = 40
    rsv = b"\x00" * 16
    off_struct = header_size + len(rsv)
    off_strings = off_struct + len(struct_block)
    total = off_strings + len(strings)
    header = struct.pack(
        ">10I",
        0xD00DFEED, total, off_struct, off_strings, header_size, 17, 16, 0,
        len(strings), len(struct_block),
    )
    return header + rsv + struct_block + strings

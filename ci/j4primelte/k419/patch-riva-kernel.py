#!/usr/bin/env python3
"""Apply a narrowly scoped compile fix to the pinned Riva 4.19 source."""
from __future__ import annotations

import sys
from pathlib import Path

BROKEN = (
    '\t\t\t\t"%s:failed:client_handle=0x%x, vote=%d,\n'
    '\t\t\t\terr=%d\\n", __func__,\n'
)
FIXED = (
    '\t\t\t\t"%s:failed:client_handle=0x%x, vote=%d, err=%d\\n", __func__,\n'
)


def patch(tree: Path) -> None:
    path = tree / "drivers/crypto/msm/ice.c"
    if not path.is_file():
        raise SystemExit(f"missing pinned Riva ICE source: {path}")
    text = path.read_text()
    occurrences = text.count(BROKEN)
    if occurrences == 1:
        text = text.replace(BROKEN, FIXED, 1)
        path.write_text(text)
    elif occurrences == 0 and text.count(FIXED) == 1:
        pass  # Allow a repeated invocation, while still verifying the result.
    else:
        raise SystemExit(f"expected exactly one known malformed ICE log string, found {occurrences}")
    print(f"patched compile-only ICE diagnostic string: {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <kernel-tree>")
    patch(Path(sys.argv[1]))

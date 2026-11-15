#!/usr/bin/env python3
"""Derive a J4+ kernel command line for the Arch Linux ARM root partition."""
from __future__ import annotations

import sys
from pathlib import Path

HEADER_SIZE = 1632
CMDLINE_SIZE = 512
ROOT_LABEL = "ARCHROOT"


def command_line(image: bytes, root_label: str = ROOT_LABEL) -> str:
    if not root_label or not all(ch.isalnum() or ch in "_-" for ch in root_label):
        raise ValueError("root filesystem label contains unsupported characters")
    if len(image) < HEADER_SIZE or image[:8] != b"ANDROID!":
        raise ValueError("not a complete Android v0 boot image")
    primary = image[64:64 + CMDLINE_SIZE].split(b"\0", 1)[0]
    extra = image[64 + CMDLINE_SIZE + 32:64 + CMDLINE_SIZE + 32 + CMDLINE_SIZE].split(b"\0", 1)[0]
    try:
        original = (primary + b" " + extra).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("boot header command line is not ASCII") from exc

    kept: list[str] = []
    for token in original.split():
        key = token.split("=", 1)[0]
        if (key in {"root", "rootfstype", "rootflags", "init", "rdinit", "quiet", "splash"}
                or key.startswith(("pmos.", "pmos_", "plymouth.", "rd.systemd.", "systemd."))):
            continue
        kept.append(token)

    if not any(token.startswith("console=") for token in kept):
        kept.append("console=tty0")
    kept.extend((f"root=LABEL={root_label}", "rootfstype=ext4", "rootwait", "rw"))
    result = " ".join(kept)
    if len(result.encode("ascii")) >= CMDLINE_SIZE:
        raise ValueError(f"Arch Linux ARM command line too long ({len(result)} >= {CMDLINE_SIZE})")
    if not any(token == f"root=LABEL={root_label}" for token in result.split()):
        raise AssertionError("Arch root label missing from command line")
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(f"usage: {sys.argv[0]} donor-boot.img [root-label]")
    try:
        label = sys.argv[2] if len(sys.argv) == 3 else ROOT_LABEL
        print(command_line(Path(sys.argv[1]).read_bytes(), label))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"arch-root-cmdline: {exc}")

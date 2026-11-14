#!/usr/bin/env python3
"""Make a bounded command line for the standalone systemd 249/legacy-cgroup test."""
from __future__ import annotations
import sys
from pathlib import Path

CMDLINE_MAX = 512
HEADER_SIZE = 1632


def command_line(image: bytes) -> str:
    if len(image) < HEADER_SIZE or image[:8] != b"ANDROID!":
        raise ValueError("not a complete Android v0 boot image")
    primary = image[64:64 + CMDLINE_MAX].split(b"\0", 1)[0]
    extra_off = 64 + CMDLINE_MAX + 32
    extra = image[extra_off:extra_off + CMDLINE_MAX].split(b"\0", 1)[0]
    try:
        original = (primary + b" " + extra).decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("boot header command line is not ASCII") from exc

    kept: list[str] = []
    for token in original.split():
        key = token.split("=", 1)[0]
        if (key in {"root", "rootfstype", "rootflags", "init", "rdinit", "quiet", "splash"}
                or key.startswith("pmos.") or key.startswith("pmos_")
                or key.startswith("plymouth.") or key.startswith("rd.systemd.")
                or key.startswith("systemd.")):
            continue
        kept.append(token)

    if not any(token.startswith("console=") for token in kept):
        kept.append("console=tty0")
    kept.extend(("rd.systemd.unit=emergency.target", "systemd.gpt_auto=0",
                 "systemd.unified_cgroup_hierarchy=0", "rd.fstab=no",
                 "systemd.show_status=1"))
    result = " ".join(kept)
    if len(result.encode("ascii")) >= CMDLINE_MAX:
        raise ValueError(f"debug command line too long ({len(result)} >= {CMDLINE_MAX})")
    if any(token.split("=", 1)[0] in {"root", "init", "rdinit"}
           for token in result.split()):
        raise AssertionError("root/init override survived filtering")
    if "systemd.unified_cgroup_hierarchy=0" not in result.split():
        raise AssertionError("legacy cgroup mode was not selected")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} donor-boot.img")
    try:
        print(command_line(Path(sys.argv[1]).read_bytes()))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"debug-cmdline: {exc}")

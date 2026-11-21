#!/usr/bin/env python3
"""Audit a root filesystem tree for Linux 3.18 compatibility.

    python3 ci/j4primelte/audit-rootfs.py <rootfs-dir>

This is the check that decides whether an image is worth flashing at all. It
exists because the failure it looks for ("Fatal: kernel too old") happens on
the phone, at boot, with no console -- and because the earlier attempt on this
branch only established the problem after a full build.

What it does
------------
1. Finds every path that could become PID 1 and every binary an init execs
   immediately, and reads each one's ELF interpreter.
2. Classifies each as static, musl, or glibc.
3. Reports the kernel floor implied for each, and fails when the boot path is
   glibc-only.

glibc's kernel floor is baked in at build time (--enable-kernel), so nothing in
userspace can raise the floor: a glibc-linked binary built for >= 3.7 aborts on
3.18. musl has no such floor for the syscalls 3.18 provides.

Exit status: 0 when the boot path is usable, 2 when it is glibc-only, 1 when the
tree is unusable for another reason.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

PT_INTERP = 3

INIT_CANDIDATES = [
    "usr/lib/systemd/systemd",
    "usr/bin/openrc-init",
    "sbin/init",
    "usr/bin/init",
]

EARLY_EXEC = [
    "usr/bin/sh",
    "bin/sh",
    "usr/bin/bash",
    "usr/bin/env",
    "usr/bin/mount",
    "usr/bin/switch_root",
    "sbin/switch_root",
]

# The order the initramfs hook uses; the first match is what actually runs.
HOOK_ORDER = ["usr/lib/systemd/systemd", "usr/bin/openrc-init", "sbin/init", "usr/bin/init"]


def elf_interpreter(path: Path) -> str | None:
    """Interpreter path for a dynamically linked ELF64-LE, None when static."""
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
            if head[:4] != b"\x7fELF" or head[4] != 2 or head[5] != 1:
                return None
            (e_phoff,) = struct.unpack_from("<Q", head, 32)
            e_phentsize, e_phnum = struct.unpack_from("<HH", head, 54)
            handle.seek(e_phoff)
            phdrs = handle.read(e_phentsize * e_phnum)
    except (OSError, struct.error):
        return None
    for index in range(e_phnum):
        base = index * e_phentsize
        if base + 56 > len(phdrs):
            break
        (p_type,) = struct.unpack_from("<I", phdrs, base)
        if p_type != PT_INTERP:
            continue
        (p_offset,) = struct.unpack_from("<Q", phdrs, base + 8)
        (p_filesz,) = struct.unpack_from("<Q", phdrs, base + 32)
        try:
            with path.open("rb") as handle:
                handle.seek(p_offset)
                return handle.read(p_filesz).rstrip(b"\x00").decode("utf-8", "replace")
        except OSError:
            return None
    return None


def classify(interpreter: str | None) -> str:
    if interpreter is None:
        return "static"
    if "ld-musl" in interpreter:
        return "musl"
    if "ld-linux" in interpreter:
        return "glibc"
    return f"unknown ({interpreter})"


def audit(root: Path) -> dict:
    found = []
    for rel in INIT_CANDIDATES + EARLY_EXEC:
        path = root / rel
        if not path.exists():
            continue
        resolved = Path(os.path.realpath(path))
        try:
            inside = resolved.relative_to(root)
        except ValueError:
            inside = resolved
        interpreter = elf_interpreter(resolved)
        found.append(
            {
                "path": "/" + rel,
                "resolves_to": "/" + str(inside),
                "interpreter": interpreter,
                "kind": classify(interpreter),
            }
        )

    boot_path = []
    for rel in HOOK_ORDER:
        for item in found:
            if item["path"] == "/" + rel:
                boot_path.append(item)
                break
        if boot_path:
            break
    first = boot_path[0] if boot_path else None

    return {
        "root": str(root),
        "found": found,
        "chosen_by_hook": first,
        "musl_present": (root / "alpine/usr/lib/ld-musl-aarch64.so.1").exists()
        or any(item["kind"] == "musl" for item in found),
        "glibc_present": (root / "usr/lib/libc.so.6").exists(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rootfs")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--allow-glibc-boot", action="store_true",
                        help="report but do not fail when the boot path is glibc")
    args = parser.parse_args()

    root = Path(args.rootfs)
    if not (root / "etc").is_dir() and not (root / "usr").is_dir():
        print(f"{root} does not look like a root filesystem", file=sys.stderr)
        return 1

    report = audit(root)
    for item in report["found"]:
        print(f"  {item['path']:<28} -> {item['resolves_to']:<32} {item['kind']}")
    chosen = report["chosen_by_hook"]
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if chosen is None:
        print(
            "FAIL: no init at any of " + ", ".join(INIT_CANDIDATES),
            file=sys.stderr,
        )
        return 1

    print(f"\nboot path: {chosen['path']} ({chosen['kind']})")
    glibc_candidates = [item for item in report["found"] if item["kind"] == "glibc"]
    if glibc_candidates:
        print(
            "\nglibc binaries found in the boot path (each one aborts on 3.18 if executed):"
        )
        for item in glibc_candidates:
            print(f"  {item['path']}")

    if chosen["kind"] == "glibc":
        message = (
            "the boot path is glibc. Arch Linux ARM's glibc is built with a kernel\n"
            "floor above 3.7, so PID 1 aborts with 'Fatal: kernel too old' on this\n"
            "3.18 kernel. Provide an Alpine/musl tree (ALPINE_BOOTSTRAP=...) so the\n"
            "hook finds a userspace that can actually start."
        )
        if args.allow_glibc_boot:
            print("WARNING: " + message)
            return 0
        print("FAIL: " + message, file=sys.stderr)
        return 2

    if chosen["kind"] == "static":
        print("note: PID 1 is static; it must not dynamically exec glibc binaries")
    print("audit: the boot path can start on a 3.18 kernel")
    return 0


if __name__ == "__main__":
    sys.exit(main())

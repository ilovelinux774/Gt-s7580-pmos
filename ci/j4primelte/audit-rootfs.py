#!/usr/bin/env python3
"""Audit a root filesystem tree for Linux 3.18 compatibility -- correctly.

    python3 ci/j4primelte/audit-rootfs.py <rootfs-dir> [--kernel 3.18.120]

What this checks, and why it is not "is it musl?"
-------------------------------------------------
The earlier version of this file failed any tree whose PID 1 was glibc, on the
theory that "glibc aborts with 'Fatal: kernel too old' on 3.18". That theory is
wrong for this device, and acting on it produced the Alpine-at-/alpine
contortion this file now exists to prevent.

The real rule:

* glibc refuses to start when ``.note.ABI-tag`` says the kernel must be newer
  than the running one. That note is fixed at build time by
  ``--enable-kernel``, but it can never be *lower* than the architecture
  minimum, which for aarch64 is **3.7.0** (glibc's
  ``sysdeps/unix/sysv/linux/aarch64/configure.ac``) and for arm32 is 3.2.0.
* Arch Linux ARM's glibc PKGBUILD passes no ``--enable-kernel`` at all, so
  ALARM and therefore ARMtix binaries carry ``ABI: 3.7.0``.
* 3.7.0 <= 3.18.120, so every one of those binaries is allowed to start.

What *would* fail is a binary carrying a floor above the phone's kernel -- for
example upstream Arch Linux's glibc, which is built with
``--enable-kernel=4.4``. So the audit reads the note out of the files instead
of guessing from the library name, and reports any file whose floor is above
the kernel this phone runs.

The audit walks the whole tree, not a fixed list of paths, because the
interesting binary is rarely the one you thought of: an init is useless if the
first thing it execs is a 4.4-only helper. Reports, in order of severity:

    FAIL  a file on the boot path has a floor above the kernel
    FAIL  no candidate for PID 1 at all
    WARN  a file links glibc but carries no ABI note (floor unknown)
    WARN  a file has the wrong architecture for the kernel (arm32 vs aarch64)

Exit status: 0 usable, 2 boot path cannot start on this kernel, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from elfnotes import ElfFacts, floor_exceeds, format_version, parse_version, read_elf  # noqa: E402

#: The initramfs hook tries these in this order; the first that exists wins.
HOOK_ORDER = [
    "usr/bin/openrc-init",
    "sbin/openrc-init",
    "usr/lib/systemd/systemd",
    "sbin/init",
    "usr/bin/init",
]

#: Everything else an init is likely to exec before anything it could log about.
EARLY_EXEC = [
    "bin/sh",
    "usr/bin/sh",
    "bin/bash",
    "usr/bin/bash",
    "bin/busybox",
    "usr/bin/env",
    "usr/bin/mount",
    "bin/mount",
    "usr/bin/switch_root",
    "sbin/switch_root",
    "usr/bin/openrc",
    "sbin/openrc",
    "usr/bin/pacman",
    "usr/bin/dropbear",
]

#: Directories that never contain anything the boot path executes.
SKIP_DIRS = {
    "usr/share", "usr/lib/firmware", "usr/lib/modules", "var", "proc", "sys",
    "dev", "run", "tmp", "boot", "usr/lib/locale", "usr/include", "usr/src",
    "lost+found", "usr/lib/systemd/system-preset",
}

MAX_BYTES = 16 * 1024 * 1024


def resolve_inside(root: Path, rel: str) -> Path | None:
    """Resolve a path inside the tree without following it outside the tree.

    Absolute symlinks in the image mean "from the root of the phone", so they
    are resolved manually rather than through os.path.realpath, which would
    chase them into the build host.
    """
    current = root / rel
    seen: set[Path] = set()
    for _ in range(16):
        if current in seen:
            return None
        seen.add(current)
        if not current.is_symlink():
            return current if current.exists() else None
        target = os.readlink(current)
        current = root / target.lstrip("/") if target.startswith("/") else current.parent / target
    return None


def is_elf(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def walk_elves(root: Path):
    """Every ELF file in the tree, skipping the paths that cannot matter."""
    skip = {root / part for part in SKIP_DIRS}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if (base / name) not in skip and not (base / name).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or not is_elf(path):
                continue
            yield path


def audit(root: Path, kernel: tuple[int, int, int], arch: str = "aarch64") -> dict:
    if not any((root / d).is_dir() for d in ("etc", "usr", "bin", "sbin")):
        raise SystemExit(f"{root} does not look like a root filesystem")

    found: list[dict] = []
    for path in walk_elves(root):
        facts: ElfFacts = read_elf(path)
        rel = "/" + str(path.relative_to(root))
        found.append(
            {
                "path": rel,
                "kind": facts.kind,
                "interpreter": facts.interpreter,
                "abi_floor": facts.floor_text,
                "machine": facts.machine,
                "bits": facts.bits,
                "refuses_this_kernel": floor_exceeds(facts.abi_floor, kernel),
                "wrong_arch": bool(facts.machine) and facts.machine != arch,
            }
        )

    by_path = {item["path"]: item for item in found}

    chosen = None
    for rel in HOOK_ORDER:
        candidate = resolve_inside(root, rel)
        if candidate is None:
            continue
        resolved = "/" + str(candidate.relative_to(root)) if candidate.is_relative_to(root) else str(candidate)
        item = by_path.get(resolved) or by_path.get("/" + rel)
        if item is None:
            item = {
                "path": "/" + rel,
                "kind": "script or non-ELF",
                "interpreter": None,
                "abi_floor": "unknown",
                "machine": "",
                "bits": 0,
                "refuses_this_kernel": False,
                "wrong_arch": False,
            }
        chosen = dict(item, hook_path="/" + rel, resolves_to=resolved)
        break

    boot_path = []
    for rel in EARLY_EXEC:
        candidate = resolve_inside(root, rel)
        if candidate is None or not candidate.is_relative_to(root):
            continue
        item = by_path.get("/" + str(candidate.relative_to(root)))
        if item:
            boot_path.append(item)

    refusing = sorted(
        (item for item in found if item["refuses_this_kernel"]),
        key=lambda item: item["path"],
    )
    unknown_floor = sorted(
        (item for item in found if item["kind"] == "glibc" and item["abi_floor"] == "unknown"),
        key=lambda item: item["path"],
    )
    wrong_arch = sorted(
        (item for item in found if item["wrong_arch"]), key=lambda item: item["path"]
    )
    boot_refusing = [
        item for item in refusing
        if chosen and item["path"] in (chosen["path"], chosen["resolves_to"])
    ] + [item for item in refusing if item in boot_path]

    return {
        "root": str(root),
        "kernel": format_version(kernel),
        "arch": arch,
        "files_scanned": len(found),
        "chosen_by_hook": chosen,
        "boot_path": boot_path,
        "refuses_this_kernel": refusing,
        "boot_path_refuses": boot_refusing,
        "unknown_floor_glibc": unknown_floor,
        "wrong_arch": wrong_arch,
        "glibc_files": sum(1 for item in found if item["kind"] == "glibc"),
        "musl_files": sum(1 for item in found if item["kind"] == "musl"),
        "static_files": sum(1 for item in found if item["kind"] == "static"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rootfs")
    parser.add_argument("--kernel", default="3.18.120",
                        help="kernel the phone runs (default 3.18.120)")
    parser.add_argument("--arch", default="aarch64", choices=["aarch64", "arm32"])
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--allow-glibc-boot", action="store_true",
                        help="kept for compatibility; the boot path may be glibc")
    args = parser.parse_args()

    root = Path(args.rootfs)
    kernel = parse_version(args.kernel)
    report = audit(root, kernel, args.arch)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(f"kernel {report['kernel']} / {report['arch']}: scanned "
          f"{report['files_scanned']} ELF files "
          f"({report['glibc_files']} glibc, {report['musl_files']} musl, "
          f"{report['static_files']} static)")

    chosen = report["chosen_by_hook"]
    if chosen is None:
        print("FAIL: no candidate for PID 1 at any of " + ", ".join(HOOK_ORDER),
              file=sys.stderr)
        return 1
    print(f"boot path: {chosen['hook_path']} -> {chosen['resolves_to']} "
          f"({chosen['kind']}, ABI floor {chosen['abi_floor']})")

    if report["refuses_this_kernel"]:
        print(f"\nFAIL: {len(report['refuses_this_kernel'])} files refuse to start on "
              f"{report['kernel']} (ABI floor above the kernel):")
        for item in report["refuses_this_kernel"][:20]:
            print(f"  {item['path']:<60} {item['kind']:<8} floor {item['abi_floor']}")
        if len(report["refuses_this_kernel"]) > 20:
            print(f"  ... and {len(report['refuses_this_kernel']) - 20} more")
        print(
            "\nThis is the 'Fatal: kernel too old' case. It means the tree was built\n"
            "against a glibc with a higher --enable-kernel (upstream Arch Linux uses\n"
            "4.4), not that glibc itself is unusable here.",
            file=sys.stderr,
        )
        return 2

    if report["unknown_floor_glibc"]:
        print(f"\nWARN: {len(report['unknown_floor_glibc'])} glibc files carry no ABI note;"
              " their floor cannot be read (stripped abnormally?)")
        for item in report["unknown_floor_glibc"][:10]:
            print(f"  {item['path']}")

    if report["wrong_arch"]:
        print(f"\nWARN: {len(report['wrong_arch'])} files are not {report['arch']}:")
        for item in report["wrong_arch"][:10]:
            print(f"  {item['path']} ({item['machine']})")

    print(f"\naudit: the boot path can start on {report['kernel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

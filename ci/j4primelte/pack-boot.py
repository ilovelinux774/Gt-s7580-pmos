#!/usr/bin/env python3
"""Pack a J4+ BOOT image that boots the Artix userspace.

The invariant of this script is that *nothing about the kernel changes*. The
kernel payload (arm64 Image.gz + appended Rev06 DTB) is copied verbatim out of
the reference image that reached the initramfs debug shell on the phone; only
the ramdisk and the command line are replaced. Everything else in the header --
page size, load addresses, header version -- is taken from that same image.

    python3 ci/j4primelte/pack-boot.py \
        --reference  medusa64-fbdev-parent-debug.img \
        --initramfs  rootswitch-initramfs.gz \
        --out        boot.img \
        --root-label ARTIXROOT

Run ``j4boot.py inspect`` on the output afterwards (this script does it too).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import j4boot  # noqa: E402


def read_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.strip().strip("'\"")
    return pins


def build_cmdline(reference_cmdline: str, root_label: str, console: str,
                  extra: list[str]) -> str:
    """Keep the verified console options, replace the pmOS root handoff.

    The reference command line is preserved verbatim except for the postmarketOS
    specific tokens, because those options are part of what made the display
    path survive: ``console=ttyMSM0`` keeps serial logging, ``console=tty0``
    plus ``fbcon=map:0,scrollback:256k`` is how the panel is driven, and
    ``consoleblank=0`` stops the VT blanking the panel.
    """
    keep = []
    for token in reference_cmdline.split():
        if token.startswith("pmos_root_uuid=") or token.startswith("pmos_rootfsopts="):
            continue
        if token == "pmos.debug-shell":
            continue
        keep.append(token)
    keep.append(f"root=LABEL={root_label}")
    keep += ["rootfstype=ext4", "rootwait"]
    # console= is repeatable on purpose: the kernel mirrors output to every
    # console= it is given, and the reference image relies on both ttyMSM0 and
    # tty0. So only an exact duplicate is dropped, and only a *missing* console
    # is added.
    console_token = f"console={console}" if "=" in console else console
    if not any(token.startswith("console=") for token in keep):
        keep.append(console_token)
    seen: set[str] = set()
    deduped = []
    for token in keep + list(extra):
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return " ".join(deduped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", required=True,
                        help="BOOT image that booted on the phone (kernel source)")
    parser.add_argument("--initramfs", required=True, help="gzip initramfs to install")
    parser.add_argument("--out", required=True)
    parser.add_argument("--root-label", default="ARTIXROOT")
    parser.add_argument("--console", default="ttyMSM0,115200n8")
    parser.add_argument("--extra-cmdline", action="append", default=[],
                        help="append raw tokens (repeatable)")
    parser.add_argument("--pins", default=str(HERE / "pins.env"))
    parser.add_argument("--allow-unpinned-kernel", action="store_true")
    args = parser.parse_args()

    pins = read_pins(Path(args.pins))
    reference = Path(args.reference).read_bytes()
    image = j4boot.unpack_image(reference, source=args.reference)

    if not args.allow_unpinned_kernel:
        expected = pins["J4_KERNEL_PAYLOAD_SHA256"]
        actual = j4boot.sha256(image.kernel)
        if actual != expected:
            print(
                f"FAIL: kernel payload {actual} != pinned {expected}\n"
                "      This is not the kernel that booted on the phone. Refusing to pack.",
                file=sys.stderr,
            )
            return 1

    ramdisk = Path(args.initramfs).read_bytes()
    if ramdisk[:2] != b"\x1f\x8b":
        print("FAIL: the ramdisk must be a plain gzip stream for this bootloader",
              file=sys.stderr)
        return 2

    cmdline = build_cmdline(
        image.header.full_cmdline(), args.root_label, args.console, args.extra_cmdline
    )
    packed = j4boot.build_image(image.header, image.kernel, ramdisk, cmdline=cmdline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(packed)

    result = j4boot.unpack_image(packed, source=str(out))
    problems: list[str] = []
    if result.kernel != image.kernel:
        problems.append("kernel payload changed while packing")
    if result.ramdisk != ramdisk:
        problems.append("ramdisk payload changed while packing")
    for field in ("page_size", "header_version", "kernel_addr", "ramdisk_addr",
                  "second_addr", "tags_addr"):
        if getattr(result.header, field) != getattr(image.header, field):
            problems.append(f"{field} changed while packing")
    if len(packed) > int(pins.get("J4_BOOT_PARTITION_BYTES", 33554432)):
        problems.append(
            f"image is {len(packed)} bytes, over the "
            f"{pins.get('J4_BOOT_PARTITION_BYTES')} byte BOOT partition"
        )

    report = result.report()
    report["root_label"] = args.root_label
    info = out.with_suffix(out.suffix + ".json")
    info.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"pack-boot: wrote {out} ({len(packed)} bytes)")
    print(f"pack-boot: sha256 {j4boot.sha256(packed)}")
    print(f"pack-boot: cmdline {cmdline}")
    print(f"pack-boot: details in {info}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

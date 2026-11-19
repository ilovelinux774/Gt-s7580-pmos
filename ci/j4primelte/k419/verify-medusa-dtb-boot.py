#!/usr/bin/env python3
"""Verify a J4 debug BOOT image using the extracted Medusa appended-DTB layout."""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "k510"))
from qcdt import parse_root_cells  # noqa: E402

ANDROID_MAGIC = b"ANDROID!"
FDT_MAGIC = 0xD00DFEED
EXPECTED = {
    "page_size": 2048,
    "kernel_addr": 0x80080000,
    "ramdisk_addr": 0x83600000,
    "second_addr": 0x80F00000,
    "tags_addr": 0x83400000,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boot", type=Path)
    parser.add_argument("dtb", type=Path)
    parser.add_argument("--expect-kernel-version", help="require this Linux version string in the gzip image")
    parser.add_argument("--root-label", help="verify a production root-switch image instead of a rootless debug image")
    args = parser.parse_args()
    try:
        image = args.boot.read_bytes()
        dtb = args.dtb.read_bytes()
        if len(image) < 1632 or image[:8] != ANDROID_MAGIC:
            raise ValueError("not a complete Android v0 boot image")
        kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size, second_addr, tags_addr, page_size, dt_size = struct.unpack_from("<9I", image, 8)
        actual = {
            "page_size": page_size,
            "kernel_addr": kernel_addr,
            "ramdisk_addr": ramdisk_addr,
            "second_addr": second_addr,
            "tags_addr": tags_addr,
        }
        if actual != EXPECTED:
            raise ValueError(f"header does not match the known-booting Medusa image: {actual}")
        if second_size != 0 or dt_size != 0:
            raise ValueError(f"expected second_size=0 and dt_size=0, got {second_size}, {dt_size}")
        if len(dtb) < 40 or struct.unpack_from(">I", dtb)[0] != FDT_MAGIC:
            raise ValueError("DTB has no FDT magic")
        if struct.unpack_from(">I", dtb, 4)[0] != len(dtb):
            raise ValueError("DTB file length does not match FDT totalsize")
        ids = parse_root_cells(dtb)
        if ids["qcom,board-id"] != [8, 6] or ids["qcom,pmic-id"] != [0x10019, 0, 0, 0]:
            raise ValueError(f"DTB does not identify J4 Rev06 / expected PMIC: {ids}")

        kernel_off = page_size
        ramdisk_off = kernel_off + (kernel_size + page_size - 1) // page_size * page_size
        ramdisk_end = ramdisk_off + ramdisk_size
        if ramdisk_end > len(image):
            raise ValueError("boot image is truncated before the ramdisk ends")
        kernel = image[kernel_off:kernel_off + kernel_size]
        if not kernel.endswith(dtb):
            raise ValueError("the Medusa DTB is not appended to the kernel payload")
        gzip_kernel = kernel[:-len(dtb)]
        decoder = zlib.decompressobj(31)
        uncompressed = decoder.decompress(gzip_kernel) + decoder.flush()
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("kernel payload is not exactly gzip Image followed by the appended DTB")
        if args.expect_kernel_version:
            marker = f"Linux version {args.expect_kernel_version}".encode("ascii")
            if marker not in uncompressed:
                raise ValueError(f"kernel does not contain expected version {args.expect_kernel_version}")

        cmdline = image[64:64 + 512].split(b"\0", 1)[0].decode("ascii")
        tokens = cmdline.split()
        if args.root_label:
            if not args.root_label or not all(c.isalnum() or c in "_-" for c in args.root_label):
                raise ValueError(f"invalid expected root label: {args.root_label!r}")
            forbidden = ("rootflags=", "init=", "rdinit=", "pmos_", "pmos.", "systemd.", "rd.systemd.")
            if any(token.startswith(forbidden) for token in tokens):
                raise ValueError(f"production image has a forbidden root/init override: {cmdline}")
            expected_root = f"root=LABEL={args.root_label}"
            roots = [token for token in tokens if token.startswith("root=")]
            if roots != [expected_root]:
                raise ValueError(f"expected only {expected_root}: {cmdline}")
            for required in ("rootfstype=ext4", "rootwait", "rw"):
                if required not in tokens:
                    raise ValueError(f"production image is missing {required}: {cmdline}")
        else:
            forbidden = ("root=", "rootfstype=", "rootflags=", "init=", "rdinit=", "pmos_", "pmos.")
            if any(token.startswith(forbidden) for token in tokens):
                raise ValueError(f"debug image has a root/init override: {cmdline}")
        if not any(token.startswith("console=") for token in tokens):
            raise ValueError("image has no kernel console")
        print(f"OK: {args.boot} ({len(image)} bytes)")
        print(f"  page={page_size}, kernel=0x{kernel_addr:08x}, ramdisk=0x{ramdisk_addr:08x}, tags=0x{tags_addr:08x}")
        print(f"  appended Medusa J4 Rev06 DTB: {len(dtb)} bytes; ids={ids}")
        print(f"  cmdline: {cmdline}")
        return 0
    except (OSError, ValueError, UnicodeDecodeError, struct.error) as exc:
        print(f"verify-medusa-dtb-boot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

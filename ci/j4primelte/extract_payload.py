#!/usr/bin/env python3
"""Extract the pinned kernel payload and Rev06 DTB out of the reference BOOT image.

The reference image is the only copy of the 3.18.120 Medusa kernel this project
has, so it is published as a release asset rather than committed (see
``ci/j4primelte/pins.env`` for the URL and SHA-256). Packing jobs download it,
run this script, and then build new BOOT images against the extracted,
hash-checked kernel payload -- never against a fresh compile of the kernel tree.

    python3 ci/j4primelte/extract_payload.py medusa64-fbdev-parent-debug.img outdir/

Writes ``outdir/kernel.payload`` (gzip Image + appended DTB), ``outdir/Image.gz``,
``outdir/appended.dtb`` and ``outdir/inspect.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import j4boot  # noqa: E402


def read_pin(env_file: Path, key: str) -> str:
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip("'\"")
    raise SystemExit(f"pin {key} not found in {env_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="reference BOOT image that booted on the phone")
    parser.add_argument("out_dir")
    parser.add_argument(
        "--pins", default=str(HERE / "pins.env"), help="file holding the expected hashes"
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="skip the hash comparison (useful when first recording the pins)",
    )
    args = parser.parse_args()

    pins = Path(args.pins)
    data = Path(args.image).read_bytes()
    image = j4boot.unpack_image(data, source=args.image)
    raw_kernel, trailing = image.kernel_parts()
    dtb = image.appended_dtb()

    if not args.allow_unpinned:
        checks = [
            ("J4_KERNEL_PAYLOAD_SHA256", j4boot.sha256(image.kernel), "kernel payload"),
            ("J4_KERNEL_IMAGE_GZ_SHA256", j4boot.sha256(raw_kernel), "kernel gzip stream"),
            ("J4_DTB_SHA256", j4boot.sha256(dtb), "appended DTB"),
        ]
        problems = []
        for key, actual, label in checks:
            expected = read_pin(pins, key)
            if actual != expected:
                problems.append(f"{label}: {actual} != pinned {expected}")
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            return 1
        print("extract_payload: all hashes match ci/j4primelte/pins.env")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kernel.payload").write_bytes(image.kernel)
    (out / "Image.gz").write_bytes(raw_kernel)
    (out / "appended.dtb").write_bytes(dtb)
    (out / "ramdisk.gz").write_bytes(image.ramdisk)
    report = image.report()
    report["extracted_dtb_sha256"] = hashlib.sha256(dtb).hexdigest()
    (out / "inspect.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    shutil.copyfile(pins, out / "pins.env")
    print(f"extract_payload: wrote {out}/kernel.payload ({len(image.kernel)} bytes)")
    print(f"extract_payload: wrote {out}/appended.dtb ({len(dtb)} bytes)")
    print(f"extract_payload: wrote {out}/ramdisk.gz ({len(image.ramdisk)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

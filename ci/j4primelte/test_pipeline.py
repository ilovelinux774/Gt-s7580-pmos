#!/usr/bin/env python3
"""End-to-end checks for the BOOT-packing pipeline.

    python3 ci/j4primelte/test_pipeline.py

Two modes:

* without ``J4_REFERENCE_BOOT`` a synthetic reference image is built from a
  fake arm64 kernel and DTB, and the whole chain is exercised on it;
* with ``J4_REFERENCE_BOOT`` the same chain runs on the *real* image, and the
  resulting BOOT image is checked against the pinned kernel/DTB hashes.

Either way the assertions are the ones that matter: the kernel payload is
untouched, the layout is unchanged, the ramdisk is the new one, the command
line carries the root handoff, and the image fits the BOOT partition.
"""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import j4boot  # noqa: E402
import pack_boot_helpers as helpers  # noqa: E402  (see bottom of file)


def gz(payload: bytes) -> bytes:
    obj = zlib.compressobj(9, zlib.DEFLATED, 31)
    return obj.compress(payload) + obj.flush()


def fake_kernel() -> bytes:
    image = bytearray(0x4000)
    image[0:4] = b"MZ\x00\x91"
    struct.pack_into("<Q", image, 8, 0x80000)
    struct.pack_into("<Q", image, 16, 0x4000)
    image[56:60] = b"ARMd"
    banner = b"Linux version 3.18.120-j4-medusa64-debug+ (fake) #1 SMP PREEMPT\n\x00"
    image[0x300 : 0x300 + len(banner)] = banner
    return gz(bytes(image)) + helpers.minimal_dtb(b"Samsung J4PRIMELTE SEA OPEN PROJECT Rev06")


def make_reference(path: Path) -> bytes:
    template = j4boot.Header(
        kernel_size=0, kernel_addr=0x80080000, ramdisk_size=0,
        ramdisk_addr=0x83600000, second_size=0, second_addr=0x80F00000,
        tags_addr=0x83400000, page_size=2048, header_version=0, os_version=0,
        name=b"\x00" * 16,
        cmdline=(
            b"console=ttyMSM0,115200n8 console=tty0 loglevel=8 "
            b"fbcon=map:0,scrollback:256k consoleblank=0 "
            b"pmos_root_uuid=86e7262b-b628-4b89-8a6e-e96ef9b1aab6 "
            b"pmos_rootfsopts=defaults pmos.debug-shell"
        ).ljust(512, b"\x00"),
        image_id=b"\x00" * 32, extra_cmdline=b"\x00" * 1024,
    )
    ramdisk = gz(b"pretend-pmos-initramfs" * 100)
    data = j4boot.build_image(template, fake_kernel(), ramdisk)
    path.write_bytes(data)
    return data


class SyntheticPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.reference = self.tmp / "reference.img"
        self.reference_data = make_reference(self.reference)
        self.initramfs = self.tmp / "initramfs.gz"
        self.initramfs.write_bytes(gz(b"new-initramfs" * 50))
        self.out = self.tmp / "boot.img"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pack(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable, str(HERE / "pack-boot.py"),
                "--reference", str(self.reference),
                "--initramfs", str(self.initramfs),
                "--out", str(self.out),
                *extra,
            ],
            capture_output=True, text=True,
        )

    def test_pack_keeps_the_kernel_byte_identical(self) -> None:
        result = self.run_pack("--allow-unpinned-kernel")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = j4boot.unpack_image(self.reference_data)
        after = j4boot.unpack_image(self.out.read_bytes())
        self.assertEqual(after.kernel, before.kernel)
        self.assertEqual(after.appended_dtb(), before.appended_dtb())

    def test_pack_keeps_the_boot_layout(self) -> None:
        self.run_pack("--allow-unpinned-kernel")
        before = j4boot.parse_header(self.reference_data)
        after = j4boot.parse_header(self.out.read_bytes())
        for field in ("page_size", "header_version", "kernel_addr", "ramdisk_addr",
                      "second_addr", "tags_addr"):
            self.assertEqual(getattr(after, field), getattr(before, field), field)

    def test_pack_installs_the_new_ramdisk(self) -> None:
        self.run_pack("--allow-unpinned-kernel")
        after = j4boot.unpack_image(self.out.read_bytes())
        self.assertEqual(after.ramdisk, self.initramfs.read_bytes())

    def test_cmdline_drops_pmos_tokens_and_adds_the_root_handoff(self) -> None:
        self.run_pack("--allow-unpinned-kernel")
        cmdline = j4boot.unpack_image(self.out.read_bytes()).header.full_cmdline()
        self.assertNotIn("pmos_root_uuid", cmdline)
        self.assertNotIn("pmos.debug-shell", cmdline)
        self.assertIn("root=LABEL=ARCHROOT", cmdline)
        self.assertIn("rootfstype=ext4", cmdline)
        self.assertIn("rootwait", cmdline)
        # the console options are part of what makes the panel path survive
        self.assertIn("console=ttyMSM0,115200n8", cmdline)
        self.assertIn("console=tty0", cmdline)
        self.assertIn("fbcon=map:0,scrollback:256k", cmdline)
        self.assertIn("consoleblank=0", cmdline)

    def test_refuses_a_kernel_that_is_not_the_pinned_one(self) -> None:
        result = self.run_pack()  # no --allow-unpinned-kernel, fake kernel
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the kernel that booted", result.stderr)
        self.assertFalse(self.out.exists())

    def test_refuses_a_non_gzip_ramdisk(self) -> None:
        self.initramfs.write_bytes(b"070701-fake-cpio")
        result = self.run_pack("--allow-unpinned-kernel")
        self.assertEqual(result.returncode, 2)
        self.assertIn("plain gzip", result.stderr)

    def test_localised_cmdline_override(self) -> None:
        result = self.run_pack("--allow-unpinned-kernel", "--root-label", "OTHER",
                               "--extra-cmdline", "loglevel=7")
        self.assertEqual(result.returncode, 0, result.stderr)
        cmdline = j4boot.unpack_image(self.out.read_bytes()).header.full_cmdline()
        self.assertIn("root=LABEL=OTHER", cmdline)
        self.assertIn("loglevel=7", cmdline)

    def test_report_is_written_next_to_the_image(self) -> None:
        self.run_pack("--allow-unpinned-kernel")
        report = json.loads(self.out.with_suffix(self.out.suffix + ".json").read_text())
        self.assertEqual(report["root_label"], "ARCHROOT")
        self.assertEqual(report["kernel_payload_sha256"],
                         j4boot.sha256(j4boot.unpack_image(self.reference_data).kernel))


@unittest.skipUnless(os.environ.get("J4_REFERENCE_BOOT"), "J4_REFERENCE_BOOT not set")
class RealReferencePipelineTests(unittest.TestCase):
    """The same chain, but against the image that booted on the phone."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.reference = Path(os.environ["J4_REFERENCE_BOOT"])
        data = self.reference.read_bytes()
        image = j4boot.unpack_image(data)
        self.ramdisk = self.tmp / "pmos-initramfs.gz"
        self.ramdisk.write_bytes(image.ramdisk)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uncpio_roundtrip_and_rootswitch_build(self) -> None:
        tree = self.tmp / "initramfs"
        subprocess.run(
            [sys.executable, str(HERE / "uncpio.py"), str(self.ramdisk), str(tree)],
            check=True, capture_output=True,
        )
        self.assertTrue((tree / "init").exists())
        self.assertTrue((tree / "bin").is_symlink())
        out = self.tmp / "rootswitch.gz"
        subprocess.run(
            [sys.executable, str(HERE / "make_rootswitch_initramfs.py"),
             str(tree), str(out), "--root-label", "ARCHROOT"],
            check=True, capture_output=True,
        )
        self.assertTrue((out.parent / (out.name + ".root") / "hooks-extra"
                         / "99-j4-root-switch.sh").exists())

    def test_full_pack_on_the_real_reference(self) -> None:
        tree = self.tmp / "initramfs"
        subprocess.run([sys.executable, str(HERE / "uncpio.py"),
                        str(self.ramdisk), str(tree)], check=True, capture_output=True)
        rootswitch = self.tmp / "rootswitch.gz"
        subprocess.run([sys.executable, str(HERE / "make_rootswitch_initramfs.py"),
                        str(tree), str(rootswitch)], check=True, capture_output=True)
        out = self.tmp / "boot.img"
        result = subprocess.run(
            [sys.executable, str(HERE / "pack-boot.py"),
             "--reference", str(self.reference), "--initramfs", str(rootswitch),
             "--out", str(out)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.with_suffix(".img.json").read_text())
        self.assertEqual(report["kernel_payload_sha256"], j4boot.REFERENCE["kernel_sha256"])
        self.assertEqual(report["dtb_sha256"], j4boot.REFERENCE["dtb_sha256"])
        self.assertLess(report["size"], 33554432)
        verify = subprocess.run(
            [sys.executable, str(HERE / "j4boot.py"), "verify", str(out),
             "--expect-kernel-version", "3.18.120-j4-medusa64-debug+",
             "--expect-dtb", "--require-cmdline", "root=LABEL=ARCHROOT",
             "--forbid-cmdline", "pmos_root_uuid"],
            capture_output=True, text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

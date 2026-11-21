#!/usr/bin/env python3
"""Tests for ci/j4primelte/j4boot.py.

Run with:  python3 ci/j4primelte/test_j4boot.py

The *real* reference image is not in the repository (it is a 17.5 MB binary
published on the j4-medusa64-fbdev-parent-debug-1 release). Point
``J4_REFERENCE_BOOT`` at a local copy to also run the byte-exact assertions:

    J4_REFERENCE_BOOT=/path/to/boot.img python3 ci/j4primelte/test_j4boot.py
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import j4boot  # noqa: E402


def gz(payload: bytes) -> bytes:
    obj = zlib.compressobj(9, zlib.DEFLATED, 31)
    return obj.compress(payload) + obj.flush()


def minimal_dtb(model: bytes = b"Test Board") -> bytes:
    """A tiny but structurally valid flattened device tree with a root model."""

    strings = b"model\x00"
    def prop(name_off: int, value: bytes) -> bytes:
        body = struct.pack(">II", len(value), name_off) + value
        body += b"\x00" * (-len(body) % 4)
        return struct.pack(">I", 3) + body

    struct_block = (
        struct.pack(">I", 1)
        + b"\x00\x00\x00\x00"  # root node, empty name, padded to 4
        + prop(0, model + b"\x00")
        + struct.pack(">I", 2)
        + struct.pack(">I", 9)
    )
    header_size = 40
    off_rsv = header_size
    rsv = b"\x00" * 16
    off_struct = off_rsv + len(rsv)
    off_strings = off_struct + len(struct_block)
    total = off_strings + len(strings)
    header = struct.pack(
        ">10I",
        0xD00DFEED, total, off_struct, off_strings, off_rsv, 17, 16, 0,
        len(strings), len(struct_block),
    )
    return header + rsv + struct_block + strings


def sample_kernel() -> bytes:
    image = bytearray(0x1000)
    image[0:4] = b"MZ\x00\x91"
    struct.pack_into("<Q", image, 8, 0x80000)
    struct.pack_into("<Q", image, 16, 0x1000)
    image[56:60] = b"ARMd"
    banner = b"Linux version 3.18.120-test+ (ci) #1 SMP PREEMPT\n\x00"
    image[0x200 : 0x200 + len(banner)] = banner
    return gz(bytes(image)) + minimal_dtb()


class HeaderTests(unittest.TestCase):
    def test_pack_parse_roundtrip(self) -> None:
        header = j4boot.Header(
            kernel_size=100,
            kernel_addr=0x80080000,
            ramdisk_size=50,
            ramdisk_addr=0x83600000,
            second_size=0,
            second_addr=0x80F00000,
            tags_addr=0x83400000,
            page_size=2048,
            header_version=0,
            os_version=0,
            name=b"",
            cmdline=b"console=ttyMSM0 loglevel=8",
            image_id=b"\x11" * 20 + b"\x00" * 12,
            extra_cmdline=b"",
        )
        parsed = j4boot.parse_header(header.raw())
        for field in (
            "kernel_size", "kernel_addr", "ramdisk_size", "ramdisk_addr",
            "second_size", "second_addr", "tags_addr", "page_size",
            "header_version", "os_version", "image_id", "extra_cmdline",
        ):
            actual, expected = getattr(parsed, field), getattr(header, field)
            if isinstance(expected, bytes):
                # parse keeps the raw fixed-size field, packing zero-pads it
                actual, expected = actual.rstrip(b"\x00"), expected.rstrip(b"\x00")
            self.assertEqual(actual, expected, field)
        self.assertEqual(parsed.cmdline_str(), "console=ttyMSM0 loglevel=8")
        self.assertEqual(len(header.raw()), j4boot.HEADER_FIELDS_SIZE)

    def test_rejects_non_android(self) -> None:
        with self.assertRaises(j4boot.BootImageError):
            j4boot.parse_header(b"NOTANDROID" + b"\x00" * 2000)

    def test_image_id_is_sha1_of_payload(self) -> None:
        kernel, ramdisk = b"k" * 10, b"r" * 10
        image_id = j4boot.android_image_id(kernel, ramdisk, b"")
        self.assertEqual(len(image_id), 32)
        self.assertEqual(image_id[20:], b"\x00" * 12)


class ImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = j4boot.Header(
            kernel_size=0, kernel_addr=0x80080000, ramdisk_size=0,
            ramdisk_addr=0x83600000, second_size=0, second_addr=0x80F00000,
            tags_addr=0x83400000, page_size=2048, header_version=0, os_version=0,
            name=b"\x00" * 16, cmdline=b"console=ttyMSM0 loglevel=8" + b"\x00" * 490,
            image_id=b"\x00" * 32, extra_cmdline=b"\x00" * 1024,
        )

    def test_build_unpack_roundtrip(self) -> None:
        kernel, ramdisk = sample_kernel(), gz(b"initramfs-bytes")
        data = j4boot.build_image(self.template, kernel, ramdisk)
        image = j4boot.unpack_image(data)
        self.assertEqual(image.kernel, kernel)
        self.assertEqual(image.ramdisk, ramdisk)
        self.assertEqual(image.header.kernel_addr, 0x80080000)
        self.assertEqual(image.header.ramdisk_addr, 0x83600000)
        self.assertEqual(image.whole_image(), data)

    def test_build_sets_a_matching_header_id(self) -> None:
        kernel, ramdisk = sample_kernel(), gz(b"initramfs")
        data = j4boot.build_image(self.template, kernel, ramdisk)
        image = j4boot.unpack_image(data)
        self.assertEqual(
            image.header.image_id,
            j4boot.android_image_id(kernel, ramdisk, b""),
        )

    def test_payload_sizes_are_page_aligned(self) -> None:
        kernel, ramdisk = sample_kernel(), gz(b"x" * 4000)
        data = j4boot.build_image(self.template, kernel, ramdisk)
        page = self.template.page_size
        self.assertEqual(len(data) % page, 0)
        self.assertEqual(
            len(data), page + j4boot.align(len(kernel), page) + j4boot.align(len(ramdisk), page)
        )

    def test_dtb_split_needs_exactly_one_gzip_stream(self) -> None:
        dtb = minimal_dtb()
        kernel = sample_kernel()
        raw, trailing = j4boot.split_kernel_payload(kernel)
        self.assertEqual(trailing, dtb)
        self.assertEqual(j4boot.gunzip(raw)[56:60], b"ARMd")

    def test_appended_dtb_reports_the_model(self) -> None:
        data = j4boot.build_image(self.template, sample_kernel(), gz(b"r"))
        image = j4boot.unpack_image(data)
        self.assertEqual(j4boot.fdt_root_prop(image.appended_dtb(), "model"), "Test Board")

    def test_missing_dtb_is_reported_not_guessed(self) -> None:
        data = j4boot.build_image(self.template, sample_kernel()[: -len(minimal_dtb())], gz(b"r"))
        image = j4boot.unpack_image(data)
        with self.assertRaises(j4boot.BootImageError):
            image.appended_dtb()

    def test_truncated_payload_is_rejected(self) -> None:
        kernel, ramdisk = sample_kernel(), gz(b"r" * 100)
        data = j4boot.build_image(self.template, kernel, ramdisk)
        with self.assertRaises(j4boot.BootImageError):
            j4boot.unpack_image(data[:-2048])

    def test_cmdline_override(self) -> None:
        data = j4boot.build_image(
            self.template, sample_kernel(), gz(b"r"), cmdline="console=ttyMSM0 root=LABEL=X"
        )
        image = j4boot.unpack_image(data)
        self.assertEqual(image.header.full_cmdline(), "console=ttyMSM0 root=LABEL=X")


@unittest.skipUnless(os.environ.get("J4_REFERENCE_BOOT"), "J4_REFERENCE_BOOT not set")
class ReferenceImageTests(unittest.TestCase):
    """Byte-exact assertions against the BOOT image that booted on the phone."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path(os.environ["J4_REFERENCE_BOOT"])
        cls.data = cls.path.read_bytes()
        cls.image = j4boot.unpack_image(cls.data)
        cls.report = cls.image.report()

    def test_size_and_page(self) -> None:
        self.assertEqual(len(self.data), 17510400)
        self.assertEqual(self.report["page_size"], 2048)
        self.assertEqual(self.report["header_version"], 0)

    def test_load_addresses(self) -> None:
        self.assertEqual(self.report["kernel_addr"], "0x80080000")
        self.assertEqual(self.report["ramdisk_addr"], "0x83600000")
        self.assertEqual(self.report["second_addr"], "0x80f00000")
        self.assertEqual(self.report["tags_addr"], "0x83400000")

    def test_kernel_is_arm64_medusa(self) -> None:
        self.assertEqual(self.report["arm64_image_magic"], "ARMd")
        self.assertIn("3.18.120-j4-medusa64-debug+", self.report["kernel_version_string"])
        self.assertEqual(
            self.report["kernel_payload_sha256"], j4boot.REFERENCE["kernel_sha256"]
        )

    def test_appended_dtb_is_rev06(self) -> None:
        self.assertEqual(self.report["dtb_sha256"], j4boot.REFERENCE["dtb_sha256"])
        self.assertEqual(self.report["dtb_model"], j4boot.REFERENCE["dtb_model"])
        self.assertEqual(self.report["dtb_board_id"], "words:0x8,0x6")

    def test_boot_id_matches_payload(self) -> None:
        self.assertEqual(
            self.image.header.image_id,
            j4boot.android_image_id(
                self.image.kernel, self.image.ramdisk, self.image.second, b""
            ),
        )

    def test_roundtrip_is_byte_identical(self) -> None:
        self.assertEqual(self.image.whole_image(), self.data)

    def test_cmdline_carries_the_pmos_root_handoff(self) -> None:
        self.assertIn(
            "pmos_root_uuid=86e7262b-b628-4b89-8a6e-e96ef9b1aab6",
            self.report["cmdline"],
        )

    def test_ramdisk_is_plain_gzip_not_lz4(self) -> None:
        self.assertEqual(self.image.ramdisk[:2], b"\x1f\x8b")
        self.assertGreater(len(j4boot.gunzip(self.image.ramdisk)), 10_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

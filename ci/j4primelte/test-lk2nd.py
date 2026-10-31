#!/usr/bin/env python3
"""Host-only tests for the combined lk2nd + M1 debug BOOT image.

Synthetic fixtures exercise image checks, not phone hardware. If
J4_LK2ND_DTB is set to the compiled outer DTB, also check its properties.
"""
import gzip
import importlib.util
import os
import struct
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pack = load('j4_lk2nd_pack_test', 'lk2nd-image.py')
fixtures = load('j4_m1_fixtures', 'test-mainline.py')


class CombinedBoot(unittest.TestCase):
    def setUp(self):
        self.uuid = '86e7262b-b628-4b89-8a6e-e96ef9b1aab6'
        self.baseargs = f'console=ttyMSM0 pmos_root_uuid={self.uuid}'
        self.debugargs = self.baseargs + ' rootwait pstore.compress=none pmos.debug-shell'
        self.ramdisk = b'unchanged donor initramfs'
        self.donor = fixtures.make_boot(page=2048, ramdisk=self.ramdisk, cmdline=self.baseargs)
        arm = fixtures.fake_arm64_image(size=4096, text_offset=0x80000)
        self.inner_tree = fixtures.fake_dtb(80)
        self.inner_kernel = gzip.compress(arm, mtime=0) + self.inner_tree
        self.inner = self.boot(self.inner_kernel, self.ramdisk, self.debugargs)
        self.lk = struct.pack('<I', 0xea000006) + b'L' * 4092
        self.outer_tree = fixtures.fake_dtb(80)

    def boot(self, kernel, ramdisk, args):
        data = bytearray(fixtures.make_boot(kernel=kernel, ramdisk=ramdisk, page=2048, cmdline=args))
        # The real M1 repacker changes the donor kernel address for its shim.
        struct.pack_into('<I', data, 12, 0x80080000)
        return bytes(data)

    def image(self):
        return pack.build_combined(self.donor, self.lk, self.outer_tree, self.inner)

    def verify(self, img):
        return pack.verify_combined(img, self.donor, self.lk, self.outer_tree, self.inner)

    def test_combined_round_trip_at_geckyn_split(self):
        img = self.image()
        report = self.verify(img)
        self.assertEqual(img[pack.OFFSET:], self.inner)
        self.assertEqual(report['inner_start'], 1048576)
        self.assertEqual(report['inner_length'], len(self.inner))
        self.assertLess(report['outer_end'], pack.OFFSET)
        self.assertEqual(img[report['outer_end']:pack.OFFSET], bytes(pack.OFFSET - report['outer_end']))
        outer = pack.parse_boot(img, require_ramdisk=False)
        self.assertEqual(outer['cmdline'], 'lk2nd')
        self.assertEqual(outer['ramdisk'], b'')
        self.assertEqual(outer['second'], b'')
        self.assertEqual(pack.unpack_gzip_dtb(outer['kernel']), (self.lk, self.outer_tree))

    def test_detect_wrong_upstream_512k_offset(self):
        img = self.image()
        bad = img[:512 * 1024] + self.inner + bytes(pack.OFFSET - 512 * 1024)
        with self.assertRaises(ValueError):
            self.verify(bad)

    def test_split_padding_corruption_is_rejected(self):
        img = bytearray(self.image())
        img[800000] = 1
        with self.assertRaisesRegex(ValueError, 'padding'):
            self.verify(bytes(img))

    def test_embedded_image_corruption_is_rejected(self):
        img = bytearray(self.image())
        img[-3] ^= 1
        with self.assertRaisesRegex(ValueError, 'byte-identical'):
            self.verify(bytes(img))

    def test_outer_header_checksum_corruption_is_rejected(self):
        img = bytearray(self.image())
        img[576] ^= 1
        with self.assertRaisesRegex(ValueError, 'image ID'):
            self.verify(bytes(img))

    def test_outer_header_address_corruption_is_rejected(self):
        img = bytearray(self.image())
        struct.pack_into('<I', img, 12, 0xdead0000)
        with self.assertRaisesRegex(ValueError, 'outer image changed donor header'):
            self.verify(bytes(img))

    def test_inner_header_address_corruption_is_rejected(self):
        inner = bytearray(self.inner)
        struct.pack_into('<I', inner, 12, 0xdead0000)
        with self.assertRaisesRegex(ValueError, 'kernel header address'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, bytes(inner))

    def test_corrupt_inner_id_is_rejected(self):
        inner = bytearray(self.inner)
        inner[576] ^= 1
        with self.assertRaisesRegex(ValueError, 'image ID'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, bytes(inner))

    def test_bad_donor_id_is_rejected(self):
        donor = bytearray(self.donor)
        donor[576] ^= 1
        with self.assertRaisesRegex(ValueError, 'image ID'):
            pack.build_combined(bytes(donor), self.lk, self.outer_tree, self.inner)

    def test_normal_rootfs_mode_is_not_packaged(self):
        inner = self.boot(self.inner_kernel, self.ramdisk, self.baseargs + ' pstore.compress=none')
        with self.assertRaisesRegex(ValueError, 'debug-shell'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_donor_initramfs_must_be_unchanged(self):
        inner = self.boot(self.inner_kernel, b'different ramdisk', self.debugargs)
        with self.assertRaisesRegex(ValueError, 'changed donor initramfs'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_root_uuid_must_match(self):
        inner = self.boot(self.inner_kernel, self.ramdisk, self.debugargs.replace(self.uuid, 'other'))
        with self.assertRaisesRegex(ValueError, 'root UUID changed'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_no_duplicate_root_uuid(self):
        inner = self.boot(self.inner_kernel, self.ramdisk, self.debugargs + ' pmos_root_uuid=other')
        with self.assertRaisesRegex(ValueError, 'exactly one root UUID'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_debug_must_fit_first_512_byte_field(self):
        args = self.baseargs + ' x=' + 'a' * 450 + ' pstore.compress=none pmos.debug-shell'
        inner = self.boot(self.inner_kernel, self.ramdisk, args)
        with self.assertRaisesRegex(ValueError, 'exceeds lk2nd first field'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_missing_pstore_compatibility_argument(self):
        inner = self.boot(self.inner_kernel, self.ramdisk, self.debugargs.replace('pstore.compress=none ', ''))
        with self.assertRaisesRegex(ValueError, 'pstore'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_second_kernel_must_have_arm64_magic(self):
        inner = self.boot(gzip.compress(b'x' * 4096, mtime=0) + self.inner_tree, self.ramdisk, self.debugargs)
        with self.assertRaisesRegex(ValueError, 'not ARM64'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_shim_address_must_match_secondary_loader(self):
        arm = fixtures.fake_arm64_image(size=4096, text_offset=0)
        inner = self.boot(gzip.compress(arm, mtime=0) + self.inner_tree, self.ramdisk, self.debugargs)
        with self.assertRaisesRegex(ValueError, 'shim header'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_kernel_effective_size_must_not_overlap_tags(self):
        arm = bytearray(fixtures.fake_arm64_image(size=4096))
        struct.pack_into('<Q', arm, 16, 0x4000000)
        inner = self.boot(gzip.compress(bytes(arm), mtime=0) + self.inner_tree, self.ramdisk, self.debugargs)
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_outer_kernel_must_be_arm32(self):
        lk = bytearray(self.lk)
        lk[56:60] = b'ARM\x64'
        with self.assertRaisesRegex(ValueError, 'ARM32'):
            pack.build_combined(self.donor, bytes(lk), self.outer_tree, self.inner)

    def test_outer_must_have_arm_branch_at_entry(self):
        with self.assertRaisesRegex(ValueError, 'branch'):
            pack.build_combined(self.donor, b'not an ARM binary' * 100, self.outer_tree, self.inner)

    def test_reject_bad_outer_fdt_size(self):
        tree = self.outer_tree[:-1]
        with self.assertRaisesRegex(ValueError, 'totalsize'):
            pack.build_combined(self.donor, self.lk, tree, self.inner)

    def test_gzip_dtb_cannot_have_a_gap(self):
        bad_kernel = gzip.compress(fixtures.fake_arm64_image(), mtime=0) + b'\0' + self.inner_tree
        inner = self.boot(bad_kernel, self.ramdisk, self.debugargs)
        with self.assertRaisesRegex(ValueError, 'flattened device tree'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, inner)

    def test_trailing_bytes_in_second_image_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'extra or missing bytes'):
            pack.build_combined(self.donor, self.lk, self.outer_tree, self.inner + b'\0')

    def test_image_must_fit_boot_partition(self):
        old = pack.PARTITION_SIZE
        try:
            pack.PARTITION_SIZE = pack.OFFSET + len(self.inner) - 1
            with self.assertRaisesRegex(ValueError, '32-MiB BOOT'):
                self.image()
        finally:
            pack.PARTITION_SIZE = old

    def test_lk2nd_must_fit_prefix(self):
        old = pack.OFFSET
        try:
            pack.OFFSET = 2048
            with self.assertRaisesRegex(ValueError, 'exceeds the 1-MiB split'):
                self.image()
        finally:
            pack.OFFSET = old

    def test_wrong_dtb_is_detected_by_independent_reparse(self):
        with self.assertRaisesRegex(ValueError, 'changed during packing'):
            pack.verify_combined(self.image(), self.donor, self.lk, fixtures.fake_dtb(81), self.inner)

    @unittest.skipUnless(os.environ.get('J4_LK2ND_DTB'), 'no compiled lk2nd DTB supplied')
    def test_real_lk2nd_dtb_properties(self):
        pack.check_outer_dtb(Path(os.environ['J4_LK2ND_DTB']))


if __name__ == '__main__':
    unittest.main(verbosity=2)

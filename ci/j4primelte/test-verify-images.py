#!/usr/bin/env python3
"""Tests for ci/j4primelte/verify-images.py.

The script does two things that must not be trusted blindly: it validates the
boot image format, and it repacks two extra images (boot-debug.img,
boot-slabdebug.img) by rewriting only the cmdline fields of the v0 header. A
mistake in the repack is invisible until the phone fails to boot, so these tests
pin the invariant that matters most: every byte from the page boundary on must be
identical, and the header fields the bootloader uses to load the payloads must
not move either.
"""
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'verify-images.py'
PAGE = 2048
KSIZE, RSIZE = 16, 16
FBCON = b'fbcon=map:0,scrollback:256k'


def read_cmdline(image):
    head = image[64:576].split(b'\0', 1)[0] + image[608:1632].split(b'\0', 1)[0]
    return head.decode()


def make_boot(cmdline=b'console=ttyMSM0,115200n8 rootwait ' + FBCON, version=0,
              page=PAGE,
              kaddr=0x80008000, raddr=0x82000000):
    assert len(cmdline) <= 512 + 1024
    head = bytearray(page)
    head[0:8] = b'ANDROID!'
    struct.pack_into('<10I', head, 8, KSIZE, kaddr, RSIZE, raddr, 0, 0, 0, page,
                     version, 0)
    if version == 0:
        # The v0 id field (576..608) is a SHA-1 over the payloads. Keep it
        # non-empty so a repack that touched it would be noticed.
        head[576:608] = b'0' * 32
    body = bytearray()
    body += b'K' * KSIZE + b'\0' * (page - KSIZE)
    body += b'R' * RSIZE + b'\0' * (page - RSIZE)
    # Fixed-width fields: assign whole slices, never a short value into a long
    # slice, or the bytearray shrinks and the payload silently moves.
    head[64:576] = cmdline[:512].ljust(512, b'\0')
    if len(cmdline) > 512:
        head[608:1632] = cmdline[512:1536].ljust(1024, b'\0')
    return bytes(head) + bytes(body)


def make_rootfs(magic=True, gpt=False):
    data = bytearray(2 * PAGE)
    if magic:
        data[1080:1082] = b'\x53\xef'
    if gpt:
        data[512:520] = b'EFI PART'
    return bytes(data)


class RepackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def build(self, cmdline=b'console=ttyMSM0,115200n8 rootwait ' + FBCON,
                rootfs=None):
        (self.tmp / 'boot.img').write_bytes(make_boot(cmdline))
        (self.tmp / 'rootfs.img').write_bytes(
            make_rootfs() if rootfs is None else rootfs)
        return subprocess.run([sys.executable, str(SCRIPT), str(self.tmp)],
                              capture_output=True, text=True)

    def test_all_variants_keep_the_payload_byte_identical(self):
        self.assertEqual(self.build().returncode, 0)
        boot = (self.tmp / 'boot.img').read_bytes()
        for name in ('boot-debug.img', 'boot-slabdebug.img', 'boot-latebind.img'):
            image = (self.tmp / name).read_bytes()
            self.assertEqual(len(image), len(boot), name)
            self.assertEqual(image[PAGE:], boot[PAGE:],
                             '%s changed the kernel or ramdisk bytes' % name)
            # Sizes, load addresses and the SHA-1 id field must not move.
            self.assertEqual(image[8:64], boot[8:64], name)
            self.assertEqual(image[576:608], boot[576:608], name)
            self.assertEqual(image[:8], b'ANDROID!', name)

    def test_only_the_expected_arguments_are_appended(self):
        self.assertEqual(self.build().returncode, 0)
        base = read_cmdline((self.tmp / 'boot.img').read_bytes())
        debug = read_cmdline((self.tmp / 'boot-debug.img').read_bytes())
        slub = read_cmdline((self.tmp / 'boot-slabdebug.img').read_bytes())
        late = read_cmdline((self.tmp / 'boot-latebind.img').read_bytes())
        self.assertEqual(debug, base + ' pmos.debug-shell')
        self.assertEqual(slub, base + ' slub_debug=FZPU panic_on_oops=0')
        self.assertEqual(late, base.replace(
            'fbcon=map:0,scrollback:256k', 'fbcon=map:0,scrollback:256k,late'))
        self.assertEqual(late.count('fbcon='), 1,
                         'late bind must extend the existing fbcon= argument')
        self.assertNotIn('slub_debug', late)
        self.assertNotIn('pmos.debug-shell', late)
        # The debugging images must stay distinguishable from each other: the
        # slab one has to boot all the way to the desktop to be useful.
        self.assertNotIn('pmos.debug-shell', slub)
        self.assertNotIn('slub_debug', debug)
        self.assertNotIn(',late', base)

    def test_normal_image_carries_no_debug_flag(self):
        self.assertEqual(self.build().returncode, 0)
        cmd = read_cmdline((self.tmp / 'boot.img').read_bytes())
        self.assertNotIn('pmos.debug-shell', cmd)
        self.assertNotIn('slub_debug', cmd)

    def test_report_lists_every_cmdline(self):
        self.assertEqual(self.build().returncode, 0)
        report = json.loads((self.tmp / 'image-info.json').read_text())
        self.assertIn('slub_debug=FZPU', report['slubdebug_cmdline'])
        self.assertIn('scrollback:256k,late', report['latebind_cmdline'])
        self.assertIn('pmos.debug-shell', report['debug_cmdline'])
        self.assertNotIn('slub_debug', report['cmdline'])
        self.assertNotIn(',late', report['cmdline'])
        self.assertFalse(report['hardware_tested'])
        self.assertEqual(report['boot_header_version'], 0)
        self.assertEqual(report['kernel_bytes'], KSIZE)
        # The printed report and the file must agree.
        self.assertEqual(json.loads(subprocess.run(
            [sys.executable, str(SCRIPT), str(self.tmp)], capture_output=True,
            text=True).stdout)['slubdebug_cmdline'], report['slubdebug_cmdline'])

    def test_long_cmdline_is_refused_instead_of_truncated(self):
        # 1536 bytes is the v0 budget (512 + 1024); appending the debug flag has
        # to fail loudly rather than silently cut the boot arguments.
        proc = self.build(b'x' * 1536)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('cmdline too long', proc.stderr)

    def test_a_base_without_the_exact_fbcon_argument_is_refused(self):
        # The late-bind variant rewrites the fbcon= string. If the device's
        # cmdline ever changes shape, a variant that is silently identical to
        # boot.img is worse than a failed build.
        proc = self.build(b'console=ttyMSM0,115200n8 rootwait')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('fbcon=', proc.stderr)

    def test_unsupported_header_version_is_refused(self):
        proc = self.build()
        boot = bytearray((self.tmp / 'boot.img').read_bytes())
        struct.pack_into('<10I', boot, 8, KSIZE, 0x80008000, RSIZE, 0x82000000,
                         0, 0, 0, PAGE, 1, 0)
        (self.tmp / 'boot.img').write_bytes(bytes(boot))
        proc = subprocess.run([sys.executable, str(SCRIPT), str(self.tmp)],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Unsupported boot header version', proc.stderr)

    def test_wrong_load_addresses_are_refused(self):
        (self.tmp / 'boot.img').write_bytes(make_boot(kaddr=0x8000_0000))
        (self.tmp / 'rootfs.img').write_bytes(make_rootfs())
        proc = subprocess.run([sys.executable, str(SCRIPT), str(self.tmp)],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('load addresses', proc.stderr)

    def test_non_ext4_rootfs_is_refused(self):
        proc = self.build(rootfs=bytes(2 * PAGE))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ext4', proc.stderr)

    def test_gpt_named_rootfs_is_refused(self):
        proc = self.build(rootfs=make_rootfs(gpt=True))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('GPT', proc.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)

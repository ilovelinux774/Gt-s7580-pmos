#!/usr/bin/env python3
"""Unit tests for the boot image root UUID patcher."""
import importlib.util
import struct
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patcher = load('patch-boot-uuid')

OLD = 'c142671f-5675-4599-99f4-82380ae1bb02'
NEW = '86e7262b-b628-4b89-8a6e-e96ef9b1aab6'
PAYLOAD = b'\xaa' * 4096


def boot_image(cmdline):
    data = bytearray(2048 + len(PAYLOAD))
    data[:8] = b'ANDROID!'
    struct.pack_into('<I', data, 8 + 7 * 4, 2048)   # page size
    struct.pack_into('<I', data, 8 + 9 * 4, 0)      # header version
    data[2048:] = PAYLOAD
    raw = cmdline.encode()
    data[64:576] = raw[:512].ljust(512, b'\0')
    data[608:1632] = raw[512:].ljust(1024, b'\0')
    return data


class ShowTests(unittest.TestCase):
    def test_shows_the_current_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'boot.img'
            path.write_bytes(boot_image(' pmos_root_uuid=%s quiet' % OLD))
            patcher.show(path)
            self.assertEqual(patcher.current_uuid(bytearray(path.read_bytes())), OLD)

    def test_rejects_a_non_boot_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'boot.img'
            path.write_bytes(b'NOTANDR!' + b'\0' * 4096)
            with self.assertRaises(patcher.Error):
                patcher.show(path)

    def test_rejects_a_cmdline_without_a_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'boot.img'
            path.write_bytes(boot_image(' quiet'))
            with self.assertRaises(patcher.Error):
                patcher.show(path)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'boot.img'
        self.path.write_bytes(boot_image(' pmos_root_uuid=%s pmos_rootfsopts=defaults' % OLD))

    def test_patches_only_the_cmdline(self):
        patcher.apply(self.path, NEW)
        data = bytearray(self.path.read_bytes())
        self.assertEqual(patcher.current_uuid(data), NEW)
        self.assertNotIn(OLD.encode(), bytes(data[:1632]))
        self.assertEqual(bytes(data[2048:]), PAYLOAD,
                         'kernel/ramdisk payload must stay byte-identical')

    def test_patch_keeps_the_rest_of_the_cmdline(self):
        patcher.apply(self.path, NEW)
        command = patcher.cmdline(bytearray(self.path.read_bytes()))
        self.assertIn(b'pmos_rootfsopts=defaults', command)

    def test_patch_is_idempotent(self):
        patcher.apply(self.path, NEW)
        patcher.apply(self.path, NEW)
        self.assertEqual(patcher.current_uuid(bytearray(self.path.read_bytes())), NEW)

    def test_rejects_a_malformed_uuid(self):
        with self.assertRaises(patcher.Error):
            patcher.apply(self.path, 'not-a-uuid')

    def test_rejects_a_length_change(self):
        with self.assertRaises(patcher.Error):
            patcher.apply(self.path, NEW + '0')


if __name__ == '__main__':
    unittest.main()

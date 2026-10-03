#!/usr/bin/env python3
"""Unit tests for the fixed root filesystem UUID helper."""
import importlib.util
import struct
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fix = load('fix-root-uuid')

OLD = '7e139e7c-fdc2-4df5-84fc-151c89fef593'


def boot_image(cmdline):
    """A minimal v0 Android boot image carrying the given cmdline."""
    data = bytearray(2048 + 4096)
    data[:8] = b'ANDROID!'
    struct.pack_into('<I', data, 8 + 9 * 4, 0)  # header version 0
    struct.pack_into('<I', data, 8 + 7 * 4, 2048)  # page size
    payload = cmdline.encode()
    data[64:576] = payload[:512].ljust(512, b'\0')
    data[608:1632] = payload[512:].ljust(1024, b'\0')
    return data


class UuidTests(unittest.TestCase):
    def test_fixed_uuid_is_stable_and_well_formed(self):
        self.assertEqual(fix.FIXED_UUID, '86e7262b-b628-4b89-8a6e-e96ef9b1aab6')
        self.assertEqual(len(fix.FIXED_UUID), 36)
        self.assertRegex(fix.FIXED_UUID,
                         r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                         r'-[0-9a-f]{4}-[0-9a-f]{12}$')

    def test_extract_reads_the_cmdline(self):
        image = boot_image(' pmos_root_uuid=%s pmos_rootfsopts=defaults' % OLD)
        self.assertEqual(fix.extract_root_uuid(image), OLD)

    def test_extract_rejects_non_boot_images(self):
        with self.assertRaises(fix.Error):
            fix.extract_root_uuid(bytearray(b'NOTANDR!') + bytearray(4096))

    def test_extract_rejects_a_cmdline_without_a_root(self):
        with self.assertRaises(fix.Error):
            fix.extract_root_uuid(boot_image(' quiet'))


class PatchTests(unittest.TestCase):
    def test_patch_replaces_only_the_cmdline_slots(self):
        image = boot_image(' pmos_root_uuid=%s pmos_rootfsopts=defaults' % OLD)
        payload_before = bytes(image[2048:])
        changed = fix.patch_cmdline(image, OLD, fix.FIXED_UUID)
        self.assertEqual(changed, 1)
        self.assertEqual(fix.extract_root_uuid(image), fix.FIXED_UUID)
        self.assertNotIn(OLD.encode(), bytes(image[:1632]))
        self.assertEqual(bytes(image[2048:]), payload_before,
                         'kernel/ramdisk payload must not change')

    def test_patch_is_idempotent(self):
        image = boot_image(' pmos_root_uuid=%s' % OLD)
        fix.patch_cmdline(image, OLD, fix.FIXED_UUID)
        self.assertEqual(fix.patch_cmdline(image, OLD, fix.FIXED_UUID), 0)
        self.assertEqual(fix.extract_root_uuid(image), fix.FIXED_UUID)

    def test_patch_refuses_a_length_change(self):
        image = boot_image(' pmos_root_uuid=%s' % OLD)
        with self.assertRaises(fix.Error):
            fix.patch_cmdline(image, OLD, fix.FIXED_UUID + '0')


class WiringTests(unittest.TestCase):
    def test_build_pipeline_enforces_the_uuid_before_validation(self):
        script = (HERE / 'build-pmos.sh').read_text()
        self.assertIn('fix-root-uuid.py', script)
        self.assertIn('verify-images.py', script)
        self.assertLess(script.index('fix-root-uuid.py'),
                        script.index('verify-images.py'),
                        'the UUID must be fixed before images are validated')

    def test_workflow_runs_these_tests(self):
        workflow = (HERE.parent.parent / '.github/workflows/build-medusa.yml').read_text()
        self.assertIn('test-fix-root-uuid.py', workflow)


if __name__ == '__main__':
    unittest.main()

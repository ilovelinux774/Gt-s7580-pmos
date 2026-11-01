#!/usr/bin/env python3
"""Offline tests for the Medusa64 staging/packing safety checks."""
from pathlib import Path
import gzip
import hashlib
import importlib.util
import shutil
import struct
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load('j4_medusa64', 'medusa64-repack.py')
s = load('j4_medusa64_stage', 'medusa64-stage.py')
v = load('j4_medusa64_verify', 'verify-medusa64.py')
fixture = load('j4_mainline_tests', 'test-mainline.py')


def image_gz(offset=0x80000, effective_size=4096, flags=0):
    raw = bytearray(4096)
    struct.pack_into('<QQQ', raw, 8, offset, effective_size, flags)
    raw[56:60] = b'ARM\x64'
    return gzip.compress(bytes(raw), mtime=0)


def make_tree(folder, rev=6, touch_bringup=1, pmic=0x10019):
    dts = folder / 'test.dts'
    dtb = folder / 'test.dtb'
    dts.write_text(f'''/dts-v1/;
/ {{
    qcom,board-id = <8 {rev}>;
    qcom,msm-id = <303 0 308 0 309 0>;
    qcom,pmic-id = <0x{pmic:x} 0 0 0>;
    soc {{
        i2c@78b6000 {{
            touchscreen@48 {{
                sec,bringup = <{touch_bringup}>;
                sec,firmware_name = "tsp_sec/s6d7at01a01_j4prime.fw";
            }};
        }};
    }};
}};
''')
    subprocess.run(['dtc', '-I', 'dts', '-O', 'dtb', '-o', str(dtb), str(dts)],
                   check=True, capture_output=True)
    return dtb


@unittest.skipUnless(shutil.which('dtc') and shutil.which('fdtget'), 'dtc/fdtget required')
class Packaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.dtb = make_tree(self.folder)
        self.tree = m.validate_dtb(self.dtb)
        self.donor = fixture.make_boot(page=2048, name=b'',
                                      cmdline='pmos_root_uuid=' + m.ROOT_UUID)
        self.donor_sha = m.sha(self.donor)
        self.ramdisk_sha = m.sha(m.r.BootImage(self.donor).ramdisk)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, donor=None, kernel=None, dtb=None, **kwargs):
        return m.build(self.donor if donor is None else donor,
                       image_gz() if kernel is None else kernel,
                       self.tree if dtb is None else dtb,
                       donor_sha=kwargs.get('donor_sha', self.donor_sha),
                       ramdisk_sha=kwargs.get('ramdisk_sha', self.ramdisk_sha))

    def test_one_rev06_and_preserved_ramdisk(self):
        out, info = self.build()
        boot = m.r.BootImage(out)
        original = m.r.BootImage(self.donor)
        self.assertEqual(boot.ramdisk, original.ramdisk)
        self.assertEqual(boot.kernel, image_gz() + self.tree)
        self.assertIn('pmos.debug-shell', boot.cmdline)
        self.assertEqual(boot.kernel_addr, 0x80080000)
        self.assertEqual(boot.tags_addr, 0x83400000)
        self.assertEqual(boot.ramdisk_addr, 0x83600000)
        self.assertTrue(info['ramdisk_copied_byte_for_byte'])
        self.assertEqual(info['root_uuid'], m.ROOT_UUID)

    def test_wrong_donor_and_ramdisk_rejected(self):
        with self.assertRaises(ValueError): self.build(donor_sha='0' * 64)
        with self.assertRaises(ValueError): self.build(ramdisk_sha='0' * 64)

    def test_donor_root_uuid_must_match(self):
        bad = fixture.make_boot(page=2048, cmdline='pmos_root_uuid=00000000-0000-0000-0000-000000000000')
        with self.assertRaises(ValueError): self.build(donor=bad, donor_sha=m.sha(bad))

    def test_wrong_revision_pmic_and_automatic_touch_update_rejected(self):
        for opts in ({'rev': 4}, {'pmic': 0x20019}, {'touch_bringup': 0}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                m.validate_dtb(make_tree(self.folder, **opts))

    def test_trailing_dtb_or_garbage_in_image_gz_rejected(self):
        for tail in (b'junk', self.tree):
            with self.assertRaises(ValueError): self.build(kernel=image_gz() + tail)

    def test_multiple_appended_dtb_rejected(self):
        with self.assertRaises(ValueError): self.build(dtb=self.tree + self.tree)

    def test_invalid_header_and_overlapping_kernel_rejected(self):
        for kernel in (image_gz(offset=0), image_gz(flags=1),
                       image_gz(effective_size=0x3400000),
                       gzip.compress(b'not an arm64 image', mtime=0), b'not gzip'):
            with self.subTest(size=len(kernel)), self.assertRaises(ValueError):
                self.build(kernel=kernel)

    def test_config_required_and_unwanted_flags(self):
        cfg = self.folder / 'config'
        lines = [f'CONFIG_{x}=y' for x in m.REQUIRED_Y] + list(m.REQUIRED_EXTRA)
        cfg.write_text('\n'.join(lines) + '\n')
        m.validate_config(cfg)
        cfg.write_text('\n'.join(lines[1:]) + '\n')
        with self.assertRaises(ValueError): m.validate_config(cfg)
        cfg.write_text('\n'.join(lines + ['CONFIG_ANDROID_PARANOID_NETWORK=y']) + '\n')
        with self.assertRaises(ValueError): m.validate_config(cfg)
        cfg.write_text('\n'.join(lines[:-1]) + '\n')
        with self.assertRaises(ValueError): m.validate_config(cfg)
        cfg.write_text('\n'.join(x for x in lines if not x.startswith('CONFIG_EXTRA_FIRMWARE=')) + '\n')
        with self.assertRaises(ValueError): m.validate_config(cfg)


@unittest.skipUnless(shutil.which('dtc') and shutil.which('fdtget'), 'dtc/fdtget required')
class IndependentVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.dtb = make_tree(self.folder)
        self.donor = fixture.make_boot(page=2048, name=b'',
                                       cmdline='pmos_root_uuid=' + m.ROOT_UUID)
        self.touch = bytes(range(251)) * (120144 // 251) + bytes(range(120144 % 251))
        self.pm4 = b'\x9d' * 9220
        self.pfp = b'\x8e' * 1156
        raw = bytearray(4096) + self.touch + self.pm4 + self.pfp
        struct.pack_into('<QQQ', raw, 8, 0x80000, len(raw), 0)
        raw[56:60] = b'ARM\x64'
        self.gz = gzip.compress(bytes(raw), mtime=0)
        self.image, _ = m.build(self.donor, self.gz, self.dtb.read_bytes(),
                                donor_sha=m.sha(self.donor),
                                ramdisk_sha=m.sha(m.r.BootImage(self.donor).ramdisk))
        self.config = '\n'.join([f'CONFIG_{x}=y' for x in m.REQUIRED_Y] + list(m.REQUIRED_EXTRA)) + '\n'
        self.constants = {k: getattr(v, k) for k in ('DONOR_SHA', 'RAMDISK_SHA', 'TOUCH_Q_SHA', 'GPU_PM4_SHA', 'GPU_PFP_SHA')}
        v.DONOR_SHA = v.sha(self.donor)
        v.RAMDISK_SHA = v.sha(m.r.BootImage(self.donor).ramdisk)
        v.TOUCH_Q_SHA = v.sha(self.touch)
        v.GPU_PM4_SHA = v.sha(self.pm4)
        v.GPU_PFP_SHA = v.sha(self.pfp)

    def tearDown(self):
        for k, value in self.constants.items(): setattr(v, k, value)
        self.tmp.cleanup()

    def verify(self, image=None, touch=None, pm4=None, pfp=None):
        return v.verify(self.image if image is None else image, self.donor, self.gz,
                        self.dtb, self.config, self.touch if touch is None else touch,
                        self.pm4 if pm4 is None else pm4, self.pfp if pfp is None else pfp)

    def test_independent_parser_accepts_valid_fixture(self):
        result = self.verify()
        self.assertEqual(result['boot_sha256'], v.sha(self.image))
        self.assertIn('static checks only', result['result'])

    def test_rejects_corrupted_header_id_and_firmware(self):
        damaged = bytearray(self.image)
        damaged[576] ^= 1
        with self.assertRaises(ValueError): self.verify(image=bytes(damaged))
        with self.assertRaises(ValueError): self.verify(touch=b'wrong')
        with self.assertRaises(ValueError): self.verify(pm4=b'wrong')
        with self.assertRaises(ValueError): self.verify(pfp=b'wrong')


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.old_constants = {k: getattr(s, k) for k in
                              ('DTS_ORIGINAL_SHA', 'DRIVER_SHA', 'TOUCH_64_SHA', 'TOUCH_Q_SHA')}
        self.dts = self.folder / s.DTS_REL
        self.driver = self.folder / s.TOUCH_DRIVER_REL
        self.fw = self.folder / s.TOUCH_REL
        self.q = self.folder / 'known-working.ihex'
        self.bt = self.folder / 'drivers/bluetooth'
        for p in (self.dts, self.driver, self.fw, self.bt / 'Kconfig'):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.dts.write_text('/ { ' + s.FW_NAME + ' ' + s.OLD_BRINGUP + ' };\n')
        self.driver.write_bytes(b'int sec_ts_firmware_update_on_probe(\nif (ts->plat_data->bringup == 1)')
        self.fw.write_bytes(b'old touchscreen data')
        self.q.write_bytes(b'known downstream touchscreen data')
        (self.bt / 'Kconfig').write_text('menu "Bluetooth"\nconfig MSM_BT_POWER\nendmenu\n')
        (self.bt / 'Makefile').write_text('# Bluetooth\n')
        s.DTS_ORIGINAL_SHA = s.sha(self.dts.read_bytes())
        s.DRIVER_SHA = s.sha(self.driver.read_bytes())
        s.TOUCH_64_SHA = s.sha(self.fw.read_bytes())
        s.TOUCH_Q_SHA = s.sha(self.q.read_bytes())

    def tearDown(self):
        for k, v in self.old_constants.items(): setattr(s, k, v)
        self.tmp.cleanup()

    def test_stages_expected_changes_idempotently(self):
        first = s.prepare(self.folder, self.q)
        second = s.prepare(self.folder, self.q)
        self.assertEqual(first, second)
        self.assertIn(s.NEW_BRINGUP, self.dts.read_text())
        self.assertEqual(self.fw.read_bytes(), self.q.read_bytes())
        self.assertTrue((self.bt / 'hci_smd.c').is_file())
        self.assertEqual((self.bt / 'Kconfig').read_text().count('config BT_HCI_SMD'), 1)
        self.assertEqual((self.bt / 'Makefile').read_text().count('hci_smd.o'), 1)

    def test_wrong_firmware_fails_before_writes(self):
        self.q.write_bytes(b'unexpected')
        before = (self.dts.read_bytes(), self.fw.read_bytes())
        with self.assertRaises(ValueError): s.prepare(self.folder, self.q)
        self.assertEqual(before, (self.dts.read_bytes(), self.fw.read_bytes()))

    def test_unexpected_dts_and_driver_fail(self):
        for p in (self.dts, self.driver):
            before = p.read_bytes()
            p.write_bytes(before + b'changed')
            with self.assertRaises(ValueError): s.prepare(self.folder, self.q)
            p.write_bytes(before)

    def test_missing_bluetooth_anchor_fails_before_writes(self):
        (self.bt / 'Kconfig').write_text('menu "Bluetooth"\nendmenu\n')
        before = (self.dts.read_bytes(), self.fw.read_bytes())
        with self.assertRaises(ValueError): s.prepare(self.folder, self.q)
        self.assertEqual(before, (self.dts.read_bytes(), self.fw.read_bytes()))


if __name__ == '__main__':
    unittest.main(verbosity=2)

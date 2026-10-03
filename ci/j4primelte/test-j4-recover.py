#!/usr/bin/env python3
"""Unit tests for the J4+ recovery helper (firmware, radio, Wi-Fi, LXQt)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = load('j4-recover')


class PayloadTests(unittest.TestCase):
    def test_embedded_installers_match_the_repo(self):
        self.assertEqual(rec.WIFI_TEXT, (HERE / 'wifi-setup.py').read_text())
        self.assertEqual(rec.LXQT_TEXT, (HERE / 'lxqt-setup.py').read_text())

    def test_embedded_service_files_match_the_overlay(self):
        self.assertEqual(rec.INIT_TEXT,
                         (HERE / 'rootfs/etc/init.d/j4-firstboot').read_text())
        self.assertEqual(rec.CONF_TEXT,
                         (HERE / 'rootfs/etc/conf.d/j4-firstboot').read_text())

    def test_image_carries_the_same_script(self):
        # A fresh rootfs heals itself, so the image must carry the helper.
        self.assertEqual(
            (HERE / 'rootfs/usr/local/sbin/j4-recover').read_text(),
            (HERE / 'j4-recover.py').read_text())

    def test_init_script_runs_at_boot_and_is_openrc(self):
        self.assertIn('#!/sbin/openrc-run', rec.INIT_TEXT)
        self.assertIn('need localmount', rec.INIT_TEXT)
        self.assertIn('/usr/local/sbin/j4-recover --boot', rec.INIT_TEXT)
        self.assertIn('J4_FIRSTBOOT_V1', rec.INIT_TEXT)

    def test_conf_d_defaults(self):
        self.assertIn('J4_FIRSTBOOT_FIRMWARE="yes"', rec.CONF_TEXT)
        self.assertIn('J4_FIRSTBOOT_DESKTOP="yes"', rec.CONF_TEXT)

    def test_no_credentials_are_embedded(self):
        for text in (rec.INIT_TEXT, rec.CONF_TEXT):
            lowered = text.lower()
            self.assertNotIn('psk', lowered)
            self.assertNotIn('password', lowered)


class FirmwareTests(unittest.TestCase):
    def test_source_list_matches_the_stock_partitions(self):
        sources = rec.firmware_sources()
        self.assertEqual(len(sources), 14)
        names = sorted(rel for _, rel in sources)
        self.assertEqual(names[0], 'wcnss.b00')
        self.assertEqual(names[-1], 'wlan/prima/WCNSS_wlan_dictionary.dat')
        self.assertIn('wcnss.b12', names)
        self.assertIn('wlan/prima/WCNSS_qcom_wlan_nv.bin', names)

    def test_sources_never_come_from_the_symlinked_vendor_dir(self):
        for source, _ in rec.firmware_sources():
            self.assertNotIn('/firmware/wcnss.', str(source),
                             'vendor /firmware/wcnss.* are Android symlinks')
            self.assertTrue(str(source).startswith(
                ('/mnt/j4-apnhlos/image/', '/mnt/j4-vendor/firmware/wlan/prima/')))

    def test_missing_detection_and_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            firmware = root / 'lib/firmware'
            firmware.mkdir(parents=True)
            saved = (rec.APNHLOS, rec.VENDOR, rec.FIRMWARE, rec.ensure_mount)
            try:
                rec.APNHLOS = root / 'mnt/j4-apnhlos'
                rec.VENDOR = root / 'mnt/j4-vendor'
                rec.FIRMWARE = firmware
                # Test staging itself; mounting is skipped in this layout.
                rec.ensure_mount = lambda *a, **k: False
                sources = rec.firmware_sources()
                for source, _ in sources:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(b'firmware')
                self.assertEqual(len(rec.firmware_missing()), len(sources))
                rec.stage_firmware()
                self.assertEqual(rec.firmware_missing(), [])
                for _, rel in sources:
                    self.assertTrue((firmware / rel).is_file())
            finally:
                rec.APNHLOS, rec.VENDOR, rec.FIRMWARE, rec.ensure_mount = saved

    def test_staging_refuses_a_symlinked_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            firmware = root / 'lib/firmware'
            firmware.mkdir(parents=True)
            saved = (rec.APNHLOS, rec.VENDOR, rec.FIRMWARE, rec.ensure_mount)
            try:
                rec.APNHLOS = root / 'mnt/j4-apnhlos'
                rec.VENDOR = root / 'mnt/j4-vendor'
                rec.FIRMWARE = firmware
                rec.ensure_mount = lambda *a, **k: False
                for source, _ in rec.firmware_sources():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if not source.exists():
                        source.symlink_to('/firmware/image/' + source.name)
                with self.assertRaises(rec.SetupError):
                    rec.stage_firmware()
            finally:
                rec.APNHLOS, rec.VENDOR, rec.FIRMWARE, rec.ensure_mount = saved


class OwnerTests(unittest.TestCase):
    def test_refuses_to_clobber_a_foreign_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'init'
            path.write_text('#!/bin/sh\nsomething else\n')
            with self.assertRaises(rec.SetupError):
                rec.write_owned(path, rec.INIT_TEXT, 0o755, 'J4_FIRSTBOOT_V1')

    def test_refuses_to_replace_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'target'
            target.write_text('x')
            link = Path(tmp) / 'link'
            link.symlink_to(target)
            with self.assertRaises(rec.SetupError):
                rec.write_owned(link, rec.CONF_TEXT, 0o644, 'J4_FIRSTBOOT_V1')

    def test_accepts_files_it_already_owns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'conf'
            path.write_text('# J4_FIRSTBOOT_V1\nold\n')
            rec.write_owned(path, rec.CONF_TEXT, 0o644, 'J4_FIRSTBOOT_V1')
            self.assertEqual(path.read_text(), rec.CONF_TEXT)


class RadioTests(unittest.TestCase):
    def test_radio_up_refuses_without_the_wcnss_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = (rec.WLAN, rec.WCNSS_DEV, rec.FWPATH)
            try:
                rec.WLAN = root / 'nope-wlan0'
                rec.WCNSS_DEV = root / 'nope-wcnss'
                rec.FWPATH = root / 'fwpath'
                rec.FWPATH.write_text('')
                with self.assertRaises(rec.SetupError):
                    rec.radio_up()
            finally:
                rec.WLAN, rec.WCNSS_DEV, rec.FWPATH = saved

    def test_radio_up_is_a_noop_when_wlan0_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            wlan = Path(tmp) / 'wlan0'
            wlan.mkdir()
            saved = rec.WLAN
            try:
                rec.WLAN = wlan
                rec.radio_up()  # must not raise
            finally:
                rec.WLAN = saved


class IntegrationTests(unittest.TestCase):
    def test_customize_rootfs_enables_the_firstboot_service(self):
        text = (HERE / 'customize-rootfs.py').read_text()
        self.assertIn("level / 'j4-firstboot'", text)
        self.assertIn("'../../init.d/j4-firstboot'", text)

    def test_service_runs_in_the_boot_runlevel(self):
        self.assertEqual(rec.RUNLEVEL_LINK,
                         Path('/etc/runlevels/boot/j4-firstboot'))

    def test_overlay_scripts_are_executable_shebangs(self):
        helper = (HERE / 'rootfs/usr/local/sbin/j4-recover').read_text()
        self.assertTrue(helper.startswith('#!/usr/bin/env python3\n'))

    def test_log_never_crashes_the_helper(self):
        # The error path must stay usable even when /var/log is read-only.
        rec.LOG = Path('/proc/definitely-not-writable/j4/recover.log')
        rec.log('test message')


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for the J4+ Bluetooth bring-up helper (userspace side)."""
import importlib.util
from pathlib import Path
import re
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bt = load('bluetooth-setup')
OVERLAY = Path(__file__).resolve().parent / 'rootfs'


class PayloadTests(unittest.TestCase):
    def test_embedded_texts_match_the_overlay(self):
        self.maxDiff = 2000
        for name, rel in (('SCRIPT_TEXT', 'usr/local/sbin/j4-bt'),
                          ('INIT_TEXT', 'etc/init.d/j4-bluetooth'),
                          ('CONF_TEXT', 'etc/conf.d/j4-bluetooth')):
            self.assertEqual(getattr(bt, name), (OVERLAY / rel).read_text(),
                             'embedded %s is out of sync' % name)

    def test_helper_is_a_posix_shell_script(self):
        self.assertTrue(bt.SCRIPT_TEXT.startswith('#!/bin/sh\n'))

    def test_helper_has_no_bashisms(self):
        for bad in ('[[', 'function ', '+=', 'echo -e', 'read -p', 'local '):
            self.assertNotIn(bad, bt.SCRIPT_TEXT, 'bashism: %r' % bad)

    def test_helper_subcommands_are_wired(self):
        script = bt.SCRIPT_TEXT
        for flag in ('--boot', '--status', '--scan', '--pair'):
            self.assertTrue(flag + ')' in script or flag + '|' in script,
                            'subcommand %s is not wired' % flag)

    def test_helper_waits_for_the_hci_device(self):
        self.assertIn('/sys/class/bluetooth/hci0', bt.SCRIPT_TEXT)
        self.assertIn('j4-bt: no Bluetooth device', bt.SCRIPT_TEXT)

    def test_helper_never_prints_or_ships_secrets(self):
        self.assertNotIn('Password', bt.SCRIPT_TEXT)
        self.assertNotIn('psk', bt.SCRIPT_TEXT.lower())

    def test_helper_logs_only_to_var_log_j4(self):
        for line in bt.SCRIPT_TEXT.splitlines():
            if '>>' in line or '>' in line and 'echo' not in line:
                self.assertNotIn('/tmp', line)

    def test_init_script_targets_openrc_and_waits_for_wifi(self):
        self.assertIn('#!/sbin/openrc-run', bt.INIT_TEXT)
        self.assertIn('after j4-wifi', bt.INIT_TEXT)
        self.assertIn('/usr/local/sbin/j4-bt --boot', bt.INIT_TEXT)

    def test_conf_d_defaults(self):
        self.assertIn('J4_BT_TIMEOUT="90"', bt.CONF_TEXT)
        self.assertIn('J4_BT_ADDRESS=""', bt.CONF_TEXT)
        self.assertIn('J4_BT_START_BLUETOOTHD="yes"', bt.CONF_TEXT)

    def test_conf_d_has_no_real_credentials(self):
        self.assertNotIn('psk', bt.CONF_TEXT.lower())


class BluezDetectionTests(unittest.TestCase):
    def test_looks_for_the_libexec_daemon(self):
        # alpine installs bluetoothd outside PATH; a bare lookup is not enough.
        for path in ('/usr/libexec/bluetooth/bluetoothd',
                     '/usr/lib/bluetooth/bluetoothd'):
            self.assertIn(path, bt.SCRIPT_TEXT)
        self.assertIn('have_bluez', bt.SCRIPT_TEXT)

    def test_stack_helper_uses_the_detection(self):
        self.assertIn('if ! have_bluez; then', bt.SCRIPT_TEXT)


class AddressTests(unittest.TestCase):
    def setUp(self):
        self.maxDiff = 2000
        self.text = bt.SCRIPT_TEXT

    def test_generated_address_is_locally_administered_unicast(self):
        # 0x02 == locally administered + unicast; never clashes with a real
        # vendor OUIs and never collides with a real device address.
        self.assertIn('echo "02$tail"', self.text)

    def test_default_address_is_rejected(self):
        self.assertIn('00:00:00:00:00:00', self.text)

    def test_address_is_persisted(self):
        self.assertIn('/var/lib/j4-bluetooth', self.text)
        self.assertIn('remember_address', self.text)

    def test_wcnss_default_address_is_rejected(self):
        # 00:00:00:* is what the controller reports with no NVM address, and it
        # was accepted before, so no public address was ever programmed.
        self.assertIn('00:00:00:*|ff:ff:ff:*|00:00:00:00:00:00) return 1',
                      self.text)

    def test_status_flags_an_unusable_address(self):
        self.assertIn('UNUSABLE: no public address', self.text)

    def test_pairing_scans_first(self):
        self.assertIn('Looking for $mac for 20 seconds', self.text)
        self.assertIn('echo "scan on"; sleep 20', self.text)

    def test_address_goes_through_the_driver_parameter(self):
        self.assertIn('BDADDR_FILE="$PARAM_DIR/bdaddr"', self.text,
                      'address must reach the driver through its bdaddr param')


class PackageTests(unittest.TestCase):
    def test_bluez_packages_are_the_alpine_3_23_names(self):
        self.assertEqual(bt.BLUEZ_PACKAGES,
                         ['bluez', 'bluez-openrc', 'bluez-btmgmt',
                          'bluez-deprecated'])

    def test_device_package_ships_bluez(self):
        apkbuild = (Path(__file__).resolve().parents[2]
                    / 'pmaports/device/downstream/device-samsung-j4primelte'
                    / 'APKBUILD').read_text()
        for package in bt.BLUEZ_PACKAGES:
            self.assertIn('\t%s\n' % package, apkbuild)


class RunlevelTests(unittest.TestCase):
    def test_customize_rootfs_enables_the_service(self):
        text = (Path(__file__).resolve().parent / 'customize-rootfs.py').read_text()
        self.assertIn("level / 'j4-bluetooth'", text)
        self.assertIn("'../../init.d/j4-bluetooth'", text)

    def test_service_runs_in_the_boot_runlevel(self):
        self.assertEqual(bt.RUNLEVEL_LINK,
                         Path('/etc/runlevels/boot/j4-bluetooth'))


if __name__ == '__main__':
    unittest.main()

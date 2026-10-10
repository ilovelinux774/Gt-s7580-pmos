#!/usr/bin/env python3
"""Synthetic tests only: never initialize Wi-Fi or alter host network settings."""
import configparser
import contextlib
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('wifi_setup', Path(__file__).with_name('wifi-setup.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
UUID = '12345678-1234-1234-1234-123456789abc'
OTHER = '87654321-4321-4321-4321-cba987654321'


class WifiSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def firmware(self):
        for name in setup.FW_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test fixture only, not firmware')

    def test_installer_only_saves_config_and_schedules_service(self):
        profiles = self.root / 'profiles'
        profiles.mkdir()
        keyfile = profiles / 'test.nmconnection'
        keyfile.write_text('[wifi-security]\npsk=FAKE_TEST_VALUE_NOT_A_REAL_PASSWORD\n')
        paths = dict(HELPER=self.root / 'bin/j4-wifi', INIT=self.root / 'init.d/j4-wifi',
                     NM_CONFIG=self.root / 'conf.d/90-j4.conf', STATE=self.root / 'state',
                     RUNLEVEL_LINK=self.root / 'default/j4-wifi', NM_PROFILES=profiles)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[:3] == ('nmcli', 'connection', 'modify'):
                return ''
            if args[:2] == ('nmcli', '-t'):
                return f'{UUID}:{keyfile}'
            if args == ('rc-update', 'add', 'j4-wifi', 'default'):
                paths['RUNLEVEL_LINK'].parent.mkdir(parents=True)
                paths['RUNLEVEL_LINK'].symlink_to(paths['INIT'])
                return ''
            raise AssertionError(f'Unexpected operation: {args}')

        output = io.StringIO()
        with patch.multiple(setup, **paths), patch.object(setup, 'preflight', return_value=(UUID, keyfile)), \
                patch.object(setup, 'run', side_effect=fake_run), patch.object(setup.os, 'sync'), \
                patch.object(setup.os, 'umask'), contextlib.redirect_stdout(output):
            setup.install()
        self.assertIn('INSTALL_OK', output.getvalue())
        self.assertNotIn('FAKE_TEST_VALUE_NOT_A_REAL_PASSWORD', output.getvalue())
        self.assertEqual(stat.S_IMODE(paths['STATE'].stat().st_mode), 0o700)
        backups = list(paths['STATE'].glob('backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE((backups[0] / 'profile.nmconnection').stat().st_mode), 0o600)
        self.assertEqual((backups[0] / 'profile.nmconnection').read_bytes(), keyfile.read_bytes())
        self.assertEqual(paths['NM_CONFIG'].read_text(), setup.NM_TEXT)
        self.assertEqual(paths['INIT'].read_text(), setup.INIT_TEXT)
        self.assertEqual(stat.S_IMODE(paths['HELPER'].stat().st_mode), 0o755)
        self.assertTrue(paths['RUNLEVEL_LINK'].is_symlink())
        self.assertIn('"cold_boot_verified": false', (paths['STATE'] / 'installed.json').read_text())
        self.assertEqual(len(calls), 3)
        self.assertIn('permanent', calls[0])
        self.assertNotIn('psk', ' '.join(calls[0]))

    def test_complete_firmware(self):
        self.firmware()
        setup.check_firmware(self.root)

    def test_incomplete_firmware_rejected(self):
        self.firmware()
        (self.root / 'wcnss.b06').unlink()
        with self.assertRaisesRegex(setup.SetupError, 'wcnss.b06'):
            setup.check_firmware(self.root)

    def test_android_symlink_rejected(self):
        self.firmware()
        p = self.root / 'wcnss.mdt'
        p.unlink()
        p.symlink_to('/firmware/image/wcnss.mdt')
        with self.assertRaisesRegex(setup.SetupError, 'wcnss.mdt'):
            setup.check_firmware(self.root)

    def test_uuid_selected_only_for_wlan0(self):
        output = f'{OTHER}:rndis0\n{UUID}:wlan0\n'
        self.assertEqual(setup.active_uuid(output), UUID)

    def test_ambiguous_or_absent_connection_rejected(self):
        for output in (f'{UUID}:rndis0', f'{UUID}:wlan0\n{OTHER}:wlan0', 'bad-uuid:wlan0'):
            with self.assertRaises(setup.SetupError):
                setup.active_uuid(output)

    def test_saved_psk_detection(self):
        p = self.root / 'profile'
        p.write_text('[wifi-security]\nkey-mgmt=wpa-psk\npsk=FAKE_TEST_VALUE%only\n')
        self.assertTrue(setup.saved_psk(p))
        p.write_text('[wifi-security]\npsk=FAKE_TEST_VALUE\npsk-flags=2\n')
        self.assertFalse(setup.saved_psk(p))
        p.write_text('[wifi-security]\nkey-mgmt=wpa-psk\n')
        self.assertFalse(setup.saved_psk(p))

    def test_bad_keyfile_never_prints_contents(self):
        p = self.root / 'profile'
        p.write_text('FAKE_PRIVATE_TEST_STRING without an INI section')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertFalse(setup.saved_psk(p))
        self.assertEqual(out.getvalue(), '')

    def test_atomic_file_modes(self):
        p = self.root / 'subdir' / 'file'
        setup.atomic_write(p, b'first', 0o600)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        setup.atomic_write(p, b'second', 0o755)
        self.assertEqual(p.read_bytes(), b'second')
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o755)
        self.assertEqual(list(p.parent.iterdir()), [p])

    def test_network_config_is_scoped(self):
        c = configparser.ConfigParser(interpolation=None)
        c.read_string(setup.NM_TEXT)
        self.assertEqual(set(c.sections()), {'device-j4-p2p', 'connection-j4-wlan'})
        self.assertEqual(c['device-j4-p2p']['match-device'], 'interface-name:p2p0')
        self.assertFalse(c.getboolean('device-j4-p2p', 'managed'))
        self.assertEqual(c['connection-j4-wlan']['match-device'], 'interface-name:wlan0')
        self.assertEqual(c['connection-j4-wlan']['wifi.cloned-mac-address'], 'permanent')
        self.assertNotIn('rndis0', setup.NM_TEXT)
        self.assertNotIn('wifi.scan-rand-mac-address', setup.NM_TEXT)

    def test_late_service_order_and_no_restart(self):
        self.assertIn('need localmount networkmanager wpa_supplicant', setup.INIT_TEXT)
        self.assertIn('after j4-usb-debug', setup.INIT_TEXT)
        self.assertNotIn('restart', setup.INIT_TEXT)
        self.assertNotIn('before networkmanager', setup.INIT_TEXT)

    def test_boot_skips_existing_wlan(self):
        with patch.object(setup, 'check_device'), patch.object(setup, 'WLAN', Mock(exists=lambda: True)), \
                patch.object(setup.subprocess, 'Popen') as spawn:
            setup.boot()
            spawn.assert_not_called()

    def test_worker_does_not_reinitialize_existing_wlan(self):
        with patch.object(setup, 'check_device'), patch.object(setup, 'check_firmware'), \
                patch.object(setup, 'WLAN', Mock(exists=lambda: True)), \
                patch.object(setup.os, 'open') as open_device:
            setup.radio_worker()
            open_device.assert_not_called()

    def test_previous_failed_attempt_not_retried(self):
        marker = self.root / 'attempted'
        marker.touch()
        with patch.object(setup, 'check_device'), patch.object(setup, 'check_firmware'), \
                patch.object(setup, 'WLAN', Mock(exists=lambda: False)), \
                patch.object(setup, 'ATTEMPT', marker), patch.object(setup.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(setup.SetupError, 'already made'):
                setup.boot()
            spawn.assert_not_called()

    def test_worker_timeout_does_not_wait_again(self):
        worker = Mock()
        worker.wait.side_effect = subprocess.TimeoutExpired('fixture', 65)
        with patch.object(setup, 'check_device'), patch.object(setup, 'check_firmware'), \
                patch.object(setup, 'WLAN', Mock(exists=lambda: False)), \
                patch.object(setup, 'ATTEMPT', self.root / 'attempted'), \
                patch.object(setup.subprocess, 'Popen', return_value=worker):
            with self.assertRaisesRegex(setup.SetupError, 'timed out'):
                setup.boot()
            worker.kill.assert_called_once()
            self.assertEqual(worker.wait.call_count, 1)


if __name__ == '__main__':
    unittest.main()

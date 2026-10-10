#!/usr/bin/env python3
"""Check the parts of the desktop that have to survive a rootfs reflash.

Installing LXQt on the phone with lxqt-setup.py works, but every rootfs flash
wiped it again, so the desktop now ships in the image: pmbootstrap builds with
ui = lxqt, the J4-specific X fixes ride along in the rootfs overlay, and
customize-rootfs.py enables the services. These tests guard that wiring; they
touch no device and need no network.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / 'ci/j4primelte'
OVERLAY = CI / 'rootfs'
MARKER = 'J4_LXQT_SETUP_V1'

OVERLAY_FILES = (
    'usr/local/sbin/j4-screen-on',
    'etc/xdg/autostart/j4-screen-on.desktop',
    'etc/init.d/j4-x-sysfs',
)


class ImageDesktopTests(unittest.TestCase):
    def test_the_overlay_carries_the_j4_x_fixes(self):
        for relative in OVERLAY_FILES:
            path = OVERLAY / relative
            self.assertTrue(path.is_file(), 'missing overlay file: ' + relative)
            self.assertIn(MARKER, path.read_text(),
                          'overlay file lost its marker: ' + relative)

    def test_the_screen_on_helper_is_executable_in_the_overlay(self):
        # customize-rootfs.py only chmods 755 for files starting with #!, so a
        # lost shebang would ship a script X cannot run.
        for relative in ('usr/local/sbin/j4-screen-on', 'etc/init.d/j4-x-sysfs'):
            self.assertTrue((OVERLAY / relative).read_text().startswith('#!'),
                            relative + ' has no shebang')

    def test_xorg_gets_the_framebuffer_before_any_display_manager(self):
        text = (OVERLAY / 'etc/init.d/j4-x-sysfs').read_text()
        for service in ('tinydm', 'lightdm', 'display-manager'):
            self.assertRegex(text, r'before\s+[^\n]*\b%s\b' % service)

    def test_the_image_is_built_with_the_desktop_ui(self):
        config = (CI / 'prepare-pmos.sh').read_text()
        self.assertIn('ui = lxqt', config)

    def test_the_extra_desktop_packages_are_device_dependencies(self):
        apkbuild = (ROOT / 'pmaports/device/downstream/device-samsung-j4primelte'
                    / 'APKBUILD')
        text = apkbuild.read_text()
        for package in ('onboard', 'unclutter-xfixes', 'network-manager-applet'):
            self.assertIn(package, text)

    def test_the_image_enables_the_desktop_services(self):
        script = (CI / 'customize-rootfs.py').read_text()
        self.assertIn("'j4-x-sysfs'", script)
        self.assertIn('display_manager', script)
        # The splash service owns fb0, so it must step aside for X.
        self.assertIn('j4-fb-splash', script)


if __name__ == '__main__':
    unittest.main()

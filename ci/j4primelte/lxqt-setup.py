#!/usr/bin/python3
# J4_LXQT_SETUP_V1
"""Install pmOS's touch/tablet LXQt stack on the running SM-J415F pmOS system.

No reflashing and no deletion: this only apk-adds postmarketos-ui-lxqt (the
tablet-tuned LXQt profile with onboard OSK autostart) plus the on-screen-keyboard
and Wi-Fi applet, writes two small autostart files, and enables the tinydm
display manager. It STOPS the j4-fb-splash service first (both want fb0).

It also installs the j4-x-sysfs boot service. This Samsung MDSS framebuffer
registers with framebuffer_alloc(..., NULL), so /sys/class/graphics/fb0 has no
"device" symlink; xorg-server 21.1.23's fbdevhw probes readlink
/sys/class/graphics/fb0/device/subsystem and silently rejects the device when
that link is missing ("No devices detected"). The service bind-mounts a tiny
fake sysfs tree over the fb0 class directory BEFORE tinydm starts X, and the
session hook re-writes the MDSS backlight register (it can read 256 while the
panel stays dark until written).

Run --check first (only an APK index refresh), then --install ON THE PHONE.
USB SSH/RNDIS, Wi-Fi profiles, firmware staging and all partitions are untouched.
Rendering is software (no DRM/GPU driver on this kernel) — expect a functional
but not silky-smooth desktop.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

MARKER = 'J4_LXQT_SETUP_V1'
MIN_FREE = 1000 * 1024 * 1024
PACKAGES = ['postmarketos-ui-lxqt', 'onboard', 'unclutter-xfixes', 'network-manager-applet']
SCREEN_ON = Path('/usr/local/sbin/j4-screen-on')
AUTOSTART = Path('/etc/xdg/autostart/j4-screen-on.desktop')
BACKUP = Path('/var/lib/j4-lxqt')
INPUT_DEVICES = Path('/proc/bus/input/devices')
FB_INIT = Path('/etc/init.d/j4-fb-splash')
FB_STARTED = Path('/run/openrc/started/j4-fb-splash')
X_SYSFS_INIT = Path('/etc/init.d/j4-x-sysfs')

SCREEN_ON_TEXT = '''#!/bin/sh
# J4_LXQT_SETUP_V1 - keep the panel lit on this MDSS driver.
# The MDSS backlight register only takes effect when written: it can read
# 256 while the panel stays dark. Wake it at session start and once more
# after the X session settles.
echo 255 > /sys/class/leds/lcd-backlight/brightness 2>/dev/null
sleep 2
echo 255 > /sys/class/leds/lcd-backlight/brightness 2>/dev/null
xset s off 2>/dev/null || true
xset s noblank 2>/dev/null || true
xset -dpms 2>/dev/null || true
'''

AUTOSTART_TEXT = '''# J4_LXQT_SETUP_V1
[Desktop Entry]
Type=Application
Name=J4 screen always on
Comment=Disable X blanking/DPMS on the J4+ debug build
Exec=/usr/local/sbin/j4-screen-on
NoDisplay=true
X-GNOME-Autostart-enabled=true
'''

FALLBACK_INIT = '''#!/sbin/openrc-run
# J4_LXQT_SETUP_V1 - fallback only when tinydm is unavailable.
name="J4+ LXQt session"
description="Start LXQt through startx on a spare VT"
supervisor="supervise-daemon"
command="/usr/local/sbin/j4-screen-on"
command_args="--session"
respawn_delay=5
respawn_max=0

depend() {
    need localmount dbus
    after j4-usb-debug
}
'''

FALLBACK_SESSION = '''#!/bin/sh
# J4_LXQT_SETUP_V1 - fallback X session (tinydm normally handles this).
if [ "$(id -u)" = 0 ] && [ -d /home/user ]; then
    exec su -s /bin/sh user -c 'exec startx /usr/bin/startlxqt -- :0 vt7 -nolisten tcp'
fi
exec startx /usr/bin/startlxqt -- :0 vt7 -nolisten tcp
'''

X_SYSFS_INIT_TEXT = '''#!/sbin/openrc-run
# J4_LXQT_SETUP_V1 - make Xorg fbdev accept the MDSS framebuffer.
# This kernel registers fb0 without a parent device, so the sysfs link
# /sys/class/graphics/fb0/device/subsystem does not exist and xorg-server
# 21.1.23 fbdevhw silently rejects the frame buffer. Fake the link via a
# bind mount before tinydm starts X, and wake the panel backlight after.
name="j4-x-sysfs"
description="Fake fb0 sysfs parent for Xorg fbdev and wake the backlight"

depend() {
    need localmount
    before tinydm
}

start() {
    ebegin "Preparing fb0 sysfs for Xorg fbdev"
    mkdir -p /tmp/x-fb-sysfs/device
    ln -sf /sys/bus/platform /tmp/x-fb-sysfs/device/subsystem
    target="$(readlink -f /sys/class/graphics/fb0)"
    umount "$target" 2>/dev/null
    mount --bind /tmp/x-fb-sysfs "$target"
    ( sleep 8; echo 255 > /sys/class/leds/lcd-backlight/brightness 2>/dev/null ) >/dev/null 2>&1 &
    eend $?
}

stop() {
    ebegin "Removing fb0 sysfs bind mount"
    umount "$(readlink -f /sys/class/graphics/fb0)" 2>/dev/null
    eend 0
}
'''


class SetupError(Exception):
    pass


def run(*args, timeout=600):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise SetupError(f'{" ".join(args[:3])}... failed: '
                         f'{result.stderr.strip() or result.stdout.strip()}')
    return result.stdout


def find_touch_event(text):
    """Return /dev/input/eventN for sec_touchscreen, or None."""
    for block in text.split('\n\n'):
        if 'Name="sec_touchscreen"' not in block:
            continue
        match = re.search(r'^H: Handlers=.*?\b(event\d+)\b', block, re.M)
        if match:
            return '/dev/input/' + match.group(1)
    return None


def free_bytes(path='/'):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def check_device():
    if os.geteuid() != 0:
        raise SetupError('Run as root on the phone, not on the PC.')
    if struct.calcsize('P') != 4 or not os.uname().machine.startswith('arm'):
        raise SetupError('Only the 32-bit ARM phone is supported.')
    info = Path('/usr/share/deviceinfo/deviceinfo')
    if not info.is_file() or 'samsung-j4primelte' not in info.read_text():
        raise SetupError('Installed deviceinfo is not samsung-j4primelte.')


def planned_packages():
    if not shutil.which('apk'):
        raise SetupError('apk not found; this is not the pmOS system.')
    run('apk', 'update', timeout=300)  # index refresh is the only --check side effect
    output = run('apk', 'add', '--simulate', *PACKAGES, timeout=120)
    return output


def package_installed(name):
    """True when apk reports the package as installed."""
    result = subprocess.run(['apk', 'info', '-e', name], capture_output=True, text=True)
    return result.returncode == 0


def plan_needs_review(plan, installed):
    """A blank apk plan is fine on re-runs: everything is already installed."""
    if installed:
        return False
    return not any(line.strip().startswith(('Installing', 'Adding')) or 'postmarketos-ui-lxqt' in line
                   for line in plan.splitlines())


def preflight():
    check_device()
    free = free_bytes()
    if free < MIN_FREE:
        raise SetupError(f'Only {free // (1024 * 1024)} MiB free; need >= '
                         f'{MIN_FREE // (1024 * 1024)} MiB. Do not wipe anything yet.')
    touch = find_touch_event(INPUT_DEVICES.read_text())
    if not touch:
        raise SetupError('sec_touchscreen input device not found; fix touch before installing.')
    plan = planned_packages()
    if plan_needs_review(plan, package_installed('postmarketos-ui-lxqt')):
        raise SetupError('apk simulation did not mention postmarketos-ui-lxqt; inspect the plan.')
    if not plan.strip():
        plan = 'all listed packages already installed; nothing to download'
    return touch, plan


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.j4-lxqt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def display_service():
    for name in ('tinydm', 'display-manager'):
        if Path('/etc/init.d', name).is_file():
            return name
    return None


def owned_by_helper(path):
    """True when we may replace this file.

    Files we write carry the J4_LXQT_SETUP_V1 marker; the first release's
    autostart file had no marker line, so accept anything that launches
    our own j4-screen-on script as ours too.
    """
    text = path.read_text(errors='replace')
    return MARKER in text[:256] or ('Exec=' + str(SCREEN_ON)) in text


def install():
    touch, plan = preflight()
    print(f'Touch input: {touch}')
    print('---- apk plan (simulation) ----')
    print(plan.strip())
    print('--------------------------------')
    for path in (SCREEN_ON, AUTOSTART, X_SYSFS_INIT):
        if path.is_symlink():
            raise SetupError(f'Refusing to replace a symlink: {path}')
        if path.exists() and not owned_by_helper(path):
            raise SetupError(f'Existing file is not owned by this helper: {path}')
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUP.chmod(0o700)
    for path in (SCREEN_ON, AUTOSTART, X_SYSFS_INIT):
        if path.exists():
            shutil.copyfile(path, BACKUP / ('old-' + path.name))
    # CRITICAL order: give up fb0 BEFORE any X server may claim it.
    if FB_INIT.exists():
        print('Stopping j4-fb-splash so X can own the framebuffer...')
        subprocess.run(['rc-service', 'j4-fb-splash', 'stop'], capture_output=True, text=True)
        subprocess.run(['rc-update', 'del', 'j4-fb-splash', 'boot'], capture_output=True, text=True)
        if FB_STARTED.exists():
            raise SetupError('j4-fb-splash did not stop; refusing to start X.')
    print('Installing packages (this downloads and unpacks, a few minutes)...')
    run('apk', 'add', '--no-progress', *PACKAGES, timeout=1800)
    atomic_write(SCREEN_ON, SCREEN_ON_TEXT.encode(), 0o755)
    atomic_write(AUTOSTART, AUTOSTART_TEXT.encode(), 0o644)
    atomic_write(X_SYSFS_INIT, X_SYSFS_INIT_TEXT.encode(), 0o755)
    run('rc-update', 'add', 'j4-x-sysfs', 'default')
    # The fake sysfs link must exist before any X server probes /dev/fb0.
    subprocess.run(['rc-service', 'j4-x-sysfs', 'start'], capture_output=True, text=True)
    service = display_service()
    if service:
        run('rc-update', 'add', service, 'default')
        subprocess.run(['rc-service', service, 'restart'], capture_output=True, text=True)
    else:
        # Rare fallback: no tinydm/display-manager service shipped.
        for path in (Path('/usr/local/sbin/j4-lxqt-session'), Path('/etc/init.d/j4-lxqt')):
            if path.exists() and MARKER not in path.read_text(errors='replace')[:256]:
                raise SetupError(f'Unexpected existing file: {path}')
        atomic_write(Path('/usr/local/sbin/j4-lxqt-session'), FALLBACK_SESSION.encode(), 0o755)
        atomic_write(Path('/etc/init.d/j4-lxqt'), FALLBACK_INIT.encode(), 0o755)
        run('rc-update', 'add', 'j4-lxqt', 'default')
        run('rc-service', 'j4-lxqt', 'restart')
        service = 'j4-lxqt'
    os.sync()
    print(f'INSTALL_OK: pmOS tablet LXQt installed; display service "{service}" started.')
    print('Look at the phone: the LXQt tablet desktop should appear within seconds.')
    print('Touch = click; onboard keyboard autostarts; Wi-Fi profile was kept.')
    print('USB SSH/RNDIS untouched. Software rendering: usable, not GPU-accelerated.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            touch, plan = preflight()
            print(f'CHECK_OK: device OK; free space {free_bytes() // (1024*1024)} MiB; '
                  f'touch {touch}; {len(PACKAGES)} packages resolvable.')
            print('Only the APK index cache was refreshed; no packages or settings changed.')
        else:
            install()
        return 0
    except (SetupError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        print('Report this message; do not wipe or reflash anything.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/python3
# J4_WIFI_HELPER_V1
"""Opt-in live-device Wi-Fi persistence for our SM-J415F pmOS image.

Run --check, then --install ON THE PHONE while wlan0 is already connected.
No flashing, stock mounts, firmware redistribution, network/USB restart,
password printing, RTC writes, or weaker WPA settings. Firmware must already
have been copied and successfully tested. --install schedules a late OpenRC
service; it does not rerun firmware initialization on the current connection.

Private backups, including the existing NM keyfile, stay under /var/lib/j4-wifi
(mode 0700), NOT the publicly shareable /var/log/j4 diagnostics directory.
"""
import argparse
import configparser
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time

MARKER = 'J4_WIFI_HELPER_V1'
HELPER = Path('/usr/local/sbin/j4-wifi')
INIT = Path('/etc/init.d/j4-wifi')
NM_CONFIG = Path('/etc/NetworkManager/conf.d/90-j4-legacy-wifi.conf')
STATE = Path('/var/lib/j4-wifi')
NM_PROFILES = Path('/etc/NetworkManager/system-connections')
RUNLEVEL_LINK = Path('/etc/runlevels/default/j4-wifi')
WLAN = Path('/sys/class/net/wlan0')
P2P = Path('/sys/class/net/p2p0')
WCNSS = Path('/dev/wcnss_wlan')
FWPATH = Path('/sys/module/wlan/parameters/fwpath')
ATTEMPT = Path('/run/j4-wifi-attempted')
UUID_RE = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
FW_FILES = ['wcnss.mdt'] + [f'wcnss.b{n}' for n in ('00', '01', '02', '04', '06', '09', '10', '11', '12')]
FW_FILES += ['wlan/prima/' + name for name in (
    'WCNSS_qcom_wlan_nv.bin', 'WCNSS_qcom_cfg.ini', 'WCNSS_cfg.dat',
    'WCNSS_wlan_dictionary.dat')]

# Match only the legacy Wi-Fi interfaces. Do not replace the global keyfile
# unmanaged-devices list, and do not change rndis0/usb0/ethernet defaults.
NM_TEXT = '''# J4_WIFI_HELPER_V1
# The tested compatibility combination; individual causes were not isolated.
[device-j4-p2p]
match-device=interface-name:p2p0
managed=false

[connection-j4-wlan]
match-device=interface-name:wlan0
wifi.cloned-mac-address=permanent
'''

# Deliberately start AFTER NM and wpa_supplicant, reproducing the successful
# manual sequence. If started before wpa_supplicant, its legacy init script
# could automatically pick p2p0 as the first wireless interface.
INIT_TEXT = '''#!/sbin/openrc-run
# J4_WIFI_HELPER_V1
name="J4+ Wi-Fi initialization"
description="Initialize already-provisioned WCNSS after NetworkManager is ready"

depend() {
    need localmount networkmanager wpa_supplicant
    after j4-usb-debug
}

start() {
    ebegin "Initializing J4+ WCNSS Wi-Fi"
    checkpath -d -m 0755 /var/log/j4 || return 1
    /usr/local/sbin/j4-wifi --boot >>/var/log/j4/wifi-start.log 2>&1
    eend $?
}

stop() {
    # One-shot initialization only. NM owns live connections; do not unload WLAN.
    return 0
}
'''


class SetupError(Exception):
    pass


def run(*args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise SetupError(f'{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout.strip()


def check_device():
    if os.geteuid() != 0:
        raise SetupError('Run as root on the phone, not on the PC.')
    if struct.calcsize('P') != 4 or not os.uname().machine.startswith('arm'):
        raise SetupError('This helper is only for the tested 32-bit ARM phone image.')
    info = Path('/usr/share/deviceinfo/deviceinfo')
    if not info.is_file() or 'samsung-j4primelte' not in info.read_text():
        raise SetupError('The installed deviceinfo is not samsung-j4primelte.')
    osinfo = Path('/etc/os-release').read_text()
    if not re.search(r'^ID=[\"\']?postmarketos[\"\']?$', osinfo, re.M):
        raise SetupError('Not the expected postmarketOS installation.')
    if not os.uname().release.startswith('3.18.140-pmos-j4-debug'):
        raise SetupError('Kernel differs from the one used in the successful live test.')


def check_firmware(base=Path('/lib/firmware')):
    bad = []
    for name in FW_FILES:
        p = base / name
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            bad.append(name)
    if bad:
        raise SetupError('Missing/empty/symlinked firmware: ' + ', '.join(bad)
                         + '. Copy real files from your phone first; no radio initialization attempted.')


def active_uuid(output):
    rows = [line.split(':', 1) for line in output.splitlines() if ':' in line]
    ids = [uuid for uuid, device in rows if device == 'wlan0' and UUID_RE.fullmatch(uuid)]
    if len(ids) != 1:
        raise SetupError('Exactly one working connection on wlan0 is required for installation.')
    return ids[0]


def profile_filename(uuid, output):
    for line in output.splitlines():
        key, sep, filename = line.partition(':')
        if sep and key == uuid:
            path = Path(filename)
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                break
            allowed = (NM_PROFILES,
                       Path('/run/NetworkManager/system-connections'),
                       Path('/usr/lib/NetworkManager/system-connections'))
            if not any(path.parent.resolve() == p.resolve() for p in allowed):
                break
            return path
    raise SetupError('Cannot locate the working NM keyfile safely. Nothing has been installed.')


def saved_psk(path):
    # This reads the local keyfile only to return a boolean. NEVER print its
    # contents, parser exceptions, or the PSK; never include it in diagnostics.
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read_string(path.read_text())
        for section in ('wifi-security', '802-11-wireless-security'):
            if config.has_section(section):
                return (config.get(section, 'psk-flags', fallback='0').strip() == '0'
                        and bool(config.get(section, 'psk', fallback='').strip()))
    except (OSError, UnicodeError, configparser.Error):
        pass
    return False


def preflight():
    check_device()
    check_firmware()
    for command in ('nmcli', 'ip', 'rc-update'):
        if not shutil.which(command):
            raise SetupError(f'Required command not found: {command}')
    if not WCNSS.exists() or not stat.S_ISCHR(WCNSS.stat().st_mode) or not FWPATH.is_file():
        raise SetupError('Expected WCNSS control device or wlan fwpath parameter is missing.')
    if not re.match(r'^100\b', run('nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'wlan0')):
        raise SetupError('Keep wlan0 connected using the tested settings before installing.')
    uuid = active_uuid(run('nmcli', '-t', '-f', 'UUID,DEVICE', 'connection', 'show', '--active'))
    path = profile_filename(uuid, run('nmcli', '-t', '--escape', 'no', '-f', 'UUID,FILENAME',
                                    'connection', 'show'))
    if not saved_psk(path):
        raise SetupError('The active keyfile has no saved, unattended PSK. Keep the current connection; '
                         'report this message without sharing the password/keyfile. Nothing installed.')
    for path_to_write in (HELPER, INIT, NM_CONFIG):
        if path_to_write.is_symlink():
            raise SetupError(f'Refusing to replace a symlink: {path_to_write}')
        if path_to_write.exists() and MARKER not in path_to_write.read_text(errors='replace')[:512]:
            raise SetupError(f'Existing file is not owned by this helper: {path_to_write}')
    if RUNLEVEL_LINK.exists() or RUNLEVEL_LINK.is_symlink():
        if not RUNLEVEL_LINK.is_symlink() or RUNLEVEL_LINK.resolve() != INIT:
            raise SetupError('An unexpected j4-wifi runlevel entry already exists.')
    return uuid, path


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.j4-wifi-', dir=path.parent)
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


def install():
    uuid, keyfile = preflight()
    source = Path(__file__).read_bytes()
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink() or not STATE.is_dir():
        raise SetupError('Private backup directory is not a regular directory.')
    STATE.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(tempfile.mkdtemp(prefix='backup-' + stamp + '-', dir=STATE))
    metadata = {'profile_uuid': uuid, 'profile_filename': str(keyfile),
                'runlevel_previously_enabled': RUNLEVEL_LINK.is_symlink(),
                'previous_files': {}, 'contains_private_wifi_credentials': True}
    shutil.copyfile(keyfile, backup / 'profile.nmconnection')
    (backup / 'profile.nmconnection').chmod(0o600)
    for index, path in enumerate((HELPER, INIT, NM_CONFIG)):
        if path.exists():
            name = f'old-file-{index}'
            shutil.copyfile(path, backup / name)
            metadata['previous_files'][str(path)] = {'backup': name, 'mode': stat.S_IMODE(path.stat().st_mode)}
        else:
            metadata['previous_files'][str(path)] = None
    atomic_write(backup / 'manifest.json', (json.dumps(metadata, indent=2) + '\n').encode(), 0o600)
    print(f'PRIVATE backup: {backup} (contains the NM keyfile; do NOT upload it)', flush=True)
    atomic_write(HELPER, source, 0o755)
    atomic_write(INIT, INIT_TEXT.encode(), 0o755)
    atomic_write(NM_CONFIG, NM_TEXT.encode(), 0o644)
    # No connection up/down, NM reload/restart, firmware open, or USB changes.
    run('nmcli', 'connection', 'modify', 'uuid', uuid,
        '802-11-wireless.cloned-mac-address', 'permanent',
        'connection.autoconnect', 'yes', 'connection.autoconnect-priority', '100')
    saved_file = profile_filename(uuid, run('nmcli', '-t', '--escape', 'no', '-f',
                                          'UUID,FILENAME', 'connection', 'show'))
    if saved_file.parent.resolve() != NM_PROFILES.resolve() or not saved_psk(saved_file):
        raise SetupError('Profile is not persistently stored with a PSK. Do not reboot yet; private backup is available above.')
    run('rc-update', 'add', 'j4-wifi', 'default')
    if not RUNLEVEL_LINK.is_symlink() or RUNLEVEL_LINK.resolve() != INIT:
        raise SetupError('Runlevel link verification failed; private backups are available above.')
    atomic_write(STATE / 'installed.json', (json.dumps({'profile_uuid': uuid,
                 'backup': str(backup), 'cold_boot_verified': False}, indent=2) + '\n').encode(), 0o600)
    os.sync()
    print('INSTALL_OK: working profile saved; P2P policy installed; late OpenRC autostart enabled.')
    print('No service was restarted or connection reactivated. USB/SSH files were not changed.')
    print('A reboot is still required to validate the cold-boot path. Do not upload the private backup.')


def radio_worker():
    check_device()
    check_firmware()
    if WLAN.exists():
        print('wlan0 already exists; refusing to reinitialize a live driver.', flush=True)
        return
    deadline = time.monotonic() + 15
    while not (WCNSS.exists() and FWPATH.is_file()):
        if time.monotonic() >= deadline:
            raise SetupError('WCNSS control nodes did not appear.')
        time.sleep(0.25)
    if not stat.S_ISCHR(WCNSS.stat().st_mode):
        raise SetupError('wcnss_wlan is not a character device.')
    print('Opening WCNSS once; no calibration-data writes.', flush=True)
    fd = os.open(WCNSS, os.O_RDWR | os.O_CLOEXEC)
    os.close(fd)
    time.sleep(5)
    print('Starting the built-in wlan driver once (fwpath=sta).', flush=True)
    FWPATH.write_text('sta\n')
    deadline = time.monotonic() + 10
    while not WLAN.exists():
        if time.monotonic() >= deadline:
            raise SetupError('wlan0 was not created; inspect kernel logs. No automatic retry.')
        time.sleep(0.25)
    if P2P.exists():
        run('ip', 'link', 'set', 'dev', 'p2p0', 'down')
    print('WLAN_INTERFACE_READY: NetworkManager handles authentication and DHCP.', flush=True)


def boot():
    check_device()
    print('\n=== J4 Wi-Fi boot attempt ===', flush=True)
    if WLAN.exists():
        print('wlan0 already exists; no firmware/driver restart performed.', flush=True)
        return
    check_firmware()
    try:
        fd = os.open(ATTEMPT, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SetupError('An initialization attempt was already made this boot; inspect logs, do not loop.')
    worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--radio-worker'])
    try:
        result = worker.wait(timeout=65)
    except subprocess.TimeoutExpired:
        worker.kill()
        # Do not wait indefinitely for a driver stuck in uninterruptible I/O.
        # USB debug is a separate service and was already started.
        raise SetupError('Radio initialization timed out. No retry scheduled; use USB to inspect logs.')
    if result:
        raise SetupError(f'Radio worker exited with status {result}; no retry scheduled.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--install', action='store_true')
    group.add_argument('--boot', action='store_true')
    group.add_argument('--radio-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.check:
            uuid, _ = preflight()
            print(f'CHECK_OK: correct device; {len(FW_FILES)} firmware files; active profile {uuid}; saved PSK present.')
            print('No password value was printed and no settings were changed.')
        elif args.install:
            install()
        elif args.boot:
            boot()
        else:
            radio_worker()
        return 0
    except (SetupError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        print('Keep USB connected. No flashing or network-service restart was performed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

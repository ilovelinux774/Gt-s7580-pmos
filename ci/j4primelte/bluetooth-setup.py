#!/usr/bin/env python3
"""J4+ Bluetooth bring-up helper installer (WCNSS / hci_smd + BlueZ).

Installs a persistent OpenRC service that waits for the hci_smd driver to
expose the WCNSS Bluetooth controller, gives it a stable public address,
powers it on and starts BlueZ so keyboards can be paired.

    python3 bluetooth-setup.py --check     report the current state
    python3 bluetooth-setup.py --install   install the service and bring BT up
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HELPER = Path('/usr/local/sbin/j4-bt')
INIT = Path('/etc/init.d/j4-bluetooth')
CONF = Path('/etc/conf.d/j4-bluetooth')
RUNLEVEL_LINK = Path('/etc/runlevels/boot/j4-bluetooth')
PARAM_DIR = Path('/sys/module/hci_smd/parameters')
STATE_FILE = PARAM_DIR / 'state'
DEVICEINFO = Path('/usr/share/deviceinfo/deviceinfo')

SCRIPT_TEXT = r"""#!/bin/sh
# J4_BT_V1 - bring up the J4+ WCNSS (WCN3620) Bluetooth controller and pair
# input devices with BlueZ.
#
# Why this exists: the j4primelte kernel has Bluetooth core support but no HCI
# transport, so the hci_smd driver (drivers/bluetooth/hci_smd.c) is needed to
# expose the controller. The SMD channels only appear once the WCNSS firmware
# boots, which j4-wifi does, so this helper waits for the driver to report a
# device, gives the controller a stable public address if it has none, powers
# it on, and starts the BlueZ stack so keyboards can be paired.
#
# Logs only under /var/log/j4; safe to re-run. The address is generated once
# and stored in /var/lib/j4-bluetooth so it survives reboots.

LOG=/var/log/j4/bluetooth.log
PARAM_DIR=/sys/module/hci_smd/parameters
STATE_FILE="$PARAM_DIR/state"
ENABLE_FILE="$PARAM_DIR/enable"
BDADDR_FILE="$PARAM_DIR/bdaddr"
ADDR_FILE=/var/lib/j4-bluetooth/address
TIMEOUT=${J4_BT_TIMEOUT:-90}
ADDRESS=${J4_BT_ADDRESS:-}
START_BLUETOOTHD=${J4_BT_START_BLUETOOTHD:-yes}

log() {
    mkdir -p /var/log/j4
    echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

driver_state() {
    if [ -r "$STATE_FILE" ]; then
        cat "$STATE_FILE" 2>/dev/null
    else
        echo "no-driver"
    fi
}

hci_addr() {
    cat /sys/class/bluetooth/hci0/address 2>/dev/null
}

addr_is_valid() {
    case "$1" in
        00:00:00:00:00:00|"") return 1 ;;
        ??:??:??:??:??:??) return 0 ;;
        *) return 1 ;;
    esac
}

random_address() {
    # Locally administered, unicast: 0x02 in the most significant byte.
    tail="";
    while : ; do
        byte=$(head -c 1 /dev/urandom 2>/dev/null | od -An -tx1 | tr -d ' \n')
        [ -n "$byte" ] || return 1
        tail="$tail:$byte"
        n=$(echo "$tail" | tr -cd ':' | wc -c)
        [ "$n" -ge 5 ] && break
    done
    echo "02$tail"
}

stored_address() {
    if [ -s "$ADDR_FILE" ]; then
        head -n 1 "$ADDR_FILE" | tr -d '\r\n'
    fi
}

remember_address() {
    mkdir -p "$(dirname "$ADDR_FILE")"
    chmod 700 "$(dirname "$ADDR_FILE")" 2>/dev/null
    echo "$1" > "$ADDR_FILE"
}

# Power the controller on. bluetoothctl runs the BlueZ power-up path (which is
# what triggers the driver's setup, where the public address is programmed);
# btmgmt and hciconfig are kernel-only fallbacks.
power_on() {
    if have bluetoothctl; then
        echo "power on" | bluetoothctl >/dev/null 2>&1 && return 0
    fi
    if have btmgmt; then
        btmgmt --index 0 power on >/dev/null 2>&1 && return 0
    fi
    if have hciconfig; then
        hciconfig hci0 up >/dev/null 2>&1 && return 0
    fi
    return 1
}

power_off() {
    if have bluetoothctl; then
        echo "power off" | bluetoothctl >/dev/null 2>&1 && return 0
    fi
    if have btmgmt; then
        btmgmt --index 0 power off >/dev/null 2>&1 && return 0
    fi
    if have hciconfig; then
        hciconfig hci0 down >/dev/null 2>&1 && return 0
    fi
    return 1
}

ensure_stack() {
    if ! have bluetoothd; then
        echo "bluez is not installed: apk add bluez bluez-openrc bluez-btmgmt bluez-deprecated"
        log "bluez missing"
        return 1
    fi
    if have rc-service; then
        rc-service dbus status >/dev/null 2>&1 || rc-service dbus start >/dev/null 2>&1
        rc-update add dbus default >/dev/null 2>&1
        if ! rc-service bluetooth status >/dev/null 2>&1; then
            rc-update add bluetooth default >/dev/null 2>&1
            rc-service bluetooth start >/dev/null 2>&1
        fi
    fi
    return 0
}

wait_for_device() {
    n=$TIMEOUT
    while [ "$n" -gt 0 ]; do
        if [ -d /sys/class/bluetooth/hci0 ]; then
            return 0
        fi
        state=$(driver_state)
        if [ "$state" = "0" ] || [ "$state" = "1" ]; then
            # Re-arm the scan; the driver gives up after its own timeout when
            # the WCNSS was not up yet.
            [ -w "$ENABLE_FILE" ] && echo 1 > "$ENABLE_FILE"
        fi
        sleep 2
        n=$((n - 2))
    done
    return 1
}

do_boot() {
    log "boot: waiting for hci0 (timeout ${TIMEOUT}s)"
    if ! wait_for_device; then
        log "FAIL: no hci device; driver state $(driver_state)"
        echo "j4-bt: no Bluetooth device. Is the hci_smd driver in this kernel?" >&2
        echo "j4-bt: driver state: $(driver_state) (0 idle, 1 waiting, 2 up)" >&2
        return 1
    fi
    log "hci0 present"

    addr=$(hci_addr)
    if addr_is_valid "$addr"; then
        log "controller address $addr"
    else
        addr="$ADDRESS"
        [ -n "$addr" ] || addr=$(stored_address)
        addr_is_valid "$addr" || addr=$(random_address)
        if [ -z "$addr" ]; then
            log "FAIL: could not obtain an address"
            return 1
        fi
        remember_address "$addr"
        if [ -w "$BDADDR_FILE" ]; then
            echo "$addr" > "$BDADDR_FILE"
            log "programming public address $addr"
            power_off
            sleep 1
        else
            log "WARN: $BDADDR_FILE not writable; address left to the controller"
        fi
    fi

    # BlueZ first: bluetoothd drives its own power-up sequence, and pairing
    # needs it running anyway.
    if [ "$START_BLUETOOTHD" = "yes" ]; then
        ensure_stack
    fi

    if ! power_on; then
        log "WARN: power on did not report success (see bluetoothctl)"
    else
        log "controller powered on: $(hci_addr)"
    fi
    return 0
}

do_status() {
    echo "hci_smd driver: $(driver_state) (0 idle, 1 waiting, 2 up)"
    if [ -d /sys/class/bluetooth/hci0 ]; then
        echo "hci0 address: $(hci_addr)"
        if have btmgmt; then
            echo "-- btmgmt info --"
            btmgmt --index 0 info 2>&1 | head -n 12
        fi
    else
        echo "hci0: absent"
    fi
    echo "bluez: $(have bluetoothd && echo installed || echo MISSING)"
    if have rc-service; then
        echo "dbus: $(rc-service dbus status >/dev/null 2>&1 && echo running || echo stopped)"
        echo "bluetooth: $(rc-service bluetooth status >/dev/null 2>&1 && echo running || echo stopped)"
    fi
    if [ -r /proc/bus/input/devices ]; then
        echo "-- input devices --"
        grep -iE '^[NHS]:|bluetooth|keyboard' /proc/bus/input/devices | head -n 30
    fi
}

do_scan() {
    ensure_stack
    echo "Scanning for 60 seconds. Note the MAC address of your device."
    ( echo "power on"; echo "agent on"; echo "default-agent"; echo "scan on"; sleep 60 ) | bluetoothctl 2>&1 | grep -viE '^(agent|bluetooth)' | tail -n 40
}

do_pair() {
    mac=$1
    pin=${2:-0000}
    if [ -z "$mac" ]; then
        do_scan
        return $?
    fi
    ensure_stack
    echo "Pairing $mac. If the controller asks for a PIN, this tries $pin."
    ( echo "power on"
      echo "agent on"
      echo "default-agent"
      sleep 1
      echo "pair $mac"
      sleep 12
      echo "$pin"
      sleep 3
      echo "trust $mac"
      sleep 2
      echo "connect $mac"
      sleep 6
      echo "info $mac"
    ) | bluetoothctl 2>&1 | tail -n 40
}

case "$1" in
    --boot)
        do_boot
        ;;
    --status|status)
        do_status
        ;;
    --scan|scan)
        do_scan
        ;;
    --pair|pair)
        do_pair "$2" "$3"
        ;;
    *)
        echo "usage: j4-bt --boot | --status | --scan | --pair [MAC] [PIN]" >&2
        exit 2
        ;;
esac
"""

INIT_TEXT = r"""#!/sbin/openrc-run
# J4_BLUETOOTH_V1 - bring up the J4+ WCNSS Bluetooth controller at boot.
name="j4-bluetooth"
description="Bring up the J4+ WCNSS Bluetooth controller and the BlueZ stack"

depend() {
    need localmount
    after j4-wifi
}

start() {
    ebegin "Bringing up J4+ Bluetooth"
    /usr/local/sbin/j4-bt --boot
    eend $?
}

stop() {
    # Bring-up only; leave the controller and bluetoothd running.
    return 0
}
"""

CONF_TEXT = r"""# J4_BLUETOOTH_V1 - J4+ Bluetooth bring-up tuning.
# Seconds to wait for the hci_smd driver to expose hci0. The WCNSS firmware
# (and with it the APPS_RIVA_BT_* channels) only exists once j4-wifi boots it.
J4_BT_TIMEOUT="90"
# Public address for the controller. Leave empty to generate one once and keep
# it in /var/lib/j4-bluetooth/address. The WCNSS controller has no address of
# its own, and BlueZ ignores a controller without a valid public address.
J4_BT_ADDRESS=""
# Start dbus and bluetoothd (BlueZ) after the controller is up. Needed to pair
# keyboards. Set to "no" if you only want the HCI device.
J4_BT_START_BLUETOOTHD="yes"
"""


class SetupError(Exception):
    pass


def run(*args, timeout=120):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SetupError(str(exc))
    except subprocess.TimeoutExpired:
        raise SetupError('%s timed out' % ' '.join(args))
    if proc.returncode != 0:
        raise SetupError('%s failed: %s' % (' '.join(args), proc.stderr.strip()))
    return proc.stdout


def check_device():
    if not DEVICEINFO.is_file():
        raise SetupError('%s missing; this is not the expected j4primelte image'
                         % DEVICEINFO)
    if 'samsung-j4primelte' not in DEVICEINFO.read_text():
        raise SetupError('Not the expected j4primelte device')


def status_lines():
    state = STATE_FILE.read_text().strip() if STATE_FILE.is_file() else 'absent'
    meaning = {'0': 'idle', '1': 'waiting for the WCNSS channels', '2': 'up'}.get(state, '?')
    lines = ['hci_smd driver: %s (%s)' % (state, meaning)]
    hci = Path('/sys/class/bluetooth/hci0')
    if hci.is_dir():
        address = (hci / 'address').read_text().strip() if (hci / 'address').is_file() else '?'
        lines.append('hci0: present, address %s' % address)
    else:
        lines.append('hci0: absent')
    return lines


def check():
    check_device()
    for line in status_lines():
        print(line)
    print('j4-bt: ' + ('present' if HELPER.is_file() else 'missing (run --install)'))
    if not PARAM_DIR.is_dir():
        print('NOTE: /sys/module/hci_smd is absent. This kernel does not carry the '
              'WCNSS Bluetooth HCI driver yet, so there is no hci device.')


def write_file(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


BLUEZ_PACKAGES = ['bluez', 'bluez-openrc', 'bluez-btmgmt', 'bluez-deprecated']


def ensure_bluez():
    """Install BlueZ on the phone if it is missing. apk writes to the rootfs,
    so the packages survive a reboot; a failure here is only a warning."""
    if shutil.which('bluetoothd'):
        return
    try:
        run('apk', 'add', *BLUEZ_PACKAGES, timeout=600)
        print('BLUEZ_OK: ' + ' '.join(BLUEZ_PACKAGES))
    except SetupError as exc:
        print('BLUEZ_MISSING: ' + str(exc))
        print('Install it manually: apk add ' + ' '.join(BLUEZ_PACKAGES))


def install():
    check_device()
    ensure_bluez()
    write_file(HELPER, SCRIPT_TEXT, 0o755)
    write_file(INIT, INIT_TEXT, 0o755)
    write_file(CONF, CONF_TEXT, 0o644)
    if not RUNLEVEL_LINK.is_symlink() and not RUNLEVEL_LINK.exists():
        RUNLEVEL_LINK.parent.mkdir(parents=True, exist_ok=True)
        run('rc-update', 'add', 'j4-bluetooth', 'boot')
    os.sync()
    print('INSTALL_OK: service j4-bluetooth installed in the boot runlevel '
          '(persists across reboots).')
    try:
        run(str(HELPER), '--boot', timeout=180)
        print('BRINGUP_OK: Bluetooth controller is up; state below.')
    except SetupError as exc:
        print('BRINGUP_PENDING: ' + str(exc))
        print('The service retries at every boot; see /var/log/j4/bluetooth.log.')
    for line in status_lines():
        print(line)
    print('Pair a keyboard with: j4-bt --scan   then   j4-bt --pair <MAC>')


def main():
    parser = argparse.ArgumentParser(description='J4+ Bluetooth bring-up helper')
    parser.add_argument('--check', action='store_true',
                        help='verify the device and report the current BT state')
    parser.add_argument('--install', action='store_true',
                        help='install the persistent j4-bluetooth service and bring BT up')
    args = parser.parse_args()
    if args.check == args.install:
        parser.error('pick exactly one of --check / --install')
    try:
        if args.check:
            check()
        else:
            install()
    except SetupError as exc:
        print('ERROR: ' + str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()

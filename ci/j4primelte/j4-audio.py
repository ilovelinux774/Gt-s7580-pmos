#!/usr/bin/python3
# J4_AUDIO_HELPER_V1
"""Opt-in live-device audio bring-up for our SM-J415F pmOS image.

Run --check, then --install ON THE PHONE. The installer writes the persistent
OpenRC service (j4-audio, boot runlevel) that boots the stock ADSP through the
adsp-loader sysfs (/sys/kernel/boot_adsp/boot) and retries the deferred ASoC
platform probes (/sys/bus/platform/drivers_probe) until the msm8952 sound card
registers. --install also brings audio up immediately.

No flashing, no stock partition mounts, no firmware copy or redistribution
(real adsp.mdt + adsp.b* files must already be copied into /lib/firmware from
this phone's own APNHLOS partition), no network/USB restarts, no mixer policy
changes. Logs only under /var/log/j4. Safe to re-run after upgrades.
"""
import argparse
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys

MARKER = 'J4_AUDIO_V1'
HELPER = Path('/usr/local/sbin/j4-audio')
INIT = Path('/etc/init.d/j4-audio')
CONF = Path('/etc/conf.d/j4-audio')
RUNLEVEL_LINK = Path('/etc/runlevels/boot/j4-audio')
BOOT_ADSP = Path('/sys/kernel/boot_adsp/boot')
SOUND_CARDS = Path('/proc/asound/cards')
ION_LINK = Path('/sys/bus/platform/devices/soc:qcom,msm-audio-ion/driver')
VOICE_LINK = Path('/sys/bus/platform/devices/soc:qcom,msm-pcm-voice/driver')
VOIP_LINK = Path('/sys/bus/platform/devices/soc:qcom,msm-voip-dsp/driver')
CARD_LINK = Path('/sys/bus/platform/devices/c051000.sound/driver')

SCRIPT_TEXT = """#!/bin/sh
# J4_AUDIO_V1 - Boot the ADSP and register the J4+ msm8952 ASoC sound card.
#
# Why this exists: the sound card's voice/voip platform devices defer forever
# (voc_alloc_cal_shared_memory -> msm_audio_ion_alloc returns -EPROBE_DEFER)
# while the audio ION device is unprobed, and msm-audio-ion itself defers until
# the ADSP is loaded (apr q6 state != DOWN). Nothing loads the ADSP on pmOS, so
# the card probe fails with -517 and no ALSA device appears. This script writes
# 1 to the stock adsp-loader sysfs (subsystem_get("adsp") -> q6v5 PIL loads
# adsp.mdt from /lib/firmware), then retries the deferred platform probes via
# /sys/bus/platform/drivers_probe until the card registers.
#
# Firmware policy: no firmware is copied, staged or redistributed here. Real
# adsp.mdt + adsp.b* files must already be in /lib/firmware (copied from this
# phone's own APNHLOS partition). Logs only under /var/log/j4; safe to re-run.

LOG=/var/log/j4/audio.log
TIMEOUT=${J4_AUDIO_TIMEOUT:-45}
BOOT_ADSP=/sys/kernel/boot_adsp/boot
ION_DEV=soc:qcom,msm-audio-ion
VOICE_DEV=soc:qcom,msm-pcm-voice
VOIP_DEV=soc:qcom,msm-voip-dsp
CARD_DEV=c051000.sound
JACK_DEV=soc:earjack

log() {
    mkdir -p /var/log/j4
    echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >> "$LOG"
}

firmware_ok() {
    [ -s /lib/firmware/adsp.mdt ] || return 1
    [ ! -L /lib/firmware/adsp.mdt ] || return 1
    for f in /lib/firmware/adsp.b*; do
        if [ -s "$f" ] && [ ! -L "$f" ]; then
            return 0
        fi
    done
    return 1
}

bound() {
    [ -e "/sys/bus/platform/devices/$1/driver" ]
}

reprobe() {
    for d in "$ION_DEV" "$VOICE_DEV" "$VOIP_DEV" "$CARD_DEV" "$JACK_DEV"; do
        echo "$d" > /sys/bus/platform/drivers_probe 2>/dev/null
    done
}

do_check() {
    echo "== J4 audio check (J4_AUDIO_V1) =="
    if [ -w "$BOOT_ADSP" ]; then
        echo "adsp-loader sysfs: OK ($BOOT_ADSP)"
    else
        echo "adsp-loader sysfs: MISSING ($BOOT_ADSP)"
    fi
    if firmware_ok; then
        echo "adsp firmware in /lib/firmware: OK (adsp.mdt + segments)"
    else
        echo "adsp firmware in /lib/firmware: MISSING (need real adsp.mdt + adsp.b*)"
    fi
    for d in "$ION_DEV" "$VOICE_DEV" "$VOIP_DEV" "$CARD_DEV" "$JACK_DEV"; do
        if bound "$d"; then
            echo "bound: $d"
        else
            echo "unbound: $d"
        fi
    done
    echo "-- /proc/asound/cards --"
    cat /proc/asound/cards 2>/dev/null
}

do_boot() {
    log "start: ADSP boot + sound card registration"
    if ! firmware_ok; then
        log "FAIL: adsp firmware missing in /lib/firmware"
        echo "j4-audio: adsp firmware missing in /lib/firmware (copy adsp.mdt + adsp.b* from APNHLOS first)" >&2
        return 1
    fi
    if [ ! -w "$BOOT_ADSP" ]; then
        log "FAIL: $BOOT_ADSP missing"
        echo "j4-audio: $BOOT_ADSP missing (adsp-loader did not probe)" >&2
        return 1
    fi
    if [ ! -d /proc/asound/card0 ]; then
        # subsystem_get("adsp") loads/authenticates the ADSP image via the
        # q6v5 PIL and the loader then marks the APR q6 state LOADED.
        echo 1 > "$BOOT_ADSP" 2>> "$LOG"
        log "adsp boot requested"
        n=$TIMEOUT
        while [ "$n" -gt 0 ]; do
            reprobe
            if [ -d /proc/asound/card0 ]; then
                break
            fi
            sleep 1
            n=$((n - 1))
        done
    fi
    reprobe
    if [ -d /proc/asound/card0 ]; then
        log "OK: sound card registered"
        cat /proc/asound/cards >> "$LOG" 2>/dev/null
        echo "j4-audio: sound card is up"
        return 0
    fi
    log "FAIL: no sound card after ${TIMEOUT}s"
    dmesg 2>/dev/null | tail -n 25 >> "$LOG"
    echo "j4-audio: no sound card after ${TIMEOUT}s (see $LOG)" >&2
    return 1
}

case "$1" in
    --check)
        do_check
        ;;
    --boot)
        do_boot
        ;;
    *)
        echo "usage: j4-audio --check | --boot" >&2
        exit 2
        ;;
esac
"""
INIT_TEXT = """#!/sbin/openrc-run
# J4_AUDIO_V1 - J4+ ADSP boot and msm8952 sound card registration.
name="j4-audio"
description="Boot ADSP and register the J4+ sound card (speaker/headphone/mic)"

depend() {
    need localmount
    after j4-usb-debug
}

start() {
    ebegin "Bringing up J4+ audio"
    /usr/local/sbin/j4-audio --boot
    eend $?
}

stop() {
    # Bring-up only; leave the card and ADSP running.
    return 0
}
"""
CONF_TEXT = """# J4_AUDIO_V1 - J4+ audio bring-up tuning.
# Seconds to wait for the ADSP load and sound card registration at boot.
J4_AUDIO_TIMEOUT="45"
"""


class SetupError(Exception):
    pass


def run(*args, timeout=60):
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
    if not re.search(r'^ID=[\\"]?postmarketos[\\"]?$', osinfo, re.M):
        raise SetupError('Not the expected postmarketOS installation.')
    if not os.uname().release.startswith('3.18.140-pmos-j4'):
        raise SetupError('Kernel differs from the tested j4 debug kernel.')


def check_adsp_loader():
    if not BOOT_ADSP.is_file():
        raise SetupError(f'adsp-loader sysfs missing: {BOOT_ADSP}')
    if not os.access(str(BOOT_ADSP), os.W_OK):
        raise SetupError(f'adsp-loader sysfs not writable: {BOOT_ADSP}')


def check_firmware(base=Path('/lib/firmware')):
    bad = []
    mdt = base / 'adsp.mdt'
    if mdt.is_symlink() or not mdt.is_file() or mdt.stat().st_size == 0:
        bad.append('adsp.mdt')
    segs = [f for f in base.glob('adsp.b*')
            if f.is_file() and not f.is_symlink() and f.stat().st_size > 0]
    if not segs:
        bad.append('adsp.b* segments')
    if bad:
        raise SetupError('Missing/empty/symlinked firmware: ' + ', '.join(bad)
                         + '. Copy real adsp files from this phone\'s APNHLOS partition '
                           'first; this helper never stages firmware.')


def status_lines():
    def yesno(path):
        return 'bound' if path.exists() else 'unbound'
    lines = [
        f'adsp-loader sysfs: {"present" if BOOT_ADSP.is_file() else "MISSING"}',
        f'audio-ion: {yesno(ION_LINK)}',
        f'pcm-voice: {yesno(VOICE_LINK)}',
        f'voip-dsp: {yesno(VOIP_LINK)}',
        f'sound card device: {yesno(CARD_LINK)}',
        '-- ' + str(SOUND_CARDS) + ' --',
    ]
    try:
        lines.append(SOUND_CARDS.read_text().rstrip('\n'))
    except OSError:
        lines.append('(unreadable)')
    return lines


def check():
    check_device()
    check_adsp_loader()
    check_firmware()
    print('CHECK_OK: correct device; adsp firmware present; adsp-loader ready.')
    for line in status_lines():
        print(line)


def write_file(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def install():
    check_device()
    check_adsp_loader()
    check_firmware()
    write_file(HELPER, SCRIPT_TEXT, 0o755)
    write_file(INIT, INIT_TEXT, 0o755)
    write_file(CONF, CONF_TEXT, 0o644)
    if not RUNLEVEL_LINK.is_symlink() and not RUNLEVEL_LINK.exists():
        RUNLEVEL_LINK.parent.mkdir(parents=True, exist_ok=True)
        run('rc-update', 'add', 'j4-audio', 'boot')
    os.sync()
    print('INSTALL_OK: service j4-audio installed in the boot runlevel '
          '(persists across reboots).')
    try:
        run(str(HELPER), '--boot', timeout=180)
        print('BRINGUP_OK: sound card registration requested; state below.')
    except SetupError as exc:
        print('BRINGUP_PENDING: ' + str(exc))
        print('The service retries at every boot; see /var/log/j4/audio.log.')
    for line in status_lines():
        print(line)


def main():
    parser = argparse.ArgumentParser(description='J4+ audio bring-up helper')
    parser.add_argument('--check', action='store_true',
                        help='verify device, firmware and current audio state')
    parser.add_argument('--install', action='store_true',
                        help='install the persistent j4-audio service and bring audio up')
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

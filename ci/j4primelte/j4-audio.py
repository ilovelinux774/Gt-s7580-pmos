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
this phone's own APNHLOS partition), no network/USB restarts. After the card registers the stock mixer paths
from mixer_paths_wcd9306.xml are applied via the j4-mixer helper (raw ALSA
ioctls, skips broken controls). Logs only under /var/log/j4. Safe to re-run.
"""
import argparse
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys

MARKER = 'J4_AUDIO_V2'
HELPER = Path('/usr/local/sbin/j4-audio')
MIXER = Path('/usr/local/sbin/j4-mixer')
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
# J4_AUDIO_V2 - Boot the ADSP and register the J4+ msm8952 ASoC sound card.
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

apply_mixers() {
    if [ -x /usr/local/sbin/j4-mixer ]; then
        /usr/local/sbin/j4-mixer reset >> "$LOG" 2>&1
        out=${J4_AUDIO_OUTPUT:-speaker}
        if /usr/local/sbin/j4-mixer "$out" >> "$LOG" 2>&1; then
            log "mixers applied: $out"
        else
            log "WARN: mixer path $out incomplete (run j4-mixer probe)"
        fi
    else
        log "j4-mixer missing, skipping mixer setup"
    fi
}

do_check() {
    echo "== J4 audio check (J4_AUDIO_V2) =="
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

snapshot_dmesg() {
    # Preserve the kernel ring buffer while it still holds the boot messages.
    # Once the battery/fuelgauge spam starts (a few minutes after boot) the
    # ASoC probe messages are gone forever, and those messages are the only
    # place snd_soc_dapm_add_routes() reports a missing DAPM route.
    mkdir -p /var/log/j4
    dmesg > "/var/log/j4/$1" 2>/dev/null
}

do_boot() {
    log "start: ADSP boot + sound card registration"
    snapshot_dmesg dmesg-boot.log
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
        snapshot_dmesg dmesg-audio.log
        apply_mixers
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
# J4_AUDIO_V2 - J4+ ADSP boot and msm8952 sound card registration.
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
CONF_TEXT = """# J4_AUDIO_V2 - J4+ audio bring-up tuning.
# Seconds to wait for the ADSP load and sound card registration at boot.
J4_AUDIO_TIMEOUT="45"
# Output mixer path applied after the card registers:
# speaker | headphones | handset
J4_AUDIO_OUTPUT="speaker"
"""

MIXER_TEXT = r"""#!/usr/bin/python3
# J4_MIXER_V2 - direct ALSA control mixer for the J4+ msm8952 sound card.
#
# Why not amixer/tinymix: alsa-lib snd_hctl_load() aborts when ANY control's
# ELEM_INFO returns an error, so one broken control ("Mixer hw:0 load error:
# No such device") makes the whole card unmixable through alsa-lib. This tool
# talks to /dev/snd/controlC0 with raw ioctls and skips broken controls while
# naming them (see: probe).
#
# Control paths are transcribed 1:1 from the stock Samsung
# vendor/etc/mixer_paths_wcd9306.xml (paths speaker, headphones, handset,
# adc1/mic, deep-buffer-playback, audio-record plus the initial mixer
# settings). The critical routing control is
# 'SLIMBUS_0_RX Audio Mixer MultiMedia1' (FE MultiMedia1 -> BE SLIMBUS_0_RX)
# without which the kernel refuses hw_params with EINVAL
# ('no backend DAIs enabled').
#
# No firmware, no partition mounts, no network. Needs python3 (apk add python3).

import argparse
import ctypes
import fcntl
import sys

CTL = '/dev/snd/controlC0'

TYPE_NONE, TYPE_BOOLEAN, TYPE_INTEGER = 0, 1, 2
TYPE_ENUMERATED, TYPE_BYTES, TYPE_IEC958, TYPE_INTEGER64 = 3, 4, 5, 6
TYPE_NAMES = {0: 'NONE', 1: 'BOOL', 2: 'INT', 3: 'ENUM', 4: 'BYTES',
              5: 'IEC958', 6: 'INT64'}


def _IOWR(type_, nr, size):
    return (3 << 30) | (size << 16) | (ord(type_) << 8) | nr


class ElemId(ctypes.Structure):
    _fields_ = [
        ('numid', ctypes.c_uint32),
        ('iface', ctypes.c_int32),
        ('device', ctypes.c_uint32),
        ('subdevice', ctypes.c_uint32),
        ('name', ctypes.c_char * 44),
        ('index', ctypes.c_uint32),
    ]


class ElemList(ctypes.Structure):
    _fields_ = [
        ('offset', ctypes.c_uint32),
        ('space', ctypes.c_uint32),
        ('used', ctypes.c_uint32),
        ('count', ctypes.c_uint32),
        ('pids', ctypes.c_void_p),
        ('reserved', ctypes.c_ubyte * 50),
    ]


class EnumInfo(ctypes.Structure):
    _fields_ = [
        ('items', ctypes.c_uint32),
        ('item', ctypes.c_uint32),
        ('name', ctypes.c_char * 64),
        ('names_ptr', ctypes.c_uint64),
        ('names_length', ctypes.c_uint32),
    ]


class InfoValue(ctypes.Union):
    class _Int(ctypes.Structure):
        _fields_ = [('min', ctypes.c_long), ('max', ctypes.c_long),
                    ('step', ctypes.c_long)]

    class _Int64(ctypes.Structure):
        _fields_ = [('min', ctypes.c_longlong), ('max', ctypes.c_longlong),
                    ('step', ctypes.c_longlong)]

    _fields_ = [
        ('integer', _Int),
        ('integer64', _Int64),
        ('enumerated', EnumInfo),
        ('reserved', ctypes.c_ubyte * 128),
    ]


class Dimen(ctypes.Union):
    _fields_ = [('d', ctypes.c_ushort * 4), ('d_ptr', ctypes.c_void_p)]


class ElemInfo(ctypes.Structure):
    _fields_ = [
        ('id', ElemId),
        ('type', ctypes.c_int),
        ('access', ctypes.c_uint),
        ('count', ctypes.c_uint),
        ('owner', ctypes.c_int),
        ('value', InfoValue),
        ('dimen', Dimen),
        ('reserved', ctypes.c_ubyte * (64 - 4 * ctypes.sizeof(ctypes.c_ushort))),
    ]


class ValUnion(ctypes.Union):
    _fields_ = [
        ('integer', ctypes.c_long * 128),
        ('integer64', ctypes.c_longlong * 64),
        ('enumerated', ctypes.c_uint32 * 128),
        ('bytes', ctypes.c_ubyte * 512),
    ]


class TimeSpec(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]


class ElemValue(ctypes.Structure):
    _fields_ = [
        ('id', ElemId),
        ('indirect', ctypes.c_uint),
        ('value', ValUnion),
        ('tstamp', TimeSpec),
        ('reserved', ctypes.c_ubyte * (128 - ctypes.sizeof(TimeSpec))),
    ]


ELEM_LIST = _IOWR('U', 0x10, ctypes.sizeof(ElemList))
ELEM_INFO = _IOWR('U', 0x11, ctypes.sizeof(ElemInfo))
ELEM_READ = _IOWR('U', 0x12, ctypes.sizeof(ElemValue))
ELEM_WRITE = _IOWR('U', 0x13, ctypes.sizeof(ElemValue))

# Initial mixer settings from mixer_paths_wcd9306.xml (voice/voip/TTY call
# controls deliberately omitted - telephony is out of scope).
INIT_SETTINGS = [
    ('LINEOUT1 Volume', '20'),
    ('LINEOUT2 Volume', '20'),
    ('HPHL Volume', '20'),
    ('HPHR Volume', '20'),
    ('RX1 Digital Volume', '84'),
    ('RX2 Digital Volume', '84'),
    ('RX3 Digital Volume', '84'),
    ('RX4 Digital Volume', '84'),
    ('ADC1 Volume', '12'),
    ('ADC2 Volume', '12'),
    ('ADC3 Volume', '12'),
    ('ADC4 Volume', '0'),
    ('ADC5 Volume', '0'),
    ('DEC1 Volume', '84'),
    ('DEC2 Volume', '84'),
    ('DEC3 Volume', '84'),
    ('DEC4 Volume', '84'),
    ('IIR1 INP1 Volume', '84'),
    ('IIR1 INP2 Volume', '84'),
    ('IIR1 INP3 Volume', '84'),
    ('IIR1 INP4 Volume', '84'),
    ('COMP0 Switch', '0'),
    ('COMP1 Switch', '0'),
    ('COMP2 Switch', '0'),
]

# deep-buffer-playback: FE MultiMedia1 -> BE SLIMBUS_0_RX
PLAYBACK_ROUTING = [
    ('SLIMBUS_0_RX Audio Mixer MultiMedia1', '1'),
]

# audio-record: FE MultiMedia1 <- BE SLIM_0_TX
RECORD_ROUTING = [
    ('MultiMedia1 Mixer SLIM_0_TX', '1'),
]

PATHS = {
    # mixer_paths_wcd9306.xml: deep-buffer-playback + speaker
    'speaker': PLAYBACK_ROUTING + [
        ('SLIM RX1 MUX', 'AIF1_PB'),
        ('SLIM_0_RX Channels', 'One'),
        ('RX4 MIX1 INP1', 'RX1'),
        ('SPK DAC Switch', '1'),
        ('COMP0 Switch', '1'),
    ],
    # deep-buffer-playback + headphones
    'headphones': PLAYBACK_ROUTING + [
        ('SLIM RX1 MUX', 'AIF1_PB'),
        ('SLIM RX2 MUX', 'AIF1_PB'),
        ('SLIM_0_RX Channels', 'Two'),
        ('RX1 MIX1 INP1', 'RX1'),
        ('RX2 MIX1 INP1', 'RX2'),
        ('CLASS_H_DSM MUX', 'RX_HPHL'),
        ('RDAC3 MUX', 'DEM2'),
        ('HPHL DAC Switch', '1'),
        ('COMP1 Switch', '1'),
    ],
    # deep-buffer-playback + handset (earpiece)
    'handset': PLAYBACK_ROUTING + [
        ('SLIM RX1 MUX', 'AIF1_PB'),
        ('SLIM_0_RX Channels', 'One'),
        ('RX1 MIX1 INP1', 'RX1'),
        ('CLASS_H_DSM MUX', 'RX_HPHL'),
        ('RDAC3 MUX', 'DEM2'),
        ('DAC1 Switch', '1'),
    ],
    # audio-record + handset-mic/speaker-mic (adc1)
    'mic': RECORD_ROUTING + [
        ('AIF1_CAP Mixer SLIM TX1', '1'),
        ('SLIM_0_TX Channels', 'One'),
        ('SLIM TX1 MUX', 'DEC1'),
        ('DEC1 MUX', 'ADC1'),
        ('IIR1 INP1 MUX', 'DEC1'),
    ],
}


class MixError(Exception):
    pass


def _name_of(eid):
    return eid.name.split(b'\0')[0].decode('ascii', 'replace')


def _ioctl(fd, req, buf):
    fcntl.ioctl(fd, req, memoryview(buf))


def open_ctl(card=0):
    return open(CTL.replace('C0', f'C{card}'), 'rb', buffering=0)


def list_controls(fd):
    lst = ElemList()
    _ioctl(fd, ELEM_LIST, lst)
    count = lst.count
    if not count:
        return []
    arr = (ElemId * count)()
    lst.offset = 0
    lst.space = count
    lst.pids = ctypes.addressof(arr)
    _ioctl(fd, ELEM_LIST, lst)
    return [arr[i] for i in range(lst.used)]


def ctl_info(fd, eid, item=None):
    info = ElemInfo()
    info.id = eid
    if item is not None:
        info.value.enumerated.item = item
    _ioctl(fd, ELEM_INFO, info)
    return info


def ctl_read(fd, eid):
    val = ElemValue()
    val.id = eid
    _ioctl(fd, ELEM_READ, val)
    return val


def ctl_write(fd, eid, first, rest_same=False):
    val = ElemValue()
    val.id = eid
    val.value.integer[0] = first
    if rest_same:
        for i in range(1, 128):
            val.value.integer[i] = first
    _ioctl(fd, ELEM_WRITE, val)


def enum_items(fd, eid, items):
    out = []
    for i in range(items):
        info = ctl_info(fd, eid, item=i)
        out.append(info.value.enumerated.name.split(b'\0')[0]
                   .decode('ascii', 'replace'))
    return out


def find_ctl(fd, name, index=0):
    for eid in list_controls(fd):
        if _name_of(eid) == name and eid.index == index:
            return eid
    return None


def describe(fd, eid):
    info = ctl_info(fd, eid)
    val = ctl_read(fd, eid)
    t = info.type
    if t == TYPE_ENUMERATED:
        items = enum_items(fd, eid, info.value.enumerated.items)
        cur = val.value.enumerated[0]
        text = items[cur] if cur < len(items) else f'?{cur}'
        return t, f'ENUM[{cur}] {text}'
    if t in (TYPE_BOOLEAN, TYPE_INTEGER, TYPE_INTEGER64):
        return t, str(val.value.integer[0])
    return t, '(raw)'


def set_ctl(fd, name, value, index=0):
    eid = find_ctl(fd, name, index)
    if eid is None:
        raise MixError(f'no such control: {name!r} (index {index})')
    info = ctl_info(fd, eid)
    t = info.type
    if t == TYPE_ENUMERATED:
        items = enum_items(fd, eid, info.value.enumerated.items)
        want = str(value)
        for i, item in enumerate(items):
            if item == want:
                ctl_write(fd, eid, i)
                return f'OK {name} = {want}'
        low = want.lower()
        for i, item in enumerate(items):
            if item.lower() == low:
                ctl_write(fd, eid, i)
                return f'OK {name} = {item}'
        raise MixError(f'{name}: no enum item {want!r} (have: {", ".join(items)})')
    if t in (TYPE_BOOLEAN, TYPE_INTEGER, TYPE_INTEGER64):
        n = int(str(value), 0)
        lo, hi = info.value.integer.min, info.value.integer.max
        if t != TYPE_BOOLEAN and not (lo <= n <= hi):
            raise MixError(f'{name}: {n} out of range [{lo}..{hi}]')
        ctl_write(fd, eid, n)
        return f'OK {name} = {n}'
    raise MixError(f'{name}: unsupported control type {TYPE_NAMES.get(t, t)}')


def apply_list(fd, entries, label):
    ok = failed = 0
    for name, value in entries:
        try:
            line = set_ctl(fd, name, value)
            print(line)
            ok += 1
        except (MixError, OSError) as exc:
            print(f'SKIP {name}: {exc}')
            failed += 1
    print(f'{label}: {ok} set, {failed} skipped')
    return failed


def cmd_probe(fd):
    broken = 0
    for eid in list_controls(fd):
        name = _name_of(eid)
        try:
            t, text = describe(fd, eid)
            print(f'{eid.numid:4d}  {TYPE_NAMES.get(t, t):5s}  '
                  f'{name}[{eid.index}] = {text}')
        except OSError as exc:
            broken += 1
            print(f'{eid.numid:4d}  BROKEN  {name}[{eid.index}]: '
                  f'{exc.__class__.__name__} errno={exc.errno}')
    print(f'-- {broken} broken control(s) skipped --')
    return 0


def cmd_show(fd):
    names = []
    for entries in list(INIT_SETTINGS) + [p for path in PATHS.values()
                                          for p in path]:
        if entries[0] not in names:
            names.append(entries[0])
    for name in names:
        eid = find_ctl(fd, name)
        if eid is None:
            print(f'-- {name}: MISSING')
            continue
        try:
            _, text = describe(fd, eid)
            print(f'-- {name} = {text}')
        except OSError as exc:
            print(f'-- {name}: BROKEN ({exc.errno})')
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='J4+ msm8952 ALSA control mixer (raw ioctl)')
    parser.add_argument('--card', type=int, default=0,
                        help='ALSA card index (default 0)')
    parser.add_argument('command',
                        choices=['probe', 'show', 'reset', 'set', 'get']
                        + sorted(PATHS),
                        help='probe=enumerate controls, show=current path '
                             'values, reset=stock volumes, set/get=single '
                             'control, speaker/headphones/handset/mic=apply '
                             'a stock mixer path')
    parser.add_argument('name', nargs='?', help='control name for set/get')
    parser.add_argument('value', nargs='?', help='value for set')
    args = parser.parse_args()

    try:
        fd = open_ctl(args.card)
    except OSError as exc:
        print(f'j4-mixer: cannot open {CTL}: {exc}', file=sys.stderr)
        return 1

    with fd:
        try:
            if args.command == 'probe':
                return cmd_probe(fd)
            if args.command == 'show':
                return cmd_show(fd)
            if args.command == 'reset':
                return 1 if apply_list(fd, INIT_SETTINGS, 'reset') else 0
            if args.command in PATHS:
                return 1 if apply_list(fd, PATHS[args.command],
                                       args.command) else 0
            if args.command == 'set':
                if not args.name or args.value is None:
                    print('usage: j4-mixer set "NAME" VALUE', file=sys.stderr)
                    return 2
                print(set_ctl(fd, args.name, args.value))
                return 0
            if args.command == 'get':
                if not args.name:
                    print('usage: j4-mixer get "NAME"', file=sys.stderr)
                    return 2
                eid = find_ctl(fd, args.name)
                if eid is None:
                    print(f'no such control: {args.name!r}')
                    return 1
                _, text = describe(fd, eid)
                print(text)
                return 0
        except (MixError, OSError) as exc:
            print(f'j4-mixer: {exc}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
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
    print('j4-mixer: ' + ('present' if MIXER.is_file() else 'missing (run --install)'))
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
    write_file(MIXER, MIXER_TEXT, 0o755)
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

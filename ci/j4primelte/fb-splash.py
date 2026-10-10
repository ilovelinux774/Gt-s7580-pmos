#!/usr/bin/python3
# J4_FB_SPLASH_V1
"""Persistent MDSS framebuffer owner + bring-up splash for SM-J415F pmOS.

The kernel hands the aboot splash over via continuous splash, has no framebuffer
console, and powers the panel down when the last /dev/fb0 handle closes. This
daemon therefore opens fb0 once and keeps it open, draws a channel-order-safe
white-on-dark splash, sets the real lcd-backlight LED level, and commits frames
with FBIOPAN_DISPLAY. Run --test on the phone first. --install copies this exact
file to /usr/local/sbin, adds a supervise-daemon OpenRC service and starts it.

No partition, firmware, network, SSH or password changes. Stop/disable this
service before installing a desktop compositor later (it owns fb0 deliberately).
"""
import argparse
import datetime
import fcntl
import mmap
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

MARKER = 'J4_FB_SPLASH_V1'
HELPER = Path('/usr/local/sbin/j4-fb-splash')
INIT = Path('/etc/init.d/j4-fb-splash')
RUNLEVEL_LINK = Path('/etc/runlevels/boot/j4-fb-splash')
BACKLIGHT = Path('/sys/class/leds/lcd-backlight')
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOPAN_DISPLAY = 0x4606
FBIOBLANK = 0x4611

# 5x7 uppercase ASCII subset. White-on-dark text is immune to RGBA/BGRA order.
GLYPHS = {
    ' ': ('.....', '.....', '.....', '.....', '.....', '.....', '.....'),
    'A': ('.###.', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'),
    'B': ('####.', '#...#', '#...#', '####.', '#...#', '#...#', '####.'),
    'C': ('.###.', '#...#', '#....', '#....', '#....', '#...#', '.###.'),
    'D': ('####.', '#...#', '#...#', '#...#', '#...#', '#...#', '####.'),
    'E': ('#####', '#....', '#....', '####.', '#....', '#....', '#####'),
    'F': ('#####', '#....', '#....', '####.', '#....', '#....', '#....'),
    'G': ('.###.', '#...#', '#....', '#.###', '#...#', '#...#', '.###.'),
    'H': ('#...#', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'),
    'I': ('#####', '..#..', '..#..', '..#..', '..#..', '..#..', '#####'),
    'J': ('..###', '...#.', '...#.', '...#.', '...#.', '#..#.', '.##..'),
    'K': ('#...#', '#..#.', '#.#..', '##...', '#.#..', '#..#.', '#...#'),
    'L': ('#....', '#....', '#....', '#....', '#....', '#....', '#####'),
    'M': ('#...#', '##.##', '#.#.#', '#.#.#', '#...#', '#...#', '#...#'),
    'N': ('#...#', '##..#', '#.#.#', '#..##', '#...#', '#...#', '#...#'),
    'O': ('.###.', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'),
    'P': ('####.', '#...#', '#...#', '####.', '#....', '#....', '#....'),
    'Q': ('.###.', '#...#', '#...#', '#...#', '#.#.#', '#..#.', '.##.#'),
    'R': ('####.', '#...#', '#...#', '####.', '#.#..', '#..#.', '#...#'),
    'S': ('.####', '#....', '#....', '.###.', '....#', '....#', '####.'),
    'T': ('#####', '..#..', '..#..', '..#..', '..#..', '..#..', '..#..'),
    'U': ('#...#', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'),
    'V': ('#...#', '#...#', '#...#', '#...#', '#...#', '.#.#.', '..#..'),
    'W': ('#...#', '#...#', '#...#', '#.#.#', '#.#.#', '##.##', '#...#'),
    'X': ('#...#', '#...#', '.#.#.', '..#..', '.#.#.', '#...#', '#...#'),
    'Y': ('#...#', '#...#', '.#.#.', '..#..', '..#..', '..#..', '..#..'),
    'Z': ('#####', '....#', '...#.', '..#..', '.#...', '#....', '#####'),
    '0': ('.###.', '#...#', '#..##', '#.#.#', '##..#', '#...#', '.###.'),
    '1': ('..#..', '.##..', '..#..', '..#..', '..#..', '..#..', '#####'),
    '2': ('.###.', '#...#', '....#', '...#.', '..#..', '.#...', '#####'),
    '3': ('.###.', '#...#', '....#', '..##.', '....#', '#...#', '.###.'),
    '4': ('...#.', '..##.', '.#.#.', '#..#.', '#####', '...#.', '...#.'),
    '5': ('#####', '#....', '####.', '....#', '....#', '#...#', '.###.'),
    '6': ('.###.', '#....', '#....', '####.', '#...#', '#...#', '.###.'),
    '7': ('#####', '....#', '...#.', '..#..', '.#...', '.#...', '.#...'),
    '8': ('.###.', '#...#', '#...#', '.###.', '#...#', '#...#', '.###.'),
    '9': ('.###.', '#...#', '#...#', '.####', '....#', '....#', '.###.'),
    '-': ('.....', '.....', '.....', '.###.', '.....', '.....', '.....'),
    '.': ('.....', '.....', '.....', '.....', '.....', '.##..', '.##..'),
    ':': ('.....', '.##..', '.##..', '.....', '.##..', '.##..', '.....'),
    '/': ('....#', '...#.', '...#.', '..#..', '.#...', '.#...', '#....'),
    '(': ('...#.', '..#..', '.#...', '.#...', '.#...', '..#..', '...#.'),
    ')': ('.#...', '..#..', '...#.', '...#.', '...#.', '..#..', '.#...'),
    '?': ('.###.', '#...#', '....#', '...#.', '..#..', '.....', '..#..'),
}

BG = (10, 14, 28, 255)
BAND = (0, 102, 204, 255)
FG = (255, 255, 255, 255)
DIM = (160, 185, 210, 255)
STATUS_Y = 470

INIT_TEXT = '''#!/sbin/openrc-run
# J4_FB_SPLASH_V1
name="J4+ framebuffer splash"
description="Own the MDSS framebuffer and show the pmOS bring-up splash"
supervisor="supervise-daemon"
command="/usr/local/sbin/j4-fb-splash"
command_args="--run"
respawn_delay=5
respawn_max=0

depend() {
    need localmount
    after devfs
}

stop() {
    # Closing fb0 powers the panel down on this driver; a stop is expected to
    # leave the screen dark until the service (or another consumer) starts.
    return 0
}
'''


class SplashError(Exception):
    pass


def geometry(fix, var):
    """Validate the exact ARM32 MDSS framebuffer ABI used on SM-J415F."""
    if len(fix) != 68 or len(var) != 160:
        raise SplashError('Unexpected framebuffer info structure size')
    ident = bytes(fix[:16]).split(b'\0', 1)[0].decode('ascii', 'replace')
    length, fb_type, _, visual = struct.unpack_from('<4I', fix, 20)
    stride = struct.unpack_from('<I', fix, 44)[0]
    w, h, vw, vh, x, y, bpp, gray = struct.unpack_from('<8I', var)
    channels = [struct.unpack_from('<3I', var, off) for off in (32, 44, 56, 68)]
    if not ident.startswith('mdssfb_') or (fb_type, visual) != (0, 2):
        raise SplashError('Not the expected packed true-colour MDSS framebuffer')
    if bpp != 32 or gray != 0 or channels != [(0, 8, 0), (8, 8, 0), (16, 8, 0), (24, 8, 0)]:
        raise SplashError('Pixel format differs from the reported RGBA8888 layout')
    if not (3 <= w <= 4096 and 1 <= h <= 4096 and w <= vw <= 4096 and h <= vh <= 8192):
        raise SplashError('Unexpected framebuffer dimensions')
    if x + w > vw or y + h > vh or (x + w) * 4 > stride:
        raise SplashError('Visible rectangle exceeds virtual dimensions or stride')
    if not (0 < length <= 64 * 1024 * 1024) or (y + h - 1) * stride + (x + w) * 4 > length:
        raise SplashError('Visible rectangle exceeds framebuffer memory')
    return dict(width=w, height=h, xoffset=x, yoffset=y, stride=stride,
                memory_bytes=length)


def fill_rect(buffer, g, x, y, w, h, color):
    """Write clipped RGBA pixels; never touch row padding or second buffer."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(g['width'], x + w), min(g['height'], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    row = bytes(color) * (x1 - x0)
    for yy in range(y0, y1):
        start = (g['yoffset'] + yy) * g['stride'] + (g['xoffset'] + x0) * 4
        buffer[start:start + len(row)] = row


def text_width(text, scale):
    return max(0, len(text) * 6 * scale - scale)


def draw_text(buffer, g, x, y, text, scale, color):
    cursor = x
    for char in text.upper():
        glyph = GLYPHS.get(char, GLYPHS['?'])
        for row, line in enumerate(glyph):
            for col, pixel in enumerate(line):
                if pixel == '#':
                    fill_rect(buffer, g, cursor + col * scale, y + row * scale,
                              scale, scale, color)
        cursor += 6 * scale
    return cursor - x


def draw_splash(buffer, g, ip_text):
    fill_rect(buffer, g, 0, 0, g['width'], g['height'], BG)
    fill_rect(buffer, g, 0, 0, g['width'], 24, BAND)
    fill_rect(buffer, g, 0, g['height'] - 24, g['width'], 24, BAND)

    def centred(text, y, scale, color):
        draw_text(buffer, g, (g['width'] - text_width(text, scale)) // 2, y,
                  text, scale, color)

    centred('POSTMARKETOS', 130, 6, FG)
    centred('SAMSUNG SM-J415F - J4PLUS DEBUG', 250, 3, FG)
    centred(ip_text, 330, 3, DIM)
    centred('FRAMEBUFFER ACTIVE', 400, 2, DIM)


def draw_status(buffer, g, text):
    scale = 3
    y = STATUS_Y
    fill_rect(buffer, g, 0, y - 4, g['width'], 7 * scale + 8, BG)
    draw_text(buffer, g, (g['width'] - text_width(text, scale)) // 2, y,
              text, scale, FG)


def read_brightness():
    try:
        maximum = int((BACKLIGHT / 'max_brightness').read_text().strip())
        old = int((BACKLIGHT / 'brightness').read_text().strip())
    except (OSError, ValueError) as exc:
        raise SplashError(f'lcd-backlight LED is not usable: {exc}')
    if not (0 <= old <= maximum) or maximum < 1:
        raise SplashError('Unusable lcd-backlight range.')
    level = max(1, min(100, maximum // 3))
    return maximum, old, level


def run(seconds=None):
    if struct.calcsize('P') != 4 or sys.byteorder != 'little' or not os.uname().machine.startswith('arm'):
        raise SplashError('Run this on the 32-bit ARM phone, not on the PC.')
    maximum, old_brightness, level = read_brightness()
    ip_text = 'USB 172.16.42.1'
    try:
        import socket
        # Cosmetic only: show the phone hostname; static IP text stays either way.
        ip_text = f'HOST {socket.gethostname().upper()}'[:18]
    except OSError:
        pass
    fd = os.open('/dev/fb0', os.O_RDWR | os.O_CLOEXEC)
    buffer = None
    stopped = {'flag': False}
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__('flag', True))
    signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__('flag', True))
    started = time.monotonic()
    try:
        fix, var = bytearray(68), bytearray(160)
        fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fix, True)
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var, True)
        g = geometry(fix, var)
        print(f'Framebuffer: {g}; brightness range 0..{maximum}, '
              f'old {old_brightness}, splash {level}', flush=True)
        fcntl.ioctl(fd, FBIOBLANK, 0)
        buffer = mmap.mmap(fd, g['memory_bytes'], flags=mmap.MAP_SHARED,
                           prot=mmap.PROT_READ | mmap.PROT_WRITE)
        (BACKLIGHT / 'brightness').write_text(str(level) + '\n')
        draw_splash(buffer, g, ip_text)
        draw_status(buffer, g, 'BOOT OK - FB OWNED BY PMOS')
        fcntl.ioctl(fd, FBIOPAN_DISPLAY, var, True)
        print('Splash committed. Keeping /dev/fb0 open; Ctrl+C/stop restores state.', flush=True)
        last_beat = time.monotonic()
        while not stopped['flag']:
            time.sleep(0.25)
            if time.monotonic() - last_beat >= 5:
                uptime = int(time.monotonic() - started)
                draw_status(buffer, g, f'UP {uptime // 3600:02d}:{uptime % 3600 // 60:02d}:{uptime % 60:02d}')
                fcntl.ioctl(fd, FBIOPAN_DISPLAY, var, True)
                last_beat = time.monotonic()
            if seconds is not None and time.monotonic() - started >= seconds:
                break
        print('Exiting. This driver turns the panel off at the last fb0 close.', flush=True)
    finally:
        if buffer is not None:
            buffer.close()
        os.close(fd)
        try:
            (BACKLIGHT / 'brightness').write_text(str(old_brightness) + '\n')
        except OSError:
            pass


def install():
    if os.geteuid() != 0:
        raise SplashError('Run --install as root on the phone.')
    if struct.calcsize('P') != 4 or not os.uname().machine.startswith('arm'):
        raise SplashError('Only the 32-bit ARM phone image is supported.')
    info = Path('/usr/share/deviceinfo/deviceinfo')
    if not info.is_file() or 'samsung-j4primelte' not in info.read_text():
        raise SplashError('Installed deviceinfo is not samsung-j4primelte.')
    read_brightness()  # fail before any writes if the LED control is unusable
    for path in (HELPER, INIT):
        if path.is_symlink():
            raise SplashError(f'Refusing to replace a symlink: {path}')
        if path.exists() and MARKER not in path.read_text(errors='replace')[:512]:
            raise SplashError(f'Existing file is not owned by this helper: {path}')
    if RUNLEVEL_LINK.exists() or RUNLEVEL_LINK.is_symlink():
        if not RUNLEVEL_LINK.is_symlink() or RUNLEVEL_LINK.resolve() != INIT:
            raise SplashError('Unexpected j4-fb-splash runlevel entry already exists.')
    if not shutil.which('rc-update') or not shutil.which('rc-service'):
        raise SplashError('OpenRC rc-update/rc-service not found.')
    source = Path(__file__).read_bytes()
    for path, data, mode in ((HELPER, source, 0o755), (INIT, INIT_TEXT.encode(), 0o755)):
        fd, temporary = tempfile.mkstemp(prefix='.j4-fb-', dir=path.parent)
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
    subprocess.run(['rc-update', 'add', 'j4-fb-splash', 'boot'], check=True)
    if not RUNLEVEL_LINK.is_symlink() or RUNLEVEL_LINK.resolve() != INIT:
        raise SplashError('Runlevel link verification failed.')
    subprocess.run(['rc-service', 'j4-fb-splash', 'restart'], check=True)
    print('INSTALL_OK: framebuffer splash installed, enabled in boot, started now.')
    print('The aboot image should be replaced within a few seconds.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--test', type=int, metavar='SECONDS')
    group.add_argument('--run', action='store_true', help=argparse.SUPPRESS)
    group.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        if args.install:
            install()
        else:
            run(seconds=args.test)
        return 0
    except (SplashError, OSError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

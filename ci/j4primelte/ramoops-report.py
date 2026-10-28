#!/usr/bin/env python3
"""Read a pstore console-ramoops capture and say what the boot did.

The board dies without an oops, so the only report available is where the log
stops. This prints that: the last lines, the command line the dead boot used
(which variant it was), the timestamps of the markers the fbcon patch emits,
and whether the console ever took over the panel.

pstore survives a warm reset, so recovery can read the *previous* boot. Pull it
BEFORE reflashing:

    sudo adb shell ls -l /sys/fs/pstore/
    sudo adb pull /sys/fs/pstore/console-ramoops ./console-ramoops.txt
    python3 ci/j4primelte/ramoops-report.py ./console-ramoops.txt

dmesg-ramoops-0 next to it is a binary record (no bracketed timestamps); read
the console- one.
"""
import re
import sys
from pathlib import Path

TS = re.compile(r'^\[\s*(\d+\.\d+)\]\s*(.*)$')

# (label, needle, what it means when present, what it means when absent)
CHECKS = [
    ('late bind marker', 'skipping it and binding at late init',
     'fbcon_startup() ran, fb_open() failed and was skipped: the patch is live',
     'fbcon_startup() never reached that point -- death is inside or before it'),
    ('console takeover', 'Console: switching',
     'fbcon bound the panel (acceptance criterion)',
     'no takeover happened in this boot'),
    ('old control-group veto', 'fbcon: fb0 is not primary device',
     '(informational)', '(informational)'),
    ('init reached', 'Run /init as init process',
     'kernel finished, so a later death is userspace or watchdog',
     'kernel never finished booting'),
    ('OpenRC', 'Welcome to openrc',
     'userspace came up', 'no userspace'),
    ('oops', 'Oops', 'the CPU faulted -- this changes everything', 'no oops'),
    ('panic', 'Kernel panic', 'panic recorded', 'no panic'),
    ('watchdog', 'soft lockup', 'watchdog/lockup text present', 'no lockup text'),
    ('backlight error', 'lm3632a_panel_power : error pinfo',
     'panel power helper was already unhappy at this point', 'backlight clean'),
]


def read_lines(path):
    out = []
    raw = Path(path).read_bytes().decode('utf-8', 'replace')
    for line in raw.splitlines():
        m = TS.match(line.strip())
        if m:
            out.append((float(m.group(1)), m.group(2)))
        elif line.strip():
            out.append((None, line.strip()))
    return out


KEY = 'Kernel command line:'


def cmdline_of(lines):
    """The full line is 'Kernel command line: <args>' and the args contain
    colons (console=ttyMSM0,115200n8), so split on the key, never on ':'."""
    for _, text in lines:
        if KEY in text:
            return text.split(KEY, 1)[1].strip()
    return ''


def report(path):
    lines = read_lines(path)
    stamped = [(t, s) for t, s in lines if t is not None]
    print('== %s' % path)
    print('lines %d, timestamped %d' % (len(lines), len(stamped)))
    cmd = cmdline_of(lines)
    print('== command line of the recorded boot')
    print('   %s' % (cmd or '<not found>'))
    late = ',late' in cmd
    print('   variant: %s' % ('LATEBIND (old, fatal path: open called, bind refused)'
                              if late else 'DEFAULT (#39 fix: open skipped, bind at late_initcall)'))
    if stamped:
        print('== time span  %.6f s -> %.6f s' % (stamped[0][0], stamped[-1][0]))
    print('== last 12 lines')
    for t, text in (stamped or lines)[-12:]:
        print('   [%9.6f] %s' % (t, text))
    print('== marker scan')
    for label, needle, yes, no in CHECKS:
        hits = [t for t, s in stamped if needle in s]
        state = 'PRESENT at %s' % (', '.join('%.6f' % h for h in hits[:3])) if hits else 'absent'
        note = yes if hits else no
        if note == '(informational)':
            note = ''
        print('   %-22s %-28s %s' % (label, state, note))
    print('== error-looking lines (first 15)')
    shown = 0
    for t, text in stamped:
        if re.search(r'error|fail|Unable|denied|invalid|refus|BUG|WARNING|not aligned', text, re.I):
            print('   [%9.6f] %s' % (t, text))
            shown += 1
            if shown >= 15:
                break
    if not shown:
        print('   none')
    verdict(stamped, late)


def verdict(stamped, late):
    marker = any('skipping it and binding at late init' in s for _, s in stamped)
    took = any('Console: switching' in s for _, s in stamped)
    init = any('Run /init as init process' in s for _, s in stamped)
    print('== verdict')
    if late:
        print('   control group, expected to hang inside mdss_fb_probe(); treat any '
              'difference from "[~1.04 s] mdss_fb_probe: fb0: split_mode" as new data.')
        return
    if init:
        print('   the default image reached init -> the deferred bind did not kill boot; '
              'a later reset is userspace/watchdog, not fbcon.')
    elif took:
        print('   takeover recorded before the death -> panel text worked, death is after it.')
    elif marker:
        print('   marker printed, no takeover: fb_open() was skipped fine, so the death is '
              'in the deferred path (fbcon_init/mdss_fb_set_par), not in mdss_fb_open(). '
              'That is a different bug than #35-#38.')
    else:
        print('   no marker at all: this boot died before/at fbcon_startup(), i.e. still '
              'inside register_framebuffer() -- same shape as #35-#38.')


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: ramoops-report.py <console-ramoops> [...]')
    for path in sys.argv[1:]:
        report(path)
        print()


if __name__ == '__main__':
    main()

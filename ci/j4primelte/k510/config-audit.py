#!/usr/bin/env python3
"""Audit a kernel .config against what systemd actually asks for.

This exists because "systemd needs 5.10" is only the version half of the story:
a 5.10 kernel built without cgroup v2 controllers, devtmpfs or COMPAT will boot
and then fail in a much more confusing way. The list below is taken from the
Kernel Config Options section of systemd's README (v262), plus the two symbols
that matter for running a 32-bit pmOS rootfs on an arm64 kernel.

    config-audit.py <config> [--mode hard|systemd|userspace] [--json]

Exit status is 0 when every *required* symbol is present with the right value,
1 otherwise. Warnings never change the exit status.

Modes:
    hard      only what cannot work at all without (cgroup v2 core, devtmpfs)
    systemd   the full set systemd lists as required (default)
    userspace what a pmOS armv7 rootfs additionally needs
"""
import argparse
import json
import sys

# symbol -> (expected, severity, why)
#   expected: 'y' (built in), 'm' (module accepted), 'n' (must not be set),
#             'any' (y or m)
REQUIRED = {
    # --- cannot boot systemd at all without these -------------------------
    'CONFIG_CGROUPS':            ('y', 'hard', 'cgroup v2 core; systemd refuses to run without it'),
    'CONFIG_DEVTMPFS':           ('y', 'hard', '/dev population'),
    'CONFIG_INOTIFY_USER':       ('y', 'hard', 'unit file watching'),
    'CONFIG_SIGNALFD':           ('y', 'hard', 'systemd core'),
    'CONFIG_TIMERFD':            ('y', 'hard', 'systemd core'),
    'CONFIG_EPOLL':              ('y', 'hard', 'systemd core'),
    'CONFIG_UNIX':               ('any', 'hard', 'AF_UNIX sockets'),
    'CONFIG_SYSFS':              ('y', 'hard', 'sysfs'),
    # --- autonomous RNDIS gadget for early-boot USB diagnostics -----------
    'CONFIG_USB_GADGET':         ('y', 'hard', 'USB device-mode gadget framework'),
    'CONFIG_USB_CHIPIDEA_UDC':   ('y', 'hard', 'MSM8917 ChipIdea USB device controller'),
    'CONFIG_USB_CHIPIDEA_MSM':  ('y', 'hard', 'Qualcomm ChipIdea glue/PHY integration'),
    'CONFIG_USB_ETH':            ('y', 'hard', 'built-in g_ether binds without initramfs setup'),
    'CONFIG_USB_ETH_RNDIS':      ('y', 'hard', 'RNDIS USB Ethernet enumeration'),
    # --- systemd's own "required" list -----------------------------------
    'CONFIG_DEVTMPFS_MOUNT':     ('y', 'systemd', 'mount devtmpfs on /dev before /usr is up'),
    'CONFIG_PROC_FS':            ('y', 'systemd', 'procfs'),
    'CONFIG_FHANDLE':            ('y', 'systemd', 'open_by_handle_at() for unit/file handles'),
    'CONFIG_TMPFS':              ('y', 'systemd', 'private /tmp and /run'),
    'CONFIG_TMPFS_POSIX_ACL':    ('y', 'systemd', 'ACLs on tmpfs'),
    'CONFIG_TMPFS_XATTR':        ('y', 'systemd', 'xattrs on tmpfs'),
    'CONFIG_SECCOMP':            ('y', 'systemd', 'sandboxing'),
    'CONFIG_SECCOMP_FILTER':     ('y', 'systemd', 'seccomp(2) filtering'),
    'CONFIG_NAMESPACES':         ('y', 'systemd', 'unit sandboxing'),
    'CONFIG_NET':                ('y', 'systemd', 'network stack'),
    'CONFIG_AUTOFS_FS':          ('any', 'systemd', 'autofs for /proc/sys/fs/binfmt_misc'),
    # --- cgroup v2 controllers -------------------------------------------
    'CONFIG_MEMCG':              ('y', 'systemd', 'memory controller (v2)'),
    'CONFIG_CGROUP_SCHED':       ('y', 'systemd', 'cpu controller (v2)'),
    'CONFIG_CPUSETS':            ('y', 'systemd', 'cpuset controller (v2)'),
    'CONFIG_CGROUP_FREEZER':     ('y', 'systemd', 'freezer (v2, kernel >= 5.2)'),
    'CONFIG_CGROUP_PIDS':        ('y', 'systemd', 'pids controller (v2)'),
    'CONFIG_CGROUP_BPF':         ('y', 'systemd', 'device controller (v2, kernel >= 4.15)'),
    'CONFIG_CGROUP_CPUACCT':     ('y', 'systemd', 'cpuacct accounting'),
    'CONFIG_MEMBARRIER':         ('y', 'systemd', 'systemd core'),
    # --- 32-bit pmOS rootfs on an arm64 kernel ---------------------------
    'CONFIG_COMPAT':             ('y', 'userspace', 'armv7 (armhf) userspace on aarch64 kernel'),
    'CONFIG_COMPAT_32BIT_TIME':  ('y', 'userspace', '32-bit time syscalls for armv7 userspace'),
    'CONFIG_SYSVIPC':            ('y', 'userspace', 'APK/Alpine userspace expects it'),
    'CONFIG_DEVPTS_MULTIPLE_INSTANCES': ('y', 'userspace', 'container/debug shells'),
    'CONFIG_BLK_DEV_INITRD':     ('y', 'userspace', 'pmOS initramfs'),
    'CONFIG_RD_GZIP':            ('y', 'userspace', 'gzip initramfs'),
    'CONFIG_EXT4_FS':            ('any', 'userspace', 'pmOS rootfs'),
    'CONFIG_VT':                 ('y', 'userspace', 'console'),
    'CONFIG_UNIX98_PTYS':        ('y', 'userspace', 'ptys for ssh/telnet'),
}

# Things that are commonly on by default in vendor defconfigs and actively hurt
# here, reported as notes only.
NOTES = {
    'CONFIG_CGROUP_DEVICE': ('cgroup v1 device controller; v2 replaces it with CONFIG_CGROUP_BPF'),
    'CONFIG_MEMCG_SWAP': ('cgroup v1-only accounting'),
    'CONFIG_ANDROID_PARANOID_NETWORK': ('blocks socket access for non-AID_INET groups; disable for pmOS'),
    'CONFIG_SECURITY_SELINUX': ('vendor policy is Android-specific; usually disabled for pmOS bring-up'),
}

SEVERITY_ORDER = ['hard', 'systemd', 'userspace']

# symbol -> first kernel that has it at all.
MINIMUM_KERNEL = {
    'CONFIG_MEMBARRIER': (4, 3),
    'CONFIG_CGROUP_PIDS': (4, 5),
    'CONFIG_CGROUP_BPF': (4, 15),
    'CONFIG_AUTOFS_FS': (4, 18),
    'CONFIG_COMPAT_32BIT_TIME': (4, 17),
}

# Same feature, different name depending on kernel generation. The first symbol
# that exists in the config decides.
ALIASES = {
    'CONFIG_AUTOFS_FS': ('CONFIG_AUTOFS4_FS',),
    'CONFIG_EXT4_FS': ('CONFIG_EXT4_FS', 'CONFIG_EXT4DEV_FS'),
}


def parse_config(path):
    # The banner ("Linux/arm64 5.10.110 Kernel Configuration") is the one place
    # a version is guaranteed, even in trees without IKCONFIG.
    values = {'CONFIG_VERSION_SIGNATURE': ''}
    with open(path, 'r', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if line.startswith('# ') and 'Kernel Configuration' in line:
                values['CONFIG_VERSION_SIGNATURE'] = line
            if line.startswith('# ') and line.endswith(' is not set'):
                values[line[2:-len(' is not set')]] = 'n'
            elif '=' in line and not line.startswith('#'):
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip()
    return values


def kernel_version(values):
    """(major, minor) from CONFIG_VERSION_SIGNATURE ('Linux version 5.10.110...')."""
    # Vendor kernels often lack CONFIG_IKHEADERS/IKCONFIG; fall back to the
    # banner in the localversion string, which is where 3.18 keeps it.
    sig = values.get('CONFIG_VERSION_SIGNATURE', '') or values.get('CONFIG_LOCALVERSION', '')
    for token in sig.replace('(', ' ').split():
        if token[:1].isdigit() and token.count('.') >= 1:
            parts = token.split('.')
            try:
                return int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
    return None


def is_set(value):
    return value in ('y', 'm') or (value is not None and value not in ('n', ''))


def check(values, mode):
    max_index = SEVERITY_ORDER.index(mode)
    version = kernel_version(values)
    results = []
    for symbol, (expected, severity, why) in REQUIRED.items():
        # Some of systemd's list only exists in newer kernels. Flagging
        # CONFIG_CPUSETS as "missing" on a 3.18 tree is noise: it is not
        # absent, it is unavailable. Label those instead of hiding them.
        minimum = MINIMUM_KERNEL.get(symbol)
        if minimum and version and version < minimum:
            results.append({'symbol': symbol, 'expected': expected, 'actual': None,
                            'ok': True, 'severity': severity, 'skipped': True,
                            'why': why + f' (needs kernel {minimum[0]}.{minimum[1]}+; '
                                         f'this tree is {version[0]}.{version[1]})'})
            continue
        if SEVERITY_ORDER.index(severity) > max_index:
            continue
        actual = values.get(symbol)
        # Follow the old name of the same feature, if the kernel only has that.
        if not is_set(actual):
            for alias in ALIASES.get(symbol, ()):
                if alias in values:
                    actual = values[alias]
                    break
        if expected == 'y':
            ok = actual == 'y'
        elif expected in ('m', 'any'):
            ok = is_set(actual)
        elif expected == 'n':
            ok = not is_set(actual)
        else:  # pragma: no cover - defensive
            raise ValueError(expected)
        results.append({
            'symbol': symbol,
            'expected': expected,
            'actual': actual if actual is not None else 'ABSENT',
            'skipped': False,
            'ok': ok,
            'severity': severity,
            'why': why,
        })
    return sorted(results, key=lambda r: (SEVERITY_ORDER.index(r['severity']), r['symbol']))


def notes(values):
    out = []
    for symbol, why in NOTES.items():
        if is_set(values.get(symbol)):
            out.append({'symbol': symbol, 'why': why})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('config')
    ap.add_argument('--mode', choices=SEVERITY_ORDER, default='systemd')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    values = parse_config(args.config)
    if not values:
        print(f'CONFIG FAIL: {args.config} parsed as empty', file=sys.stderr)
        return 1
    version = values.get('CONFIG_LOCALVERSION', '').strip('"')
    results = check(values, args.mode)
    failed = [r for r in results if not r['ok']]

    if args.json:
        json.dump({'localversion': version, 'results': results,
                   'notes': notes(values), 'failed': len(failed)},
                  sys.stdout, indent=2)
        sys.stdout.write('\n')
    else:
        print(f'config audit ({args.mode}): {len(results)} symbols checked')
        for r in results:
            mark = 'n/a ' if r.get('skipped') else ('ok  ' if r['ok'] else 'FAIL')
            got = 'n/a' if r.get('skipped') else (r['actual'] or 'ABSENT')
            print(f"  {mark} {r['symbol']:<34} want={r['expected']:<4} got={got:<6} {r['why']}")
        for n in notes(values):
            print(f"  note {n['symbol']}: {n['why']}")
        if failed:
            print(f'\n{len(failed)} required symbol(s) missing; the kernel would boot '
                  f'but systemd or the pmOS userspace would fail.', file=sys.stderr)
        else:
            print('\nall required symbols present')

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

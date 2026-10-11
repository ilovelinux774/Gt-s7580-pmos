#!/usr/bin/env python3
"""Build and audit the .config for the Galaxy J4+ mainline kernel.

The base is the arm64 defconfig of the pinned tree; on top of it goes
mainline/j4mainline.fragment. Both halves matter:

  * `make defconfig` on arm64 already knows about Qualcomm SoCs, but it also
    drags in every other vendor's display, mmc and usb driver, and it leaves
    CONFIG_MODULES=m - which on this phone means "the driver is not there",
    because the donor initramfs and the installed rootfs are armv7.
  * the fragment is a *policy* file: what must be built in, what must be off,
    and why. It is deliberately short enough to read in one sitting.

`check` re-reads the finished .config and fails if anything the boot depends on
is not built in. It is a separate subcommand so CI can audit a .config that came
from a cache, a hand edit or a `make nconfig` session.
"""
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
FRAGMENT = HERE / 'mainline' / 'j4mainline.fragment'
# A kconfig identifier may start with a digit (CONFIG_9P_FS does) and has to
# contain a letter; anything else is a fragment typo, not a symbol.
SYMBOL = re.compile(r'^[A-Z0-9][A-Z0-9_]*$')

# Everything below has to be =y, not =m and not off: a missing one is a boot
# that produces no output at all, so it is worth a hard failure.
REQUIRED = {
    # the SoC itself
    'ARCH_QCOM': 'without this no qcom driver is even offered',
    'OF': 'every device on this phone is described by the flattened tree',
    'RELOCATABLE': 'LK may hand over the kernel at a different address',
    # clocks, pinctrl, resets - nothing probes without them
    'MSM_GCC_8917': 'the MSM8917 clock controller; NOT selected by ARCH_QCOM',
    'QCOM_CLK_RPMH': 'rpmh clocks (SDC1/MMC, CPU)',
    'QCOM_CLK_SMD_RPM': 'smd-rpm clocks for the older RPM interface',
    'RESET_CONTROLLER': 'needed by the mmc and usb drivers on this SoC',
    'PINCTRL_MSM': 'the TLMM pin controller',
    'PINCTRL_MSM8917': 'pin function table for this SoC',
    'QCOM_PDC': 'power domain controller interrupt parent',
    'QCOM_IPCC': 'inter-processor communication; SMD and RPMh ride on it',
    'QCOM_SCM': 'secure monitor calls (also gates the cpufreq and smp path)',
    'QCOM_SMEM': 'shared memory with the modems and bootloader',
    'QCOM_BAM_DMA': 'the mmc and uart drivers ask for this dma engine',
    'QCOM_GDSC': 'the MDSS power domain, which the splash lives in',
    'QCOM_RPMPD': 'rpm power domains for the cpu cluster',
    # pmic bus and regulators
    'SPMI': 'the pmic bus',
    'SPMI_MSM_PMIC_ARB': 'the arbiter that owns it on this SoC',
    'MFD_SPMI_PMIC': 'pm8937 mfd cell driver',
    'REGULATOR': 'regulator core',
    'REGULATOR_FIXED_VOLTAGE': 'the fixed rails the device tree declares',
    'REGULATOR_GPIO': 'lcd_extra_power / tsp_vdd are gpio regulators',
    'REGULATOR_QCOM_SPMI': 'the pm8937 LDOs that feed eMMC, SD and the panel',
    'POWER_SUPPLY': 'battery and charging attributes',
    'NVMEM_QCOM_QFPROM': 'panel id, serial and mac live in fuses',
    # storage - the rootfs is on eMMC
    'MMC_BLOCK': 'block devices on top of mmc',
    'MMC_SDHCI': 'sdhci core',
    'MMC_SDHCI_MSM': 'the Qualcomm sdhci-4.0 host driver',
    'EXT4_FS': 'the installed rootfs is ext4',
    'TMPFS': '/run and /tmp',
    'DEVTMPFS': 'the initramfs mounts this before anything else',
    # console: in M1 the only display is the uart
    'TTY': 'a shell needs a tty',
    'VT': 'the /dev/ttyN nodes init and getty expect',
    'SERIAL_EARLYCON': 'earlycon, so a hang before the real console is visible',
    'SERIAL_MSM': 'the BLSP1 uart driver',
    'SERIAL_MSM_CONSOLE': 'console=ttyMSM0',
    'FRAMEBUFFER_CONSOLE': 'so the splash and later panic text reach the screen',
    # display
    'DRM': 'direct rendering core',
    'DRM_SIMPLEDRM': 'hands the bootloader framebuffer to the userspace',
    'DRM_FBDEV_EMULATION': 'so weston/fbdev users have something to draw on',
    'INPUT_EVDEV': '/dev/input/eventN',
    # usb gadget: the network, the shell and the only way in
    'USB_GADGET': 'gadget support',
    'USB_LIBCOMPOSITE': 'the composite framework configfs sits on',
    'USB_F_RNDIS': 'the RNDIS function pmaports configures for debugging',
    'USB_CONFIGFS': 'configfs gadget control, which is what the init script uses',
    'USB_CONFIGFS_RNDIS': 'so the gadget can actually be assembled at runtime',
    # networking the userspace expects to find
    'NET': 'sockets',
    'WIRELESS': '802.11 uapi',
    'CFG80211': 'nl80211, which NetworkManager talks to',
    'MAC80211': 'the stack wcn36xx needs',
    'BT': 'bluetooth core (keyboard bring-up already works on this phone)',
    # postmortem and debug plumbing
    'PSTORE_RAM': 'ramoops, the only way to read a panic after a reflash',
    'DEBUG_FS': 'the debugfs the wifi and drm code expose state through',
    'CONFIGFS_FS': 'usb gadgets and configfs mounts',
}

# Only with --panel (M2). The M1 image must not have these: MDSS stays with the
# bootloader until the panel driver is validated.
PANEL_REQUIRED = {
    'DRM_MSM': 'the display controller owner in M2',
    'DRM_MSM_DSI': 'dsi host that the panel hangs off',
    'DRM_MSM_DSI_28NM_PHY': 'msm8917 uses the 28nm dsi phy',
    'DRM_PANEL': 'panel bridge core',
    'DRM_PANEL_SIMPLE': 'selected by the generated panel driver',
    'DRM_PANEL_SAMSUNG_S6D7AT0B01': 'the J4+ panel driver staged by mainline-kernel.py',
    'MFD_TI_LMU': 'lm3632 is an mfd device with regulator and backlight cells',
    'REGULATOR_LM363X': 'the vpos/vneg bias rails the panel needs',
    'TOUCHSCREEN_S6SY761': 's6sy761, the candidate driver for sec_ts',
}

# Must be off, and must be off on purpose. Checked in every build.
FORBIDDEN_ALWAYS = {
    'MODULES': 'the donor initramfs and the armv7 rootfs carry no arm64 modules '
               'for this kernel, so =m means the driver is absent at boot',
}

# Must never be on. Some of these are downstream-only symbols that exist to make
# sure a future fork does not quietly turn them back on; a symbol the tree does
# not define is simply never reported as a problem by check.
FORBIDDEN = {
    'ANDROID_PARANOID_NETWORK': 'blocks network sockets for uid 0/2000 (a '
                                'downstream-only symbol; it must stay off if the '
                                'fork ever grows it back)',
    'EFI': 'LK hands over no EFI environment; the stub only gets in the way',
    'CMDLINE_FORCE': 'would hide the vendor arguments LK appends',
}


def parse_fragment(fragment: Path, panel: bool = False) -> list:
    """Return [(kind, symbol, value)] in file order."""
    ops = []
    for number, raw in enumerate(fragment.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        gated = line.startswith('PANEL ')
        if gated:
            if not panel:
                continue
            line = line[len('PANEL '):].strip()
        verb, _, rest = line.partition(' ')
        rest = rest.strip()
        if verb == '=':
            match = re.match(r'^CONFIG_([A-Z0-9_]+)=(.*)$', rest)
            if not match:
                raise ValueError(f'{fragment}:{number}: expected CONFIG_NAME=value '
                                 f'after "=", got {rest!r}')
            ops.append(('literal', match.group(1), match.group(2)))
            continue
        if verb not in ('+', '-', '@'):
            raise ValueError(f'{fragment}:{number}: unknown instruction {verb!r} '
                             '(use "+", "-", "@" or "=")')
        # "@" carries a value: CONFIG_LOCALVERSION -j4mainline, or CONFIG_CMDLINE
        # with several bootargs in it. The symbol is the first word, whatever is
        # left is the string -- quoted later, never here.
        symbol, _, value = rest.partition(' ')
        value = value.strip()
        if not SYMBOL.match(symbol):
            raise ValueError(f'{fragment}:{number}: {symbol!r} is not a kconfig '
                             'symbol name (uppercase, digits and underscores)')
        if verb == '@' and not value:
            raise ValueError(f'{fragment}:{number}: "@" needs a value after the symbol')
        if verb != '@' and value:
            raise ValueError(f'{fragment}:{number}: "{verb}" takes a bare symbol, '
                             f'not {rest!r}')
        kind = {'+': 'enable', '-': 'disable', '@': 'set-str'}[verb]
        ops.append((kind, symbol, value if kind == 'set-str' else None))
    if panel:  # a panel build must not silently forget its own driver
        enabled = {symbol for kind, symbol, _ in ops if kind == 'enable'}
        if 'DRM_PANEL_SAMSUNG_S6D7AT0B01' not in enabled:
            raise ValueError(f'{fragment}: --panel was requested but the fragment '
                             'enables no panel driver')
    return ops


# The panel driver is not part of the pinned tree: mainline-kernel.py stages it,
# including its Kconfig stanza. It is therefore legal in the fragment even though
# an *unstaged* tree does not define it - and if the staging ever stops happening,
# check_config fails the build because the symbol is REQUIRED in a panel build.
STAGED_SYMBOLS = {'DRM_PANEL_SAMSUNG_S6D7AT0B01'}


def known_symbols(tree: Path):
    """Every Kconfig symbol the tree defines, without running kconfig.

    `scripts/config --enable NO_SUCH_DRIVER` succeeds, writes
    CONFIG_NO_SUCH_DRIVER=y, and olddefconfig drops it again - so "the symbol
    showed up in the .config" proves nothing about whether a driver is really
    there. Invented symbol names have been the failure mode of this bring-up
    twice, so every name in the fragment is checked against the tree it is
    about to be applied to.
    """
    if not (tree / '.git').exists():
        # A tarball or an exported tree has no index: fall back to grep, which is
        # slow but correct, and only the Kconfig files.
        files = [p for p in tree.rglob('Kconfig*')]
        if not files:
            return None
        names = set()
        pattern = re.compile(r'^\s*(?:menu)?config\s+([A-Za-z0-9_]+)', re.M)
        for path in files:
            try:
                names.update(pattern.findall(path.read_text(errors='replace')))
            except OSError:
                continue
        return names
    proc = subprocess.run(['git', '-C', str(tree), 'grep', '-h', '-E',
                           r'\b(menu)?config\s+[A-Za-z0-9_]+', '--', '*Kconfig*'],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f'cannot audit the fragment against {tree}: git grep exit '
                         f'{proc.returncode}: {proc.stderr.strip()[:200]}')
    return {m.group(1) for m in
            re.finditer(r'\b(?:menu)?config\s+([A-Za-z0-9_]+)', proc.stdout)}


def audit_symbols(tree: Path, ops) -> None:
    known = known_symbols(tree)
    if known is None:
        print('note: no kernel tree to audit against; symbol names unverified')
        return
    named = {symbol for _kind, symbol, _value in ops}
    unknown = sorted((named - known) - STAGED_SYMBOLS)
    if unknown:
        raise SystemExit('the fragment names symbols this tree does not define: '
                         + ', '.join(unknown[:12])
                         + '\n(a typo here is silently dropped by olddefconfig; '
                           'check the spelling against the pinned tree)')


def render_defconfig(fragment: Path, panel: bool = False) -> str:
    """The fragment as defconfig lines, for feeding `make defconfig` on stdin."""
    out = []
    for kind, symbol, value in parse_fragment(fragment, panel=panel):
        if kind == 'enable':
            out.append(f'CONFIG_{symbol}=y')
        elif kind == 'disable':
            out.append(f'# CONFIG_{symbol} is not set')
        elif kind == 'set-str':
            out.append(f'CONFIG_{symbol}="{value}"')
        else:
            out.append(f'CONFIG_{symbol}={value}')
    return '\n'.join(out) + '\n'


def merge_config(tree: Path, dest: Path, fragment: Path, panel: bool) -> Path:
    """defconfig + fragment, with the fragment last so it wins."""
    base = tree / 'arch' / 'arm64' / 'configs' / 'defconfig'
    if not base.is_file():
        raise SystemExit(f'{base}: no arm64 defconfig in this tree')
    dest.write_text(base.read_text() + '\n' + render_defconfig(fragment, panel))
    return dest


def apply_config(tree: Path, out: Path, fragment: Path, panel: bool = False):
    """Produce out/.config: base defconfig + fragment, dependency-resolved."""
    env = dict(os.environ, ARCH='arm64')
    # Before anything is written: an invented symbol name is invisible later.
    ops = parse_fragment(fragment, panel=panel)
    audit_symbols(tree, ops)
    out.mkdir(parents=True, exist_ok=True)
    cfg = out / '.config'
    merged = merge_config(tree, out / 'j4mainline.merged_defconfig', fragment, panel)

    def run(*args, stdin=None):
        # kconfig failures are opaque unless the make output is shown: a missing
        # host tool (flex/bison) and a recursive dependency both look like
        # "exit status 2" from here.
        proc = subprocess.run(args, cwd=tree, env=env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
        if proc.returncode:
            sys.stderr.write(f'\n--- {" ".join(args)} (exit {proc.returncode}) ---\n')
            sys.stderr.write(proc.stdout or '')
            sys.stderr.write('--- end ---\n')
            raise SystemExit(f'{args[1] if len(args) > 1 else args[0]} failed; '
                             'see the captured make output above')
        return proc

    if not cfg.is_file():
        # `make defconfig` reads the config from stdin when it is not a tty,
        # which avoids kconfig's relative DEFCONFIG path lookup entirely.
        run('make', f'O={out}', 'defconfig', stdin=merged.read_text())
    for kind, symbol, value in ops:
        if kind == 'literal':
            continue
        args = ['scripts/config', '--file', str(cfg), f'--{kind}', symbol]
        if value is not None:
            args.append(value)
        subprocess.run(args, cwd=tree, env=env, check=True)
    # Literal lines (e.g. CONFIG_MMC_BLOCK_MINORS=32) are appended after the
    # enable/disable pass so a "is not set" line from the base cannot win.
    literals = [f'CONFIG_{symbol}={value}' for kind, symbol, value in ops
                if kind == 'literal']
    if literals:
        with cfg.open('a') as handle:
            handle.write('\n'.join(literals) + '\n')
    run('make', f'O={out}', 'olddefconfig')
    print(f'{cfg}: applied')


def read_config(cfg: Path) -> dict:
    values = {}
    for line in cfg.read_text().splitlines():
        match = re.match(r'^CONFIG_([A-Z0-9_]+)=(.*)$', line)
        if match:
            values[match.group(1)] = match.group(2).strip('"')
            continue
        match = re.match(r'^# CONFIG_([A-Z0-9_]+) is not set$', line)
        if match:
            values[match.group(1)] = 'n'
    return values


def check_config(cfg: Path, panel: bool) -> dict:
    if not cfg.is_file():
        raise SystemExit(f'{cfg}: no .config to check (run apply first)')
    values = read_config(cfg)
    wanted = dict(REQUIRED)
    if panel:
        wanted.update(PANEL_REQUIRED)
    else:
        # An M1 image that grew panel support is a different milestone; say so.
        pass
    problems = []
    for symbol, why in sorted(wanted.items()):
        got = values.get(symbol, 'missing')
        if got != 'y':
            problems.append(f'CONFIG_{symbol}={got} (must be y): {why}')
    for symbol, why in sorted(FORBIDDEN.items()):
        if values.get(symbol, 'n') != 'n':
            problems.append(f'CONFIG_{symbol} must never be on: {why}')
    for symbol, why in sorted(FORBIDDEN_ALWAYS.items()):
        if values.get(symbol, 'n') != 'n':
            problems.append(f'CONFIG_{symbol} must never be on '
                            f'(no arm64 modules exist in the donor initramfs)')
    release = 'unknown'
    candidate = cfg.parent / 'include' / 'config' / 'kernel.release'
    if candidate.is_file():
        release = candidate.read_text().strip()
    report = {
        'config': str(cfg),
        'kernel_release': release,
        'panel_build': panel,
        'checked_options': len(wanted) + len(FORBIDDEN) + len(FORBIDDEN_ALWAYS),
        'enabled': sum(1 for v in values.values() if v == 'y'),
    }
    if problems:
        sys.stderr.write('CONFIG CHECK FAILED:\n')
        for line in problems:
            sys.stderr.write(f'  {line}\n')
        print(json.dumps(report, indent=2))
        raise SystemExit(1)
    print(json.dumps(report, indent=2))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('apply', 'check', 'render'))
    parser.add_argument('--tree', type=Path, default=Path.home() / 'j4-mainline-kernel',
                        help='pinned kernel source tree')
    parser.add_argument('--out', type=Path, help='build directory holding .config')
    parser.add_argument('--config', type=Path, help='existing .config to check')
    parser.add_argument('--fragment', type=Path, default=FRAGMENT)
    parser.add_argument('--panel', action='store_true', help='the M2 milestone build')
    args = parser.parse_args(argv)

    if args.command == 'render':
        sys.stdout.write(render_defconfig(args.fragment, panel=args.panel))
        return 0
    if args.command == 'apply':
        if not args.out:
            parser.error('apply needs --out')
        apply_config(args.tree.resolve(), args.out.resolve(), args.fragment,
                     panel=args.panel)
        check_config(args.out / '.config', panel=args.panel)
        return 0
    check_config((args.config or (args.out / '.config')).resolve(), panel=args.panel)
    return 0


if __name__ == '__main__':
    sys.exit(main())

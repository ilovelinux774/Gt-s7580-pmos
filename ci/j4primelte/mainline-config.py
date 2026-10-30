#!/usr/bin/env python3
"""Build and audit the .config for the Galaxy J4+ mainline kernel.

The base is the arm64 defconfig of the pinned tree; on top of it goes
mainline/j4mainline.fragment. Both halves matter:

  * `make defconfig` on arm64 already knows about Qualcomm SoCs, but also
    enables many unrelated platforms and modular drivers. We trim those first,
    then explicitly enable the J4+ drivers as built-ins: the ARM32 donor
    initramfs and existing rootfs do not contain modules for this kernel.
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
    'COMPAT': 'the donor initramfs and existing rootfs contain ARM32 binaries',
    'BLK_DEV_INITRD': 'the bootloader-supplied initramfs must be accepted',
    'RD_GZIP': 'the known-good donor initramfs is gzip compressed',
    'OF': 'every device on this phone is described by the flattened tree',
    'RELOCATABLE': 'LK may hand over the kernel at a different address',
    # clocks, pinctrl, resets - nothing probes without them
    'MSM_GCC_8917': 'the MSM8917 clock controller; NOT selected by ARCH_QCOM',
    'QCOM_CLK_RPMH': 'rpmh clocks (SDC1/MMC, CPU)',
    'QCOM_CLK_SMD_RPM': 'smd-rpm clocks for the older RPM interface',
    'RPMSG': 'the messaging bus underneath the SMD RPM transport',
    'RPMSG_QCOM_SMD': 'Qualcomm SMD transports regulator and clock requests',
    'QCOM_SMD_RPM': 'the older Qualcomm RPM transport for eMMC supplies',
    'REGULATOR_QCOM_SMD_RPM': 'RPM-controlled PMIC supplies for eMMC',
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
    'USB_CHIPIDEA': 'the qcom,ci-hdrc USB controller in msm8917.dtsi',
    'USB_CHIPIDEA_UDC': 'USB device mode on the ChipIdea controller',
    'USB_CHIPIDEA_MSM': 'Qualcomm platform glue for the ChipIdea controller',
    'USB_ULPI_BUS': 'the USB controller talks to its transceiver over ULPI',
    'PHY_QCOM_USB_HS_28NM': 'msm8917.dtsi uses qcom,usb-hs-28nm-femtophy',
    'EXTCON': 'the gadget controller receives USB VBUS state via extcon',
    'EXTCON_USB_DUMMY': 'provides linux,extcon-usb-dummy declared in the J4 DT',
    'USB_F_ACM': 'optional serial console inside the donor debug shell',
    'USB_F_NCM': 'the initramfs can fall back to the NCM gadget function',
    'USB_CONFIGFS_ACM': 'configfs can create the donor debug shell ACM gadget',
    'USB_CONFIGFS_NCM': 'configfs can create an NCM network gadget',
    'USB_LIBCOMPOSITE': 'the composite framework configfs sits on',
    'USB_F_RNDIS': 'the RNDIS function pmaports configures for debugging',
    'USB_CONFIGFS': 'configfs gadget control, which is what the init script uses',
    'USB_CONFIGFS_RNDIS': 'so the gadget can actually be assembled at runtime',
    # networking the userspace expects to find
    'NET': 'sockets',
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
    'I2C_GPIO': 'the panel bias chip uses the GPIO-backed I2C bus in the panel DT',
    'MFD_TI_LMU': 'lm3632 is an mfd device with regulator and backlight cells',
    'REGULATOR_LM363X': 'the vpos/vneg bias rails the panel needs',
    'TOUCHSCREEN_S6SY761': 's6sy761, the candidate driver for sec_ts',
}

# Must be off, and must be off on purpose. Checked in every build.
FORBIDDEN_ALWAYS = {
    'MODULES': 'the donor initramfs and the armv7 rootfs carry no arm64 modules '
               'for this kernel, so =m means the driver is absent at boot',
}

FORBIDDEN_M1 = {
    'DRM_MSM': 'M1 does not take ownership of the display controller',
    'DRM_PANEL_SAMSUNG_S6D7AT0B01': 'the panel belongs to the later M2 build',
    'TOUCHSCREEN_S6SY761': 'touch belongs to a later milestone',
    'PCI': 'not required for the M1 USB debug shell',
    'USB': 'USB host stacks are not required for the USB gadget',
    'WIRELESS': 'Wi-Fi is a later milestone',
    'BT': 'Bluetooth is a later milestone',
    'IPV6': 'the debug shell uses IPv4',
    'F2FS_FS': 'the M1 debug shell does not mount f2fs',
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
    # A partial checkout is more dangerous than no tree at all: the answer would
    # be a smaller set of real symbols, and every driver left out would then be
    # reported as invented. Require the files an arm64 build reads, and return
    # None - "unverified", the same answer as no tree at all - when they are not.
    for required in ('arch/arm64/Kconfig', 'drivers/gpu/drm/Kconfig',
                     'drivers/regulator/Kconfig', 'init/Kconfig'):
        if not (tree / required).is_file():
            return None
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


def prune_inherited_config(text: str, platform_kconfig: str):
    """Disable unrelated ARM64 platform menus and inherited =m drivers.

    Turning MODULES off while carrying the generic ARM64 defconfig's hundreds of
    =m entries silently promotes unrelated drivers to built-ins. Besides wasted
    build time, the enlarged uncompressed Image can collide with the bootloader's
    fixed DTB/initramfs addresses. Keep only Qualcomm platform menus; the explicit
    fragment re-enables every driver this particular board needs.
    """
    text, modules = re.subn(r'^CONFIG_([A-Z0-9_]+)=m$',
                            r'# CONFIG_\1 is not set', text, flags=re.M)
    platforms = set(re.findall(r'^(?:menuconfig|config) (ARCH_[A-Z0-9_]+)$',
                               platform_kconfig, flags=re.M))
    if 'ARCH_QCOM' not in platforms:
        raise ValueError('arm64 platform definitions do not include ARCH_QCOM')
    disabled = 0
    for symbol in sorted(platforms - {'ARCH_QCOM'}):
        text, count = re.subn(r'^CONFIG_' + re.escape(symbol) + r'=[ym]$',
                              '# CONFIG_' + symbol + ' is not set', text,
                              flags=re.M)
        disabled += count
    return text, modules, disabled


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
        # Start with the tree's actual arm64 defconfig, then discard unrelated
        # modules/platforms BEFORE disabling MODULES or Kconfig promotes them.
        run('make', f'O={out}', 'defconfig')
        platforms = tree / 'arch' / 'arm64' / 'Kconfig.platforms'
        if not platforms.is_file():
            raise SystemExit(f'{platforms}: missing ARM64 platform menu')
        trimmed, modules, platforms_disabled = prune_inherited_config(
            cfg.read_text(), platforms.read_text())
        cfg.write_text(trimmed)
        print(f'pruned {modules} inherited modules and {platforms_disabled} '
              'non-Qualcomm platform menus before applying the J4+ fragment')
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
    if not panel:
        for symbol, why in sorted(FORBIDDEN_M1.items()):
            if values.get(symbol, 'n') != 'n':
                problems.append(f'CONFIG_{symbol} must be off in M1: {why}')
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

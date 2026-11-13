#!/usr/bin/env python3
"""Check a config fragment against a kernel tree without building anything.

`scripts/config` accepts any symbol name and exits 0; `olddefconfig` then drops
the ones the tree does not have. The result is a kernel that builds cleanly and
silently lacks a driver, which is the worst failure mode for bring-up: the
image boots (maybe) and the hardware is missing with no error anywhere.

So the fragment is checked statically against the tree's own Kconfig files
first. Every `config FOO` / `menuconfig FOO` definition is collected, and each
fragment symbol must appear in that set.

    fragment-audit.py <tree> <fragment> [--json]

Exit status: 0 if every symbol is defined, 1 otherwise. Symbols that are
arch-specific or driver-specific are not filtered out: if the name is not in
the tree at all, the fragment is wrong.
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEF_RE = re.compile(r'^\s*(?:menu)?config\s+([A-Z0-9_]+)\s*$')


def tree_symbols(tree: Path):
    """All CONFIG_* names defined anywhere in the tree's Kconfig files."""
    names = set()
    for path in tree.rglob('Kconfig*'):
        # .git, Kconfig.orig, and build output directories are not definitions.
        if '.git' in path.parts or 'Kconfig.orig' in path.name:
            continue
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        for line in text.splitlines():
            m = DEF_RE.match(line)
            if m:
                names.add(m.group(1))
    return names


def parse_fragment(path: Path):
    out = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        # "PANEL <directive>" means: only in the panel build. Strip it and audit
        # the inner directive, so panel symbols are checked too.
        if line.startswith('PANEL '):
            line = line[len('PANEL '):].strip()
        parts = line.split(None, 1)
        op = parts[0]
        rest = parts[1] if len(parts) > 1 else ''
        if op == '!':
            # Auditor directives, e.g. "! SUPERSEDES DRM_PANEL_FOO". A fragment
            # cannot always be edited (it is kept verbatim from another branch),
            # so the later file declares what it replaced by name. Anything not
            # declared stays fatal.
            sub = rest.split()
            if len(sub) == 2 and sub[0] == 'SUPERSEDES':
                out.append({'line': number, 'raw': line, 'verb': '!',
                            'symbol': sub[1], 'supersedes': sub[1]})
            else:
                out.append({'line': number, 'raw': line, 'verb': '!', 'symbol': None})
            continue
        if op in ('+', '-', '@'):
            # "+ SYM" / "- SYM" / "@ SYM value": verb and name are separate
            symbol = rest.split()[0] if rest else ''
            verb = op
        elif op[0] == '=':
            # "= CONFIG_FOO=1": passed through verbatim
            symbol = line.lstrip('=').strip().split('=')[0].strip()
            if symbol.startswith('CONFIG_'):
                symbol = symbol[len('CONFIG_'):]
            verb = '='
        elif op[:1] in '+-@' and len(op) > 1:
            # "+SYM" (no space), as used by scripts/config callers
            symbol = op[1:]
            verb = op[0]
        else:
            out.append({'line': number, 'raw': raw, 'verb': None, 'symbol': None})
            continue
        out.append({'line': number, 'raw': line, 'verb': verb, 'symbol': symbol})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('tree')
    ap.add_argument('fragment', nargs='+')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    tree = Path(args.tree)
    fragments = [Path(f) for f in args.fragment]
    for p, what in [(tree, 'tree')] + [(f, 'fragment') for f in fragments]:
        if not p.exists():
            print(f'fragment-audit: {what} does not exist: {p}', file=sys.stderr)
            return 1

    defined = tree_symbols(tree)
    if not defined:
        print('fragment-audit: no Kconfig symbols found; is this a kernel tree?',
              file=sys.stderr)
        return 1

    entries = []
    for f in fragments:
        for e in parse_fragment(f):
            e['file'] = f.name
            entries.append(e)
    unknown = []
    malformed = []
    checked = 0
    for e in entries:
        if e['verb'] is None:
            malformed.append(e)
            continue
        if e['verb'] == '!':
            checked += 1
            if not e.get('supersedes'):
                malformed.append(e)
            continue
        checked += 1
        if not e['symbol']:
            unknown.append(e)
        elif e['symbol'] not in defined and e['verb'] != '-':
            # Disabling a symbol that does not exist is exactly how a tree
            # without that feature is handled; enabling one is a bug.
            unknown.append(e)

    # A fragment kept verbatim from another branch can name a symbol this tree
    # does not have; the overrides file then declares it superseded and enables
    # the equivalent that does exist. Declared is not the same as unnoticed:
    # superseded symbols are still printed.
    superseded = {e['symbol'] for e in entries if e['verb'] == '!' and e.get('supersedes')}
    resolved_notes = [e for e in unknown if e['verb'] == '+' and e['symbol'] in superseded]
    unknown = [e for e in unknown if not (e['verb'] == '+' and e['symbol'] in superseded)]

    if args.json:
        json.dump({'tree': str(tree), 'fragment': str(fragment),
                   'symbols_in_tree': len(defined), 'directives': checked,
                   'unknown': [{'line': e['line'], 'raw': e['raw'],
                                'symbol': e['symbol']} for e in unknown],
                   'superseded': [e['symbol'] for e in resolved_notes],
                   'malformed': [{'line': e['line'], 'raw': e['raw']} for e in malformed]},
                  sys.stdout, indent=2)
        sys.stdout.write('\n')
    else:
        print(f'fragment audit: {checked} directives, {len(defined)} symbols in tree')
        for e in resolved_notes:
            print(f"  {e.get('file', '')}:{e['line']}: superseded by the overrides file: {e['symbol']}")
        for e in malformed[:20]:
            print(f"  line {e['line']}: cannot parse: {e['raw']!r}")
        for e in unknown[:40]:
            print(f"  {e.get('file', '')}:{e['line']}: not in this tree: {e['raw']!r}")
        if len(unknown) > 40:
            print(f'  ... and {len(unknown) - 40} more')
        if not unknown and not malformed:
            print('  every fragment symbol exists in this tree')

    # A conflicting directive is resolved by a later fragment (the overrides
    # file), so it is reported but is not fatal on its own.
    return 1 if (unknown or malformed) else 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Mark optional SUSFS credential helpers as possibly unused, when needed."""

import argparse
from pathlib import Path
import re

from fix_susfs_priority_uid import guarded


PRIORITY_CONFIG = "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER"
HELPERS = {
    "susfs_switch_to_ksu_domain": r"struct\s+cred\s*\*\s*",
    "susfs_restore_creds": r"void\s+",
}


def code_only(text):
    # Keep offsets and preprocessor guards, ignoring names in comments/strings.
    return re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        text,
        flags=re.S,
    )


def fix(kernel_root):
    path = kernel_root / "fs/susfs.c"
    if not path.is_file():
        print("SUSFS credentials: no fs/susfs.c; no changes needed")
        return []

    original = path.read_text()
    code = code_only(original)
    edits = []
    for name, return_type in HELPERS.items():
        # Only recognize bare static definitions. Upstream annotations, inline
        # definitions, removed helpers and unfamiliar declarations are left alone.
        pattern = (r"(?m)^[ \t]*(?P<static>static)\s+" + return_type +
                   re.escape(name) + r"\s*\([^();{}]*\)\s*\{")
        for match in re.finditer(pattern, code):
            if guarded(code, match.start(), PRIORITY_CONFIG):
                continue
            edits.append((match.end("static"), name))

    if not edits:
        print("SUSFS credentials: no unprotected bare helpers; no changes needed")
        return []

    text = original
    for offset, name in sorted(edits, reverse=True):
        # Callers depend on Kconfig; counting textual references cannot tell
        # whether a helper is used. Keep the body callable in every configuration.
        text = text[:offset] + " __maybe_unused" + text[offset:]
        print(f"SUSFS credentials: marked {name} __maybe_unused")
    path.write_text(text)
    return [path]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path, help="actual kernel source root, e.g. kernel_workspace/common")
    args = parser.parse_args()
    fix(args.kernel_root.resolve())


if __name__ == "__main__":
    main()

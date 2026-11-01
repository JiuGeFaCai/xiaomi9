#!/usr/bin/env python3
"""Guard optional KernelSU SELinux recording hooks absent from the selected SUSFS."""

import argparse
from pathlib import Path
import re

from fix_susfs_priority_uid import c_code, guarded, protect, read_if_exists


HOOKS = {
    "susfs_add_sus_type": "KSU_SUSFS_HAS_ADD_SUS_TYPE",
    "susfs_add_sus_avd_allowed_by_type": "KSU_SUSFS_HAS_ADD_SUS_AVD_ALLOWED_BY_TYPE",
}
BEGIN = "/* BEGIN KSU SUSFS optional SELinux hooks */"
END = "/* END KSU SUSFS optional SELinux hooks */"


def has_global_implementation(source, name):
    pattern = rf"(?m)^[ \t]*((?:\w+[ \t]+)+){re.escape(name)}\s*\([^;{{}}]*\)\s*\{{"
    return any("static" not in match.group(1).split()
               for match in re.finditer(pattern, c_code(source)))


def fix(kernel_root):
    path = kernel_root / "drivers/kernelsu/selinux/sepolicy.c"
    if not path.is_file():
        print("SUSFS SELinux: no KernelSU sepolicy.c; no changes needed")
        return []
    original = path.read_text()
    references = re.compile(r"\b(?:" + "|".join(HOOKS) + r")\b")
    if not references.search(c_code(original)):
        print("SUSFS SELinux: KernelSU has no optional recording hooks; no changes needed")
        return []

    implementation = read_if_exists(kernel_root / "fs/susfs.c")
    available = {name: has_global_implementation(implementation, name) for name in HOOKS}
    managed = BEGIN in original or END in original
    if all(available.values()) and not managed:
        print("SUSFS SELinux: selected SUSFS implements both recording hooks; no changes needed")
        return []

    text = original
    if managed:
        block = re.compile(re.escape(BEGIN) + r"\n.*?" + re.escape(END) + r"\n", re.S)
        if original.count(BEGIN) != 1 or original.count(END) != 1 or not block.search(text):
            raise SystemExit("SUSFS SELinux: malformed compatibility block; no files written")
        text = block.sub("", text, count=1)

    # Each hook has an independent capability guard. Never replace missing hooks with stubs.
    for name, capability in HOOKS.items():
        pattern = (rf"^[ \t]*(?:extern[ \t]+int[ \t]+)?{name}\s*\([^;{{}}]*\)"
                   r"[ \t]*;[^\n]*\n")
        guard = f"#if defined(CONFIG_KSU_SUSFS) && defined({capability})"
        text = protect(text, pattern, guard, capability)

    # Validate all declarations and calls before changing the file.
    for match in references.finditer(c_code(text)):
        capability = HOOKS[match.group()]
        if not guarded(text, match.start(), capability):
            raise SystemExit(f"SUSFS SELinux: unsupported unguarded {match.group()} in {path}; "
                             "upstream layout changed, no files written")

    lines = [BEGIN]
    for name, capability in HOOKS.items():
        lines.append(f"#undef {capability}")
        if available[name]:
            lines.append(f"#define {capability} 1")
    lines.extend([END, ""])
    text = "\n".join(lines) + text
    if text == original:
        print("SUSFS SELinux: recording hook guards already match the selected SUSFS")
        return []

    path.write_text(text)
    missing = [name for name, provided in available.items() if not provided]
    if missing:
        print(f"SUSFS SELinux: guarded unsupported recording hooks in {path}: {', '.join(missing)}")
    else:
        print(f"SUSFS SELinux: re-enabled recording hooks implemented by the selected SUSFS in {path}")
    return [path]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path, help="actual kernel source root, e.g. kernel_workspace/common")
    args = parser.parse_args()
    fix(args.kernel_root.resolve())


if __name__ == "__main__":
    main()

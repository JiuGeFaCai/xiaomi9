#!/usr/bin/env python3
"""Adapt legacy KernelSU hook registration to the selected kernel's LSM API."""

import argparse
from pathlib import Path
import re

from fix_susfs_priority_uid import c_code, read_if_exists, without_comments


LSM_ID_SIGNATURE = re.compile(
    r"\bsecurity_add_hooks\s*\([^;{}]*,\s*const\s+struct\s+lsm_id\s*\*\s*(?:\w+\s*)?\)\s*;"
)
LEGACY_CALL = re.compile(
    r'\bsecurity_add_hooks\s*\(\s*ksu_hooks\s*,\s*ARRAY_SIZE\s*\(\s*ksu_hooks\s*\)'
    r'\s*,\s*(?P<name>"ksu")\s*\)\s*;'
)
HOOK_INIT = re.compile(
    r"(?m)^void\s+__init\s+ksu_lsm_hook_(?:built_in_)?init\s*\(\s*void\s*\)\s*\{"
)
LSM_ID = """static const struct lsm_id ksu_lsmid = {
    .name = "ksu",
    .id = LSM_ID_UNDEF,
};

"""
LSM_COUNT = re.compile(r"(?m)^#define[ \t]+MAX_LSM_COUNT[ \t]+\\\n[ \t]*COUNT_LSMS\([ \t]*\\\n")
KSU_LSM_COUNT = """/* KernelSU registers its hooks separately from the built-in LSM list. */
#if IS_BUILTIN(CONFIG_KSU)
#define KSU_LSM_ENABLED 1,
#else
#define KSU_LSM_ENABLED
#endif

"""


def fix(kernel_root):
    header = kernel_root / "include/linux/lsm_hooks.h"
    if not LSM_ID_SIGNATURE.search(c_code(read_if_exists(header))):
        print("KernelSU LSM: security_add_hooks does not require an lsm_id")
        return

    changes = {}
    for name in ("lsm_hook.c", "lsm_hooks.c"):
        path = kernel_root / "drivers/kernelsu/hook" / name
        original = read_if_exists(path)
        code = c_code(original)
        calls = list(LEGACY_CALL.finditer(code))
        if not calls:
            continue

        init = HOOK_INIT.search(code)
        if len(calls) != 1 or not init or init.end() > calls[0].start():
            raise SystemExit(f"KernelSU LSM: unsupported hook layout in {path}")
        start, end = calls[0].span("name")
        updated = original[:start] + "&ksu_lsmid" + original[end:]
        changes[path] = updated[:init.start()] + LSM_ID + updated[init.start():]

    if not changes:
        print("KernelSU LSM: no legacy hook registration to adapt")
        return

    count_header = kernel_root / "include/linux/lsm_count.h"
    if count_header.is_file():
        original = count_header.read_text()
        code = without_comments(original)
        if not re.search(r"\bCONFIG_KSU\b", code):
            count = LSM_COUNT.search(code)
            if not count:
                raise SystemExit(f"KernelSU LSM: unsupported MAX_LSM_COUNT layout in {count_header}")
            count_header.write_text(
                original[:count.start()] + KSU_LSM_COUNT + original[count.start():count.end()]
                + "\t\tKSU_LSM_ENABLED\t\t\\\n" + original[count.end():]
            )
            print(f"KernelSU LSM: reserved a registration slot in {count_header}")

    for path, updated in changes.items():
        path.write_text(updated)
        print(f"KernelSU LSM: adapted security_add_hooks in {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path)
    fix(parser.parse_args().kernel_root)

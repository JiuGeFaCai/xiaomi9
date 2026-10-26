#!/usr/bin/env python3
"""Find official KernelSU baselines in the selected SUSFS patch history."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


PATCH_PATH = "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"
HISTORY_LIMIT = 64
OFFICIAL_COMMIT = re.compile(
    r"https://github\.com/tiann/KernelSU/commit/([0-9a-f]{40})(?![0-9a-z])",
    re.IGNORECASE,
)
FALLBACK_REVISIONS = (
    "0a1f3ff71719fa2d6ec15f5568dac27fdb957fcc",
    "0ff54fab4e741d958daf3048d7711644c40eca79",
    "87db8315db2c9f5286e47b963ea02b46a88a7e56",
)


def find_candidates(susfs_dir):
    # HEAD respects a user-selected SUSFS tag or commit, even if its branch moved.
    try:
        result = subprocess.run(
            [
                "git", "log", "--topo-order", f"--max-count={HISTORY_LIMIT}",
                "--format=%B", "HEAD", "--", PATCH_PATH,
            ],
            cwd=susfs_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        history = result.stdout if result.returncode == 0 else ""
    except OSError:
        history = ""

    # Only official repository links with full commit IDs become candidates.
    # Deduplicate while keeping newer patch updates ahead of older references.
    discovered = list(dict.fromkeys(
        match.lower() for match in OFFICIAL_COMMIT.findall(history)
    ))
    if discovered:
        print(
            f"SUSFS 补丁历史提供 {len(discovered)} 个官方 KSU 候选提交，"
            f"优先检查 {discovered[0]}",
            file=sys.stderr,
        )
    else:
        print("SUSFS 补丁历史没有可用的官方 KSU 链接，检查已知回退提交", file=sys.stderr)

    return list(dict.fromkeys([*discovered, *FALLBACK_REVISIONS]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("susfs_dir", type=Path)
    args = parser.parse_args()
    # stdout is consumed by the workflow's candidate loop; diagnostics use stderr.
    for revision in find_candidates(args.susfs_dir):
        print(revision)

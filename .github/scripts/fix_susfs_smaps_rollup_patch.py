#!/usr/bin/env python3
"""Adapt legacy SUSFS smaps-rollup hunks to the newer three-argument walker."""

import argparse
import difflib
from pathlib import Path
import re


CONFIG = "CONFIG_KSU_SUSFS_SUS_MAP"
HIDDEN = "SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))"


def adapt_patch(kernel_dir, patch_path):
    source = (kernel_dir / "fs/proc/task_mmu.c").read_text()
    # Isolate the function, ignoring braces and calls in comments and strings.
    code = re.sub(
        r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
        lambda match: re.sub(r"[^\n]", " ", match[0]), source, flags=re.S,
    )
    signature = re.search(r"\bstatic\s+int\s+show_smaps_rollup\s*\([^;{}]*\)\s*\{", code)
    if signature is None:
        print("SUSFS smaps rollup: no show_smaps_rollup() to adapt")
        return False
    depth = 1
    end = signature.end()
    while end < len(code) and depth:
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    if depth:
        raise SystemExit("SUSFS smaps rollup: cannot locate the function end")
    calls = list(re.finditer(
        r"(?m)^([ \t]*)smap_gather_stats\(vma, &mss, (?:0|last_vma_end)\);[ \t]*$",
        code[signature.start():end],
    ))
    if not calls:
        print("SUSFS smaps rollup: legacy walker; no patch adaptation needed")
        return False

    lines = patch_path.read_text().splitlines(keepends=True)
    marker = "diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n"
    if marker not in lines:
        print("SUSFS smaps rollup: no task_mmu.c patch section")
        return False
    start = lines.index(marker)
    stop = next((i for i in range(start + 1, len(lines))
                 if lines[i].startswith("diff --git ")), len(lines))
    hunks = [i for i in range(start, stop) if lines[i].startswith("@@ ")]
    legacy = [(a, b) for a, b in zip(hunks, hunks[1:] + [stop])
              if "show_smaps_rollup(" in lines[a]]
    if not legacy or not any("smap_gather_stats(priv, vma, &mss);" in line
                             for a, b in legacy for line in lines[a:b]):
        print("SUSFS smaps rollup: patch already uses a compatible walker")
        return False
    additions = [line[1:].strip() for a, b in legacy
                 for line in lines[a + 1:b] if line.startswith("+")]
    expected = [f"#ifdef {CONFIG}", f"if (vma->vm_file && {HIDDEN})",
                "goto bypass_orig_flow;", f"#endif // #ifdef {CONFIG}", "",
                f"#ifdef {CONFIG}", "bypass_orig_flow:", f"#endif // #ifdef {CONFIG}"]
    if len(legacy) != 2 or additions != expected or len(calls) != 3:
        raise SystemExit("SUSFS smaps rollup: unrecognized walker or patch; no changes written")

    updated = source
    for match in reversed(calls):
        a, b = signature.start() + match.start(), signature.start() + match.end()
        indent = match[1]
        # Guard only statistics; VMA progress and lock handling stay unconditional.
        # Older SUSFS macros expand to an unparenthesized A && B && C chain.
        # Negate the entire expansion, rather than only its first operand.
        replacement = (f"#ifdef {CONFIG}\n"
                       f"{indent}if (!vma->vm_file || !({HIDDEN}))\n#endif\n"
                       f"{indent}\t{source[a + len(indent):b]}")
        updated = updated[:a] + replacement + updated[b:]
    replacement_hunks = list(difflib.unified_diff(
        source.splitlines(keepends=True), updated.splitlines(keepends=True),
    ))[2:]
    for index, (a, b) in reversed(list(enumerate(legacy))):
        lines[a:b] = replacement_hunks if index == 0 else []
    patch_path.write_text("".join(lines))
    print("SUSFS smaps rollup: replaced legacy goto hunks with guards on all three statistics calls")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_dir", type=Path)
    parser.add_argument("patch_path", type=Path)
    args = parser.parse_args()
    adapt_patch(args.kernel_dir, args.patch_path)

#!/usr/bin/env python3
"""Rebase SUSFS task_mmu hooks onto the current three-argument smaps walker."""

import argparse
import difflib
from pathlib import Path
import re
import subprocess
import tempfile


CONFIG = "CONFIG_KSU_SUSFS_SUS_MAP"
HIDDEN = "SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))"
TASK_MMU = "fs/proc/task_mmu.c"
SECTION = f"diff --git a/{TASK_MMU} b/{TASK_MMU}\n"
SMAP_GUARD = [
    f"#ifdef {CONFIG}\n",
    "\tif (vma->vm_file) {\n",
    f"\t\tif ({HIDDEN})\n",
    "\t\t\treturn 0;\n",
    "\t}\n",
    f"#endif // #ifdef {CONFIG}\n",
    "\n",
]


def code_only(source):
    return re.sub(
        r"""/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""",
        lambda match: re.sub(r"[^\n]", " ", match[0]), source, flags=re.S,
    )


def function_bounds(source, name):
    code = code_only(source)
    signature = re.search(rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{", code)
    if signature is None:
        return None
    depth = 1
    end = signature.end()
    while end < len(code) and depth:
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    if depth:
        raise SystemExit(f"SUSFS task_mmu: cannot locate the end of {name}()")
    return signature.start(), end, code[signature.start():end]


def has_modern_walker(bounds):
    return bounds is not None and re.search(
        r"\bsmap_gather_stats\s*\(\s*vma\s*,", bounds[2],
    ) is not None


def has_legacy_walker(bounds):
    return bounds is not None and re.search(
        r"\bsmap_gather_stats\s*\(\s*priv\s*,\s*vma\s*,", bounds[2],
    ) is not None


def patch_section(lines):
    if SECTION not in lines:
        return None
    if lines.count(SECTION) != 1:
        raise SystemExit("SUSFS task_mmu: duplicate file sections in upstream patch")
    start = lines.index(SECTION)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("diff --git ")), len(lines))
    hunks = [i for i in range(start, end) if lines[i].startswith("@@ ")]
    return start, end, list(zip(hunks, hunks[1:] + [end]))


def adapt_mm_hook_context(source, lines):
    """Drop an absent vendor include only where it anchors the SUSFS include."""
    if re.search(r"(?m)^\s*#\s*include\s*<trace/hooks/mm\.h>", code_only(source)):
        return lines, False
    section = patch_section(lines)
    if section is None:
        return lines, False
    updated = list(lines)
    changed = False
    context = " #include <trace/hooks/mm.h>\n"
    for start, end in reversed(section[2]):
        body = lines[start + 1:end]
        if context not in body or "+#include <linux/susfs_def.h>\n" not in body:
            continue
        header = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)",
            lines[start].rstrip("\n"),
        )
        if header is None:
            raise SystemExit("SUSFS task_mmu: invalid mm hook include hunk; no changes written")
        kept = [line for line in body if line != context]
        old_count = sum(line.startswith((" ", "-")) for line in kept)
        new_count = sum(line.startswith((" ", "+")) for line in kept)
        updated[start:end] = [
            f"@@ -{header[1]},{old_count} +{header[3]},{new_count} @@{header[5]}\n",
            *kept,
        ]
        changed = True
    return updated, changed


def rebase_hunk(source, header, body):
    before = "".join(line[1:] for line in body if line.startswith((" ", "-")))
    after = "".join(line[1:] for line in body if line.startswith((" ", "+")))
    if not before:
        raise SystemExit("SUSFS task_mmu: context-free hunk cannot be rebased safely")
    scope = re.search(r"\b(show_map_vma|pagemap_read)\s*\(", header)
    bounds = function_bounds(source, scope[1]) if scope else None
    if scope and bounds is None:
        raise SystemExit(f"SUSFS task_mmu: missing {scope[1]}()")
    start, end = bounds[:2] if bounds else (0, len(source))
    matches = [start + match.start() for match in re.finditer(
        re.escape(before), source[start:end],
    )]
    if len(matches) != 1:
        raise SystemExit(
            f"SUSFS task_mmu: expected one exact context match for {header.strip()}, "
            f"found {len(matches)}; no changes written"
        )
    return matches[0], matches[0] + len(before), after


def smap_guard_edit(source, body):
    additions = [line[1:] for line in body if line.startswith("+")]
    if additions != SMAP_GUARD:
        raise SystemExit("SUSFS task_mmu: unrecognized show_smap hook; no changes written")
    bounds = function_bounds(source, "show_smap")
    if bounds is None or not re.search(r"\bstruct\s+vm_area_struct\s*\*\s*vma\s*=", bounds[2]):
        raise SystemExit("SUSFS task_mmu: show_smap() has no local VMA declaration")
    anchor = re.search(
        r"(?m)^([ \t]*)if\s*\(\s*!vma_(?:data_)?pages\(vma\)\s*\)[ \t]*$", bounds[2],
    )
    if anchor is None:
        raise SystemExit("SUSFS task_mmu: cannot locate show_smap padding check")
    position = bounds[0] + anchor.start()
    return position, position, "".join(additions)


def adapt_patch(kernel_dir, patch_path):
    source = (kernel_dir / TASK_MMU).read_text()
    bounds = function_bounds(source, "show_smaps_rollup")
    if bounds is None:
        print("SUSFS smaps rollup: no show_smaps_rollup() to adapt")
        return False
    calls = list(re.finditer(
        r"(?m)^([ \t]*)smap_gather_stats\(vma, &mss, (?:0|last_vma_end)\);[ \t]*$",
        bounds[2],
    ))
    if not calls:
        print("SUSFS smaps rollup: legacy walker; no patch adaptation needed")
        return False

    lines, context_changed = adapt_mm_hook_context(
        source, patch_path.read_text().splitlines(keepends=True),
    )
    section = patch_section(lines)
    if section is None:
        print("SUSFS smaps rollup: no task_mmu.c patch section")
        return False
    start, stop, hunks = section
    legacy = [(a, b) for a, b in hunks
              if "show_smaps_rollup(" in lines[a]]
    if not legacy or not any("smap_gather_stats(priv, vma, &mss);" in line
                             for a, b in legacy for line in lines[a:b]):
        print("SUSFS smaps rollup: no legacy walker hunks to rebase")
        return False
    additions = [line[1:].strip() for a, b in legacy
                 for line in lines[a + 1:b] if line.startswith("+")]
    expected = [f"#ifdef {CONFIG}", f"if (vma->vm_file && {HIDDEN})",
                "goto bypass_orig_flow;", f"#endif // #ifdef {CONFIG}", "",
                f"#ifdef {CONFIG}", "bypass_orig_flow:", f"#endif // #ifdef {CONFIG}"]
    if len(legacy) != 2 or additions != expected or len(calls) != 3:
        raise SystemExit("SUSFS smaps rollup: unrecognized walker or patch; no changes written")

    edits = []
    for a, b in hunks:
        if (a, b) in legacy:
            continue
        if re.search(r"\bshow_smap\s*\(", lines[a]):
            edits.append(smap_guard_edit(source, lines[a + 1:b]))
        else:
            edits.append(rebase_hunk(source, lines[a], lines[a + 1:b]))
    for match in calls:
        a, b = bounds[0] + match.start(), bounds[0] + match.end()
        indent = match[1]
        # Guard only statistics; VMA progress and lock handling stay unconditional.
        # Older SUSFS macros expand to an unparenthesized A && B && C chain.
        # Negate the entire expansion, rather than only its first operand.
        replacement = (f"#ifdef {CONFIG}\n"
                       f"{indent}if (!vma->vm_file || !({HIDDEN}))\n#endif\n"
                       f"{indent}\t{source[a + len(indent):b]}")
        edits.append((a, b, replacement))
    edits.sort()
    if any(left[1] > right[0] for left, right in zip(edits, edits[1:])):
        raise SystemExit("SUSFS task_mmu: overlapping edits; no changes written")
    updated = source
    for a, b, replacement in reversed(edits):
        updated = updated[:a] + replacement + updated[b:]
    # Regenerate this entire file in source order. Mixing new rollup line
    # numbers with old neighboring hunks allowed patch -F3 to hit another
    # function and then reject the rollup changes as misordered.
    replacement_section = [SECTION, *difflib.unified_diff(
        source.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{TASK_MMU}", tofile=f"b/{TASK_MMU}", n=5,
    )]
    verify_source(updated)
    lines[start:stop] = replacement_section
    patch_path.write_text("".join(lines))
    if context_changed:
        print("SUSFS task_mmu: removed absent mm hook include context")
    print("SUSFS task_mmu: rebased all file hunks; guarded all three rollup statistics calls")
    return True


def compact_body(bounds):
    # Validate the SUS_MAP-enabled path, excluding its disabled #else branches.
    # Keep unrelated conditional branches so every possible call is checked.
    lines = []
    stack = []
    active = True
    for line in bounds[2].splitlines(keepends=True):
        directive = re.match(r"^[ \t]*#(if|ifdef|ifndef|elif|else|endif)\b(.*)", line)
        if directive is None:
            if active:
                lines.append(line)
            continue
        kind, expression = directive.groups()
        condition = None
        expression = re.sub(r"\s+", "", expression)
        if CONFIG in expression:
            if kind in ("ifdef", "ifndef") and expression == CONFIG:
                condition = kind == "ifdef"
            elif kind in ("if", "elif") and expression in (
                CONFIG, f"defined({CONFIG})", f"defined{CONFIG}",
            ):
                condition = True
            elif kind in ("if", "elif") and expression in (
                f"!defined({CONFIG})", f"!defined{CONFIG}",
            ):
                condition = False
            else:
                raise SystemExit("SUSFS task_mmu: unrecognized SUS_MAP condition; stopping for review")
        if kind in ("if", "ifdef", "ifndef"):
            stack.append((active, condition))
            active = active and condition is not False
        elif kind in ("else", "elif"):
            parent, taken = stack[-1]
            active = parent and taken is not True and (kind == "else" or condition is not False)
            stack[-1] = (parent, taken if kind == "else" or taken is True else condition)
        else:
            active, _ = stack.pop()
    return re.sub(r"\s+", "", "".join(lines))


def rollup_call_guarded(body, call):
    hidden = re.escape(HIDDEN)
    before = body[:call.start()]
    if re.search(rf"if\(!vma->vm_file\|\|!\({hidden}\)\)\{{?$", before):
        return True
    bypass = re.search(
        rf"if\(vma->vm_file&&\(?{hidden}\)?\)goto(?P<label>\w+);$", before,
    )
    # The author's 5.15 patch jumps over exactly one statistics call. The
    # progress update and lock handling must remain outside that skipped span.
    return bypass is not None and re.match(
        rf"smap_gather_stats\(vma,&mss,(?:0|last_vma_end)\);{re.escape(bypass['label'])}:",
        body[call.start():],
    ) is not None


def verify_source(source):
    opened = function_bounds(source, "smaps_rollup_open")
    if opened is not None and re.search(r"\bvma\b", opened[2]) and not re.search(
        r"\bstruct\s+vm_area_struct\s*\*\s*vma\b", opened[2],
    ):
        raise SystemExit("SUSFS task_mmu: VMA hook leaked into smaps_rollup_open()")
    bounds = function_bounds(source, "show_smaps_rollup")
    if bounds is None or has_legacy_walker(bounds):
        return
    if not has_modern_walker(bounds):
        raise SystemExit("SUSFS task_mmu: unknown upstream statistics walker; stopping for review")
    hidden = re.escape(HIDDEN)
    # New upstream patches may filter centrally in smap_gather_stats(), instead
    # of guarding its call sites. Accept that form without injecting duplicates.
    helper = function_bounds(source, "smap_gather_stats")
    hidden_return = re.compile(
        rf"if\(vma->vm_file&&\(?{hidden}\)?\)\{{?return;\}}?",
    )
    helper_guarded = helper is not None and hidden_return.search(compact_body(helper)) is not None
    body = compact_body(bounds)
    calls = list(re.finditer(r"\bsmap_gather_stats\(vma,", body))
    if not helper_guarded and (not calls or any(
        not rollup_call_guarded(body, call) for call in calls
    )):
        raise SystemExit("SUSFS task_mmu: upstream rollup hooks are missing or unrecognized; no fuzzy fallback")
    smap = function_bounds(source, "show_smap")
    if smap is not None:
        body = compact_body(smap)
        nested = re.search(rf"if\(vma->vm_file\)\{{if\({hidden}\)return0;\}}", body)
        combined = re.search(rf"if\(vma->vm_file&&\(?{hidden}\)?\)\{{?return0;\}}?", body)
        if nested is None and combined is None:
            raise SystemExit("SUSFS task_mmu: show_smap hook is missing or unrecognized")


def verify_hooks(kernel_dir):
    path = kernel_dir / TASK_MMU
    rejects = path.with_suffix(path.suffix + ".rej")
    if rejects.exists() and rejects.read_text().strip():
        raise SystemExit("SUSFS task_mmu: patch has rejected hunks; stopping before compilation")
    verify_source(path.read_text())
    print("SUSFS task_mmu: hook placement verified; no rejected hunks")


def preview_section(source, section, reverse=False):
    with tempfile.TemporaryDirectory(prefix="susfs-task-mmu-preview-") as tmp:
        root = Path(tmp)
        target = root / TASK_MMU
        target.parent.mkdir(parents=True)
        target.write_text(source)
        patch = root / "task_mmu.patch"
        patch.write_text(section)
        command = ["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch),
                   "--reverse" if reverse else "--forward"]
        result = subprocess.run(command, cwd=root, text=True, capture_output=True)
        return target.read_text() if result.returncode == 0 else None


def apply_rebased_patch(kernel_dir, patch_path, section=None):
    lines = patch_path.read_text().splitlines(keepends=True)
    start, stop, _ = patch_section(lines)
    with tempfile.TemporaryDirectory(prefix="susfs-task-mmu-") as tmp:
        isolated = Path(tmp) / "task_mmu.patch"
        isolated.write_text(section if section is not None else "".join(lines[start:stop]))
        command = ["patch", "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch",
                   "-p1", "-i", str(isolated)]
        for dry_run in (True, False):
            result = subprocess.run(command + (["--dry-run"] if dry_run else []),
                                    cwd=kernel_dir, text=True, capture_output=True)
            if result.returncode:
                raise SystemExit("SUSFS task_mmu: exact patch application failed:\n" +
                                 result.stdout + result.stderr)
        verify_hooks(kernel_dir)
    # The remaining files retain their upstream patch; task_mmu.c has already
    # been applied exactly and must not pass through the workflow's fuzzy patch.
    patch_path.write_text("".join(lines[:start] + lines[stop:]))
    print("SUSFS task_mmu: applied separately with fuzz=0")


def prepare_and_apply(kernel_dir, patch_path):
    lines = patch_path.read_text().splitlines(keepends=True)
    section = patch_section(lines)
    if section is None:
        print("SUSFS task_mmu: no upstream file section; nothing to apply")
        return
    source = (kernel_dir / TASK_MMU).read_text()
    bounds = function_bounds(source, "show_smaps_rollup")
    if bounds is None or has_legacy_walker(bounds):
        print("SUSFS task_mmu: legacy walker; retaining the upstream patch")
        return
    if not has_modern_walker(bounds):
        raise SystemExit("SUSFS task_mmu: unknown upstream statistics walker; no changes written")
    start, stop, _ = section
    isolated_lines, context_changed = adapt_mm_hook_context(source, lines[start:stop])
    isolated = "".join(isolated_lines)
    candidate = preview_section(source, isolated)
    if candidate is not None:
        # Prefer an updated author's patch when it already fits this source.
        # Validate its hooks, then apply it verbatim with zero fuzz.
        verify_source(candidate)
        if context_changed:
            print("SUSFS task_mmu: removed absent mm hook include context; retaining upstream hooks")
        else:
            print("SUSFS task_mmu: upstream patch already fits; applying without adaptation")
        apply_rebased_patch(kernel_dir, patch_path, isolated)
        return
    if preview_section(source, isolated, reverse=True) is not None:
        verify_source(source)
        patch_path.write_text("".join(lines[:start] + lines[stop:]))
        print("SUSFS task_mmu: upstream hooks already applied; skipping duplicate application")
        return
    helper = function_bounds(source, "smap_gather_stats")
    if HIDDEN in bounds[2] or (helper is not None and HIDDEN in helper[2]):
        raise SystemExit("SUSFS task_mmu: source already has hooks but this patch differs; refusing duplicate insertion")
    if not adapt_patch(kernel_dir, patch_path):
        raise SystemExit("SUSFS task_mmu: updated upstream patch does not fit; stopping before modifications")
    apply_rebased_patch(kernel_dir, patch_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_dir", type=Path)
    parser.add_argument("patch_path", type=Path, nargs="?")
    parser.add_argument("--apply", action="store_true", help="apply rebased task_mmu hunks with fuzz=0")
    parser.add_argument("--verify", action="store_true", help="check hooks after patching")
    args = parser.parse_args()
    if args.verify:
        verify_hooks(args.kernel_dir)
    elif args.patch_path is None:
        parser.error("patch_path is required unless --verify is used")
    elif args.apply:
        prepare_and_apply(args.kernel_dir, args.patch_path)
    else:
        adapt_patch(args.kernel_dir, args.patch_path)

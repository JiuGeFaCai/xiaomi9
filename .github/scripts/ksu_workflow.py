#!/usr/bin/env python3
"""Shared KernelSU setup and configuration for the four build workflows."""

import argparse
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


# Match the ksu_type sources used by oppo_oplus_realme_sm8750 for the new variants.
VARIANTS = {
    "SukiSU": ("SukiSU-Ultra/SukiSU-Ultra", "main", "KernelSU", "SukiSU"),
    "BakaSU": ("Baka-SU/BakaSU", "main", "KernelSU", "BakaSU"),
    "ksunext": ("pershoot/KernelSU-Next", "dev-susfs", "KernelSU-Next", "KSUNext"),
    "ksu": ("tiann/KernelSU", "main", "KernelSU", "KSU"),
}
# Official revisions matching the GKI and OKI SUSFS patch bases, newest first.
# Only considered for an automatic ref, after the requested patch rejects main.
SUSFS_COMPAT_REVISIONS = (
    "0ff54fab4e741d958daf3048d7711644c40eca79",
    "87db8315db2c9f5286e47b963ea02b46a88a7e56",
)
SUSFS_OPTIONS = (
    "KSU_SUSFS", "KSU_SUSFS_SUS_PATH", "KSU_SUSFS_SUS_MAP",
    "KSU_SUSFS_SUS_MOUNT", "KSU_SUSFS_SUS_KSTAT", "KSU_SUSFS_SPOOF_UNAME",
    "KSU_SUSFS_ENABLE_LOG", "KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS",
    "KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG", "KSU_SUSFS_OPEN_REDIRECT",
)


def emit(**values):
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            value = str(value)
            if "\n" in value or "\r" in value:
                raise ValueError(f"{key} 不能包含换行")
            stream.write(f"{key}={value}\n")


def git(source, *args, optional=False):
    result = subprocess.run(
        ["git", "-C", str(source), *args], text=True, capture_output=True,
        check=not optional,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def has_config(source, symbol):
    pattern = re.compile(r"^\s*(?:menu)?config\s+" + re.escape(symbol) + r"\s*$", re.M)
    return any(
        pattern.search(path.read_text())
        for path in (source / "kernel").rglob("Kconfig*") if path.is_file()
    )


def resolve():
    variant = os.environ.get("KSU_VARIANT_INPUT", "BakaSU").strip()
    if variant == "无":  # Accept the previous spelling used by 4.yml.
        variant = "none"
    if variant not in VARIANTS and variant != "none":
        raise ValueError(f"未知 KernelSU 变体: {variant}")
    kpm = os.environ.get("KSU_KPM_INPUT", "false")
    if kpm not in {"false", "builtin", "kpn"}:
        raise ValueError(f"未知 KPM 模式: {kpm}")
    if variant == "none" and kpm == "builtin":
        raise ValueError("none 不能使用 builtin KPM，请选择 false 或独立的 kpn")
    susfs = os.environ.get("KSU_SUSFS_INPUT", "true").lower()
    if susfs not in {"true", "false"}:
        raise ValueError(f"无效的 SUSFS 开关: {susfs}")
    ref = os.environ.get("KSU_REF_INPUT", "").strip()
    auto_ref = not ref and variant != "none"
    override = "".join(os.environ.get("KSU_VERSION_OVERRIDE_INPUT", "").split())
    if variant != "none":
        ref = ref or VARIANTS[variant][1]
        if ref.startswith("-") or any(char.isspace() for char in ref):
            raise ValueError("KSU 分支/标签/提交不能以 '-' 开头或包含空白")
        if override and not re.fullmatch(r"[0-9]+", override):
            raise ValueError("ksu_version_override 必须是纯数字")
    else:
        ref, override, susfs = "", "", "false"
        print("none 模式：跳过 KernelSU 和 SUSFS，独立 kpn 仍可选")
    emit(
        KSU_VARIANT=variant,
        KSU_ENABLED=str(variant != "none").lower(),
        KSU_SUSFS_ENABLED=susfs,
        KSU_KPM_MODE=kpm,
        KSU_REF=ref,
        KSU_REF_AUTO=str(auto_ref).lower(),
        KSU_VERSION_OVERRIDE=override,
        KSUVARIANT=VARIANTS[variant][3] if variant != "none" else "NoKSU",
    )


def setup(root):
    variant = os.environ["KSU_VARIANT"]
    if variant == "none":
        return
    repo, setup_ref, directory, _ = VARIANTS[variant]
    ref = os.environ["KSU_REF"]
    url = f"https://raw.githubusercontent.com/{repo}/{setup_ref}/kernel/setup.sh"
    print(f"添加 {variant}: {repo}@{ref}", flush=True)
    script = subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error", "--retry", "3", url],
        check=True, capture_output=True,
    ).stdout
    subprocess.run(["bash", "-s", "--", ref], input=script, cwd=root, check=True)
    source = (root / directory).resolve()
    # Upstream setup scripts may silently fall back after a failed checkout.
    selected = git(source, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if selected != git(source, "rev-parse", "HEAD"):
        raise ValueError(f"{variant} 未切换到指定的 {ref}，停止构建")
    if os.environ["KSU_KPM_MODE"] == "builtin" and not has_config(source, "KPM"):
        raise ValueError(f"{variant}@{ref} 没有内置 KPM，请选择 false 或独立的 kpn")
    emit(KSU_SOURCE_DIR=source)


def apply_susfs(source, susfs_dir):
    if has_config(source, "KSU_SUSFS"):
        print("所选 KSU 源码已包含 SUSFS 配置，跳过重复打补丁")
        return
    patch = (susfs_dir / "kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch").resolve()
    if not patch.is_file():
        raise ValueError(f"缺少官方 KernelSU 的 SUSFS 补丁: {patch}")
    command = ["patch", "--batch", "--forward", "-p1", "--fuzz=0", "-i", str(patch)]
    # Check every hunk before changing the tree. Never compile a partially applied KSU patch.
    def check(directory):
        return subprocess.run(command + ["--dry-run"], cwd=directory, capture_output=True, text=True)

    result = check(source)
    if result.returncode and os.environ.get("KSU_REF_AUTO") == "true":
        if git(source, "status", "--porcelain"):
            raise ValueError("KSU 源码已有修改，不能自动切换 SUSFS 兼容提交")
        for revision in SUSFS_COMPAT_REVISIONS:
            if git(source, "cat-file", "-t", revision, optional=True) != "commit":
                continue
            archive = subprocess.run(
                ["git", "-C", str(source), "archive", revision, "kernel"],
                capture_output=True, check=True,
            ).stdout
            with tempfile.TemporaryDirectory(prefix="ksu-susfs-check-") as temporary:
                candidate = Path(temporary)
                with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                    for member in bundle.getmembers():
                        if not member.isfile():
                            continue
                        relative = Path(member.name)
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError("KSU archive contains an invalid path")
                        target = candidate / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(bundle.extractfile(member).read())
                if check(candidate).returncode:
                    continue
            subprocess.run(["git", "-C", str(source), "checkout", "--detach", revision], check=True)
            result = check(source)
            if result.returncode == 0:
                print(f"main 与所选 SUSFS 补丁不匹配，自动使用已通过完整预检的官方提交 {revision}")
                os.environ["KSU_REF"] = revision
                emit(KSU_REF=revision)
            break
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        raise ValueError("官方 KSU 与所选 SUSFS 补丁不匹配；请指定兼容提交，或关闭 SUSFS")
    subprocess.run(command, cwd=source, check=True)


def version_metadata(source, variant):
    base, offset = "HEAD", 30000
    if variant == "SukiSU":
        base, offset = "main", 37185
    elif variant == "BakaSU":
        offset = 30700
    elif variant == "ksunext":
        # Mirror pershoot's Kbuild: dev-susfs uses its merge-base with origin/dev.
        branch = git(source, "rev-parse", "--abbrev-ref", "HEAD").split("-", 1)[0]
        base = (
            git(source, "merge-base", "HEAD", f"refs/remotes/origin/{branch}", optional=True)
            or git(source, "merge-base", "HEAD", "refs/remotes/origin/main", optional=True)
            or "HEAD"
        )
    version = int(git(source, "rev-list", "--count", base)) + offset
    tag = git(source, "describe", "--tags", "--abbrev=0", base, optional=True)
    return version, tag


def override_version(source, version, tag=""):
    matched = False
    for path in (source / "kernel/Kbuild", source / "kernel/Makefile"):
        if not path.is_file():
            continue
        text = path.read_text()
        # Support SukiSU/BakaSU's := and official/Next's $(eval KSU_VERSION=...).
        for pattern in (
            r"(?m)^([ \t]*)KSU_VERSION\s*:?=.*$",
            r"(?m)^([ \t]*)\$\(eval\s+KSU_VERSION\s*=.*$",
        ):
            text, count = re.subn(pattern, lambda m: f"{m[1]}KSU_VERSION := {version}", text)
            matched = matched or count > 0
        text = re.sub(r"(?m)^KSU_VERSION_FALLBACK\s*:=.*$", f"KSU_VERSION_FALLBACK := {version}", text)
        text = re.sub(r"-DKSU_VERSION=[0-9]+\b", f"-DKSU_VERSION={version}", text)
        if tag and re.fullmatch(r"[A-Za-z0-9._/+\-]+", tag):
            text = re.sub(r"(?m)^KSU_VERSION_TAG_FALLBACK\s*:=.*$", f"KSU_VERSION_TAG_FALLBACK := {tag}", text)
        path.write_text(text)
    if not matched:
        raise ValueError("无法识别上游 KSU_VERSION 定义，停止构建以免内核与产物版本号不一致")


def finalize(susfs_dir):
    variant = os.environ["KSU_VARIANT"]
    version = 0
    if variant != "none":
        source = Path(os.environ["KSU_SOURCE_DIR"])
        if os.environ["KSU_SUSFS_ENABLED"] == "true":
            if variant == "ksu":
                apply_susfs(source, susfs_dir)
            if not has_config(source, "KSU_SUSFS"):
                raise ValueError(f"{variant}@{os.environ['KSU_REF']} 未提供 SUSFS 支持，请关闭 SUSFS 或选择兼容分支")
        version, tag = version_metadata(source, variant)
        override = os.environ.get("KSU_VERSION_OVERRIDE", "")
        if override:
            version = int(override)
        if override or variant in {"ksu", "ksunext"}:
            # Also fill the fallback used when Bazel cannot see the repository's .git.
            override_version(source, version, tag if variant == "ksunext" else "")
    suffix = os.environ.get("KSU_ARTIFACT_SUFFIX", "")
    emit(KSUVER=f"{version}_{suffix}" if suffix else version)
    print(f"KernelSU 变体: {variant}, 版本号: {version}")


def configure(path):
    enabled = os.environ["KSU_ENABLED"] == "true"
    susfs = enabled and os.environ["KSU_SUSFS_ENABLED"] == "true"
    builtin = enabled and os.environ["KSU_KPM_MODE"] == "builtin"
    lines = []
    for line in path.read_text().splitlines():
        match = re.match(r"(?:# )?CONFIG_([A-Z0-9_]+)(?:=| is not set)", line.strip())
        symbol = match[1] if match else ""
        managed = symbol in {"KSU", "KPM", *SUSFS_OPTIONS}
        if not susfs and symbol.startswith("KSU_SUSFS"):
            managed = True
        if not enabled and symbol.startswith("KSU_"):
            managed = True
        if not managed:
            lines.append(line)
    lines.append("CONFIG_KSU=y" if enabled else "# CONFIG_KSU is not set")
    lines.append("CONFIG_KPM=y" if builtin else "# CONFIG_KPM is not set")
    if susfs:
        lines.extend(f"CONFIG_{option}=y" for option in SUSFS_OPTIONS)
    else:
        lines.append("# CONFIG_KSU_SUSFS is not set")
    path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "setup", "finalize", "config"))
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command != "resolve" and args.path is None:
        parser.error(f"{args.command} requires a path")
    try:
        if args.command == "resolve":
            resolve()
        elif args.command == "setup":
            setup(args.path.resolve())
        elif args.command == "finalize":
            finalize(args.path.resolve())
        else:
            configure(args.path)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

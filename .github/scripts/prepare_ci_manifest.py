#!/usr/bin/env python3
"""Prepare a Google CI snapshot for sync after its branch names have moved."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET


COMMIT_ID = re.compile(r"[0-9a-fA-F]{40}\Z")


def fetch_refs(url, refs):
    """Read only the candidate refs, retrying transient Git transport failures."""
    command = [
        "git", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=20",
        "ls-remote", "--refs", url, *refs,
    ]
    error = ""
    for _ in range(2):
        try:
            result = subprocess.run(
                command, text=True, capture_output=True, timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
            continue
        if result.returncode == 0:
            return {
                line.split()[1] for line in result.stdout.splitlines()
                if len(line.split()) == 2
            }
        error = result.stderr.strip()
    raise RuntimeError(f"无法核对 {url} 的分支：{error[:500]}")


def ref_candidates(value):
    if value.startswith("refs/"):
        ref = value
    else:
        ref = "refs/heads/" + value
    candidates = [ref]
    prefix = "refs/heads/"
    if ref.startswith(prefix) and not ref.startswith(prefix + "deprecated/"):
        candidates.append(prefix + "deprecated/" + ref[len(prefix):])
    return candidates


def prepare_manifest(source, output, lookup=fetch_refs):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.parse(source, parser=parser)
    root = tree.getroot()
    if root.tag != "manifest" or not root.findall("project"):
        raise ValueError("CI 文件不是有效的 repo 源码清单")
    if next(root.iter("include"), None) is not None or next(root.iter("submanifest"), None) is not None:
        raise ValueError("CI 清单必须是展开且锁定提交的完整快照")

    default = root.find("default")
    defaults = dict(default.attrib) if default is not None else {}
    remotes = {remote.get("name"): remote for remote in root.findall("remote")}
    projects = []

    def collect(parent, parent_name=""):
        for project in parent.findall("project"):
            name = project.get("name", "")
            if not name:
                raise ValueError("CI 清单中的 project 缺少 name")
            full_name = parent_name + "/" + name if parent_name else name
            remote_name = project.get("remote") or defaults.get("remote")
            remote = remotes.get(remote_name)
            if remote is None or not remote.get("fetch"):
                raise ValueError(f"{full_name}: 缺少有效的下载 remote")
            revision = (
                project.get("revision") or remote.get("revision")
                or defaults.get("revision", "")
            )
            if not COMMIT_ID.fullmatch(revision):
                raise ValueError(f"{full_name}: revision 没有锁定完整提交：{revision!r}")
            projects.append((project, full_name, remote, revision))
            collect(project, full_name)

    # Validate the whole snapshot before contacting remotes or writing output.
    collect(root)
    for remote in remotes.values():
        fetch = remote.get("fetch", "")
        parts = urlsplit(fetch)
        if parts.hostname == "android-review.googlesource.com":
            remote.set("fetch", urlunsplit((
                "https", "android.googlesource.com", parts.path, "", "",
            )))
            print("旧 CI 清单下载地址：android-review.googlesource.com -> android.googlesource.com")

    cache = {}

    def resolve(url, value):
        key = (url, value)
        if key not in cache:
            candidates = ref_candidates(value)
            try:
                available = lookup(url, candidates)
            except RuntimeError as exc:
                print(f"{exc}；跳过分支提示，直接获取固定提交", file=sys.stderr)
                available = set()
            ref = next((ref for ref in candidates if ref in available), None)
            if ref and not value.startswith("refs/"):
                ref = ref.removeprefix("refs/heads/")
            cache[key] = ref
        return cache[key]

    for project, name, remote, revision in projects:
        # Explicit revisions also prevent remote/default branch inheritance.
        project.set("revision", revision)
        url = remote.get("fetch").rstrip("/") + "/" + name
        for attribute in ("upstream", "dest-branch"):
            value = project.get(attribute) or defaults.get(attribute)
            if not value:
                continue
            replacement = None if COMMIT_ID.fullmatch(value) else resolve(url, value)
            if replacement:
                project.set(attribute, replacement)
                if replacement != value:
                    print(f"{name}: {attribute} {value} -> {replacement}")
            else:
                project.attrib.pop(attribute, None)
                print(f"{name}: 移除失效的 {attribute}={value}，使用固定提交 {revision}")

    if default is not None:
        # Repo 2.73 can use default.revision as an implicit upstream, even for
        # SHA-pinned projects. A global branch need not exist in every repo.
        for attribute in ("revision", "upstream", "dest-branch"):
            default.attrib.pop(attribute, None)

    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)
    print(f"CI 同步清单已准备好：{len(projects)} 个仓库均锁定原始提交")
    return {name: revision for _, name, _, revision in projects}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        prepare_manifest(args.source, args.output)
    except (OSError, ValueError, ET.ParseError) as exc:
        parser.exit(1, f"CI manifest 准备失败：{exc}\n")

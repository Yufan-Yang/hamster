#!/usr/bin/env python3
"""Exact deployment snapshots and read-only release verification (stdlib only)."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SHIP = ("grabber.py", "index.html", "refresh_metadata.py", "requirements-pi.txt",
        "shiguang", "tests", "pi", "static")
SNAPSHOT = (*SHIP, "deploy.sh", "mac", "HEALING.md", "README.md")


def git(*args, root=ROOT, env=None):
    return subprocess.check_output(["git", *args], cwd=root, env=env)


def pack(path, root=ROOT):
    """Save dirty code without changing HEAD, the user's index or working files."""
    with tempfile.TemporaryDirectory(prefix="shiguang-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        parent = git("rev-parse", "HEAD", root=root).decode().strip()
        git("read-tree", parent, root=root, env=env)
        paths = [p for p in SNAPSHOT if (root / p).exists() or
                 git("ls-files", "--", p, root=root, env=env).strip()]
        git("add", "-A", "--", *paths, root=root, env=env)
        tree = git("write-tree", root=root, env=env).decode().strip()
        old_tree = git("rev-parse", f"{parent}^{{tree}}", root=root).decode().strip()
        revision = parent if tree == old_tree else git(
            "commit-tree", tree, "-p", parent, "-m", "Deployment source snapshot",
            root=root, env=env).decode().strip()
    git("update-ref", f"refs/shiguang/releases/{revision}", revision, root=root)
    manifest = {"revision": revision, "created": time.time(), "files": {}}
    with tarfile.open(path, "w:gz") as archive:
        listing = git("ls-tree", "-rz", revision, "--", *SHIP, root=root)
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, name = entry.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError(f"Release contains a non-regular file: {name!r}")
            name = name.decode()
            data = git("cat-file", "blob", oid.decode(), root=root)
            add_file(archive, name, data, 0o755 if mode == b"100755" else 0o644)
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        shortcut = root / "send-to-pi.shortcut"
        if shortcut.is_file():
            data = shortcut.read_bytes()
            add_file(archive, shortcut.name, data)
            manifest["files"][shortcut.name] = hashlib.sha256(data).hexdigest()
        add_file(archive, "REVISION", (revision + "\n").encode())
        add_file(archive, "RELEASE.json", json.dumps(manifest, sort_keys=True).encode())
    return revision


def add_file(archive, name, data, mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.size, entry.mode = len(data), mode
    entry.mtime = int(time.time())
    archive.addfile(entry, io.BytesIO(data))


def verify(root):
    manifest = json.loads((root / "RELEASE.json").read_text())
    if (root / "REVISION").read_text().strip() != manifest["revision"]:
        raise ValueError("Release revision does not match its manifest")
    for name, expected in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Invalid release path: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Release file mismatch: {name}")
    return manifest


def health(root, since):
    manifest = verify(root)
    def fetch(path):
        with urllib.request.urlopen("http://127.0.0.1:8088" + path, timeout=8) as response:
            return response.read()
    page = fetch("/").decode()
    if not page or "<html" not in page.lower():
        raise ValueError("The page did not return HTML")
    tasks = json.loads(fetch("/api/tasks"))
    fresh = {w["name"] for w in tasks["workers"]
             if w.get("online") and w.get("seen", 0) >= since}
    if "pi-cpu" not in fresh:
        raise ValueError("The restarted worker has not sent a fresh heartbeat")
    return {"revision": manifest["revision"], "verified_at": time.time(),
            "checks": ["source_hashes", "page", "task_api", "fresh_pi_cpu"]}


def dependencies(path):
    """Validate the shared runtime without modifying it during a deployment."""
    from importlib.metadata import version
    from pip._vendor.packaging.requirements import Requirement
    installed = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        actual = version(requirement.name)
        if actual not in requirement.specifier:
            raise ValueError(f"{requirement.name} {actual} does not satisfy {requirement.specifier}; "
                             "prepare runtime dependencies before publishing")
        installed[requirement.name] = actual
    return installed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("pack", "verify", "health", "dependencies"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--since", type=float, default=0)
    args = parser.parse_args()
    actions = {"pack": lambda: pack(args.path), "verify": lambda: verify(args.path),
               "health": lambda: health(args.path, args.since), "dependencies": lambda: dependencies(args.path)}
    result = actions[args.action]()
    print(result if isinstance(result, str) else json.dumps(result))

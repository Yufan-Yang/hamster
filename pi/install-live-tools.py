#!/usr/bin/env python3
"""Install the pinned upstream Biliup CLI used by the live clipping publisher."""
import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path


def main():
    version = "v1.2.11"
    machine = platform.machine()
    arch = {"aarch64": "aarch64-linux", "armv7l": "arm-linux", "x86_64": "x86_64-linux"}.get(machine)
    if platform.system() != "Linux" or not arch:
        raise SystemExit("Run this installer on the Linux Pi (supported: aarch64, armv7l, x86_64)")
    request = urllib.request.Request(f"https://api.github.com/repos/biliup/biliup/releases/tags/{version}",
                                     headers={"User-Agent": "shiguang-live-installer"})
    release = json.load(urllib.request.urlopen(request, timeout=30))
    name = f"biliupR-{version}-{arch}.tar.xz"
    asset = next(a for a in release["assets"] if a["name"] == name)
    payload = urllib.request.urlopen(asset["browser_download_url"], timeout=120).read()
    digest = hashlib.sha256(payload).hexdigest()
    if asset.get("digest", "").startswith("sha256:") and digest != asset["digest"][7:]:
        raise SystemExit("Upstream release digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
        members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).name in ("biliup", "biliupR")]
        if len(members) != 1:
            raise SystemExit("Release does not contain exactly one Biliup executable")
        binary = archive.extractfile(members[0]).read()
    target = Path(os.environ.get("STATE_DIR", "/var/lib/grabber")) / "tools" / "biliup"
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(".next")
    staged.write_bytes(binary)
    staged.chmod(0o755)
    subprocess.run([str(staged), "--version"], check=True)
    if target.exists():
        target.rename(target.with_suffix(".previous"))
    staged.replace(target)
    print(f"Installed {version}: {target}; archive sha256={digest}")


if __name__ == "__main__":
    main()

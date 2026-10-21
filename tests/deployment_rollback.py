#!/usr/bin/env python3
"""Run deploy.sh's remote transaction locally with service/network commands replaced."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OLD_REVISION = "a" * 40
NEW_REVISION = "b" * 40
SHIP = ("grabber.py", "index.html", "refresh_metadata.py", "requirements-pi.txt",
        "shiguang", "tests", "pi", "static", "REVISION", "RELEASE.json")


class DeploymentRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="grabber-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        self.staged = self.root / "release"
        self.commands = self.root / "bin"
        self.commands.mkdir()
        self.log = self.root / "commands.log"
        self.log.write_text("")
        self.make_release(self.app, OLD_REVISION, "old")
        self.make_release(self.staged, NEW_REVISION, "new")
        (self.app / "VERIFICATION.json").write_text('{"revision":"old-verified"}\n')
        (self.app / "static/old-only.js").write_text("old asset")
        (self.staged / "static/new-only.js").write_text("new asset")
        self.archive = self.root / "release.tgz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for entry in self.staged.iterdir():
                archive.add(entry, arcname=entry.name)
        deploy = (ROOT / "deploy.sh").read_text()
        match = re.search(r"<<'REMOTE_SCRIPT'\n(.*?)\nREMOTE_SCRIPT", deploy, re.S)
        self.assertIsNotNone(match, "deploy.sh must expose the remote transaction for this test")
        self.script = match.group(1)
        self.assertEqual(self.script.count("APP=/opt/grabber"), 1)
        self.script = self.script.replace("APP=/opt/grabber", f"APP={shlex.quote(str(self.app))}")
        self.script = self.script.replace("/tmp/shiguang-release-page.js", str(self.root / "page.js"))
        # Restrict PATH to a known set: filesystem operations are real; all privileged,
        # network, service, Python-validation and delay commands are test substitutes.
        for command in ("cat", "cp", "date", "mkdir", "mv", "rm", "seq", "tar"):
            executable = shutil.which(command)
            self.assertIsNotNone(executable, f"{command} is required for this filesystem integration test")
            (self.commands / command).symlink_to(executable)
        self.executable("sudo", """
if [ "$1" = -u ]; then shift 2; fi
exec "$@"
""")
        self.executable("systemctl", """
printf 'systemctl %s\\n' "$*" >> "$DEPLOY_TEST_LOG"
exit 0
""")
        self.executable("node", """
printf 'node %s\\n' "$*" >> "$DEPLOY_TEST_LOG"
exit 0
""")
        self.executable("curl", """
printf 'curl %s\\n' "$*" >> "$DEPLOY_TEST_LOG"
printf '%s' "${DEPLOY_TEST_HTTP_CODE:-200}"
""")
        self.executable("chown", "exit 0\n")
        self.executable("sleep", "exit 0\n")
        python = self.executable("python3", """
printf 'python %s\\n' "$*" >> "$DEPLOY_TEST_LOG"
if [ "$1" = -c ]; then : > "$DEPLOY_TEST_PAGE"; exit 0; fi
if [ "$1" = -m ] && [ "$2" = pip ] && [ "$3" = check ]; then exit 0; fi
case "$1" in
  */release.py|pi/release.py)
    case "$2" in
      dependencies) exit 0;;
      verify)
        count=0
        if [ -f "$DEPLOY_TEST_ROOT/verify-count" ]; then count=$(cat "$DEPLOY_TEST_ROOT/verify-count"); fi
        count=$((count + 1))
        printf '%s' "$count" > "$DEPLOY_TEST_ROOT/verify-count"
        if [ "$count" -eq 2 ] && [ "${DEPLOY_TEST_CHANGE_DURING_VALIDATION:-0}" = 1 ]; then
          printf '%s\\n' external-change > "$DEPLOY_TEST_APP/REVISION"
        fi
        exit 0;;
      health)
        printf 'health\\n' >> "$DEPLOY_TEST_ROOT/health-count"
        [ "${DEPLOY_TEST_FAIL_HEALTH:-0}" != 1 ] || exit 1
        printf '{"revision":"%s","checks":["mock-services"]}\\n' "$(cat "$DEPLOY_TEST_APP/REVISION")"
        exit 0;;
    esac;;
  tests/*.py) exit 0;;
esac
printf 'unexpected python invocation: %s\\n' "$*" >&2
exit 97
""")
        venv = self.app / "venv/bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(python)
        pip = venv / "pip"
        pip.write_text("#!/bin/sh\nexit 0\n")
        pip.chmod(0o755)
        self.before = self.shipped_state()
        self.environment = {
            "PATH": str(self.commands), "LANG": "C",
            "DEPLOY_TEST_ROOT": str(self.root), "DEPLOY_TEST_APP": str(self.app),
            "DEPLOY_TEST_LOG": str(self.log), "DEPLOY_TEST_PAGE": str(self.root / "page.js"),
        }

    def executable(self, name, body):
        path = self.commands / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o755)
        return path

    def make_release(self, directory, revision, marker):
        directory.mkdir()
        for name in ("shiguang", "tests", "pi", "static"):
            (directory / name).mkdir()
        for name in ("grabber.py", "refresh_metadata.py", "requirements-pi.txt", "shiguang/core.py",
                     "pi/release.py", "tests/smoke.py", "static/live.js", "static/style.css"):
            (directory / name).write_text(f"{marker}: {name}\n")
        (directory / "index.html").write_text(f"<script>const release = '{marker}';</script>")
        (directory / "REVISION").write_text(revision + "\n")
        (directory / "RELEASE.json").write_text(json.dumps({"revision": revision, "marker": marker}))

    def shipped_state(self):
        result = {}
        for name in (*SHIP, "VERIFICATION.json"):
            path = self.app / name
            if path.is_file():
                result[name] = path.read_bytes()
            elif path.is_dir():
                for item in path.rglob("*"):
                    if item.is_file():
                        result[str(item.relative_to(self.app))] = item.read_bytes()
        return result

    def run_deployment(self, expected=OLD_REVISION, **options):
        return subprocess.run(
            ["/bin/sh", "-s", "--", str(self.archive), NEW_REVISION, expected],
            input=self.script, text=True, capture_output=True,
            env={**self.environment, **options}, cwd=self.root, timeout=15,
        )

    def test_success_promotes_code_static_manifest_and_verified_revision(self):
        result = self.run_deployment()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.app / "REVISION").read_text().strip(), NEW_REVISION)
        self.assertEqual(json.loads((self.app / "RELEASE.json").read_text())["marker"], "new")
        self.assertEqual((self.app / "static/live.js").read_text(), "new: static/live.js\n")
        self.assertTrue((self.app / "static/new-only.js").exists())
        self.assertFalse((self.app / "static/old-only.js").exists())
        self.assertEqual(json.loads((self.app / "VERIFICATION.json").read_text())["revision"], NEW_REVISION)
        self.assertEqual((self.root / "health-count").read_text().splitlines(), ["health"] * 3)
        self.assertFalse(self.archive.exists())

    def test_failed_health_restores_code_static_manifest_and_previous_receipt(self):
        result = self.run_deployment(DEPLOY_TEST_FAIL_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restoring application, static assets and revision", result.stdout)
        self.assertEqual(self.shipped_state(), self.before)
        self.assertTrue((self.app / "static/old-only.js").exists())
        self.assertFalse((self.app / "static/new-only.js").exists())
        self.assertEqual((self.root / "health-count").read_text().splitlines(), ["health"] * 20)
        log = self.log.read_text()
        self.assertEqual(log.count("systemctl restart grabber\n"), 2)
        self.assertEqual(log.count("systemctl restart grabber-worker\n"), 2)

    def test_failed_http_health_also_rolls_back_before_worker_health(self):
        result = self.run_deployment(DEPLOY_TEST_HTTP_CODE="503")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shipped_state(), self.before)
        self.assertFalse((self.root / "health-count").exists())

    def test_expected_revision_mismatch_does_not_change_production_or_staging(self):
        existing_stage = self.root / "app.next"
        existing_stage.mkdir()
        (existing_stage / "sentinel").write_text("untouched")
        result = self.run_deployment(expected="c" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("production changed since diagnosis", result.stdout)
        self.assertEqual(self.shipped_state(), self.before)
        self.assertEqual((existing_stage / "sentinel").read_text(), "untouched")
        self.assertFalse((self.root / "app.prev").exists())
        self.assertEqual(self.log.read_text(), "")

    def test_revision_change_during_validation_aborts_before_promotion(self):
        result = self.run_deployment(DEPLOY_TEST_CHANGE_DURING_VALIDATION="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("production changed during validation", result.stdout)
        expected = dict(self.before, REVISION=b"external-change\n")
        self.assertEqual(self.shipped_state(), expected)
        self.assertFalse((self.root / "app.prev").exists())
        self.assertNotIn("systemctl ", self.log.read_text())


if __name__ == "__main__":
    unittest.main()

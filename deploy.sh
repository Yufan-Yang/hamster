#!/bin/sh
# Build an exact source snapshot; serialize testing/promotion on the Pi.
# ./deploy.sh [--no-mac]
set -e
PI=${PI:-pi@192.168.3.200}
cd "$(dirname "$0")"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/shiguang-release.XXXXXX")
trap 'rm -rf "$TMP"' 0 HUP INT TERM

echo "== offline routing and deployment checks"
python3 tests/ai_routing.py
python3 tests/vpn_routing.py
python3 tests/deployment.py
python3 tests/deployment_rollback.py
python3 tests/heal_workflow.py
echo "== lint"
PYFLAKES=${PYFLAKES:-python3 -m pyflakes}
if $PYFLAKES --version >/dev/null 2>&1; then
  $PYFLAKES grabber.py shiguang tests pi/release.py mac/mac_worker.py
else
  echo "(no pyflakes here: skipped)"
fi

# Internal git refs retain dirty snapshots without changing the user's branch/index.
REV=$(python3 pi/release.py pack "$TMP/release.tgz")
EXPECTED=${SHIGUANG_EXPECTED_REVISION:-}
case "$EXPECTED" in *[!a-f0-9]*)
  echo "Invalid expected revision"; exit 1;;
esac
REMOTE="/tmp/shiguang-release-$REV-$$.tgz"
echo "== release $REV"
scp -q "$TMP/release.tgz" "$PI:$REMOTE"

# One lock covers staging, tests, promotion, verification and rollback. A second
# deploy cannot replace .next/.prev while the first deployment is checking them.
ssh "$PI" "sudo flock -n /opt/grabber.deploy.lock sh -s -- '$REMOTE' '$REV' '$EXPECTED'" <<'REMOTE_SCRIPT'
set -e
archive=$1
revision=$2
expected=$3
APP=/opt/grabber
trap 'rm -f "$archive"' 0
if [ -n "$expected" ] && [ "$(cat "$APP/REVISION")" != "$expected" ]; then
  echo "Deployment stopped: production changed since diagnosis"
  exit 1
fi
rm -rf "$APP.next"
mkdir -p "$APP.next"
tar xzf "$archive" -C "$APP.next"
rm -f "$archive"
chown -R grabber:grabber "$APP.next"
cd "$APP.next"
python3 pi/release.py verify "$APP.next" >/dev/null
echo "== tests against staged source and a database copy"
sudo -u grabber "$APP/venv/bin/python" pi/release.py dependencies requirements-pi.txt
sudo -u grabber "$APP/venv/bin/python" -m pip check
python3 -c "import re;open('/tmp/shiguang-release-page.js','w').write(re.findall(r'<script>(.*?)</script>',open('index.html').read(),re.S)[0])"
node --check /tmp/shiguang-release-page.js
rm -f /tmp/shiguang-release-page.js
node --check static/live.js
node --check static/devices.js
for test in workspace live_interactions live_recordings series; do node "tests/$test.js"; done
for test in backend_reliability intelligence live_assistant live_finalization live_review live_result task_lifecycle television device_routes cast_protocol; do
  sudo -u grabber "$APP/venv/bin/python" "tests/$test.py"
done
sudo -u grabber "$APP/venv/bin/python" tests/devices_browser.py
sudo -u grabber "$APP/venv/bin/python" tests/casting_browser.py "$APP.next"
sudo -u grabber "$APP/venv/bin/python" tests/download_fallback.py --local-http
for test in tests/heal_regression_*.py; do
  if [ -f "$test" ]; then sudo -u grabber "$APP/venv/bin/python" "$test"; fi
done
sudo -u grabber "$APP/venv/bin/python" tests/smoke.py "$APP.next"
python3 pi/release.py verify "$APP.next" >/dev/null
if [ -n "$expected" ] && [ "$(cat "$APP/REVISION")" != "$expected" ]; then
  echo "Deployment stopped: production changed during validation"
  exit 1
fi

echo "== promote and verify"
rm -rf "$APP.prev"
mkdir -p "$APP.prev"
files='grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests pi static REVISION RELEASE.json'
if [ -f "$APP.next/send-to-pi.shortcut" ]; then files="$files send-to-pi.shortcut"; fi
for f in $files; do
  if [ -e "$APP/$f" ]; then cp -a "$APP/$f" "$APP.prev/"; fi
done
if [ -f "$APP/VERIFICATION.json" ]; then cp -a "$APP/VERIFICATION.json" "$APP.prev/"; fi
rollback() {
  trap - 0 HUP INT TERM
  set +e
  echo "Verification failed: restoring application, static assets and revision"
  cd "$APP"
  for f in $files VERIFICATION.json; do
    rm -rf "$APP/$f"
    if [ -e "$APP.prev/$f" ]; then cp -a "$APP.prev/$f" "$APP/"; fi
  done
  systemctl restart grabber
  systemctl restart grabber-worker
  systemctl is-active grabber grabber-worker
  exit 1
}
trap rollback 0 HUP INT TERM
systemctl stop grabber-worker grabber
for f in $files; do rm -rf "$APP/$f"; cp -a "$APP.next/$f" "$APP/"; done
rm -f "$APP/VERIFICATION.json"
systemctl restart grabber
web_ok=0
for i in $(seq 1 20); do
  if [ "$(curl --max-time 5 -s -o /dev/null -w '%{http_code}' localhost:8088/)" = 200 ]; then web_ok=1; break; fi
  sleep 1
done
[ "$web_ok" = 1 ] || exit 1
started=$(date +%s)
systemctl restart grabber-worker
passed=0
for i in $(seq 1 20); do
  sleep 3
  if systemctl is-active -q grabber grabber-worker &&
     python3 "$APP/pi/release.py" health "$APP" --since "$started" > "$APP/VERIFICATION.json.tmp"; then
    passed=$((passed + 1))
    if [ "$passed" -ge 3 ]; then break; fi
  else
    passed=0
  fi
done
[ "$passed" -ge 3 ] || exit 1
mv "$APP/VERIFICATION.json.tmp" "$APP/VERIFICATION.json"
trap - 0 HUP INT TERM
echo "Verified release $revision (three consecutive checks)"
REMOTE_SCRIPT

if [ "${1:-}" != "--no-mac" ] && ! diff -q mac/mac_worker.py "$HOME/shiguang-compute/mac_worker.py" >/dev/null 2>&1; then
  echo "== Mac worker"
  ./mac/install.sh | tail -1
fi
echo "done: $REV"

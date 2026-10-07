#!/bin/sh
# Deploy 拾光 to the Pi: lint here, test on the Pi against a copy of the real database, then swap the code in and
# restart; if the page or the workers don't come back, the previous code goes back in.
#   ./deploy.sh            code to the Pi (and the Mac worker, when mac/ changed)
#   ./deploy.sh --no-mac   only the Pi
set -e
PI=${PI:-pi@192.168.3.200}
APP=/opt/grabber
cd "$(dirname "$0")"

echo "== lint"
PYFLAKES=${PYFLAKES:-python3 -m pyflakes}
if $PYFLAKES --version >/dev/null 2>&1; then $PYFLAKES grabber.py shiguang tests mac/mac_worker.py; else echo "(no pyflakes here: skipped)"; fi
python3 -c "import re;open('/tmp/shiguang-page.js','w').write(re.findall(r'<script>(.*?)</script>',open('index.html').read(),re.S)[0])"
scp -q /tmp/shiguang-page.js "$PI":/tmp/shiguang-page.js
ssh "$PI" 'node --check /tmp/shiguang-page.js; s=$?; rm -f /tmp/shiguang-page.js; exit $s'

echo "== copy to the Pi (staging)"
tar czf /tmp/shiguang-deploy.tgz grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests static send-to-pi.shortcut 2>/dev/null \
  || tar czf /tmp/shiguang-deploy.tgz grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests static
scp -q /tmp/shiguang-deploy.tgz "$PI":/tmp/
ssh "$PI" "sudo rm -rf $APP.next && sudo mkdir -p $APP.next && sudo tar xzf /tmp/shiguang-deploy.tgz -C $APP.next && rm /tmp/shiguang-deploy.tgz \
  && sudo find $APP.next -name __pycache__ -prune -exec rm -rf {} + ; sudo chown -R grabber:grabber $APP.next"

echo "== smoke test on a copy of the database"
ssh "$PI" "sudo -u grabber $APP/venv/bin/pip install -q -r $APP.next/requirements-pi.txt && cd $APP.next && sudo -u grabber $APP/venv/bin/python tests/smoke.py $APP.next > /tmp/shiguang-smoke.log 2>&1; s=\$?; grep -v -i warn /tmp/shiguang-smoke.log; rm -f /tmp/shiguang-smoke.log; exit \$s"

echo "== swap in and restart"
ssh "$PI" "set -e
sudo rm -rf $APP.prev && sudo mkdir -p $APP.prev
for f in grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests; do [ -e $APP/\$f ] && sudo cp -a $APP/\$f $APP.prev/ || true; done
for f in grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests; do sudo rm -rf $APP/\$f; sudo cp -a $APP.next/\$f $APP/; done
sudo cp -a $APP.next/static/. $APP/static/
[ -e $APP.next/send-to-pi.shortcut ] && sudo cp -a $APP.next/send-to-pi.shortcut $APP/ || true
sudo systemctl restart grabber grabber-worker
ok=0
for i in \$(seq 1 20); do
  sleep 3
  if [ \"\$(curl -s -o /dev/null -w '%{http_code}' localhost:8088/)\" = 200 ] && systemctl is-active -q grabber-worker \
     && curl -s localhost:8088/api/tasks | grep -q '\"pi-cpu\"'; then ok=1; break; fi
done
if [ \$ok = 1 ]; then echo 'up and running'; else
  echo 'NOT healthy: putting the previous code back'
  for f in grabber.py index.html refresh_metadata.py requirements-pi.txt shiguang tests; do sudo rm -rf $APP/\$f; [ -e $APP.prev/\$f ] && sudo cp -a $APP.prev/\$f $APP/; done
  sudo systemctl restart grabber grabber-worker; exit 1
fi"

if [ "$1" != "--no-mac" ] && ! diff -q mac/mac_worker.py "$HOME/shiguang-compute/mac_worker.py" >/dev/null 2>&1; then
  echo "== Mac worker"
  ./mac/install.sh | tail -1
fi
echo "done"

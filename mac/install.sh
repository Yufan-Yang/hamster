#!/bin/sh
# Install the 拾光 compute worker on a Mac (Apple silicon): ./install.sh [dir]   (default ~/shiguang-compute)
# Needs uv and ffmpeg (Homebrew). The Pi's /etc/grabber.env must have the same COMPUTE_TOKEN as <dir>/token.
set -e
DIR="${1:-$HOME/shiguang-compute}"
mkdir -p "$DIR"
cp "$(dirname "$0")/mac_worker.py" "$DIR/"
[ -d "$DIR/.venv" ] || uv venv -q --python 3.12 "$DIR/.venv"
uv pip install -q --python "$DIR/.venv/bin/python" mlx-whisper numpy requests opencc-python-reimplemented \
    onnxruntime pillow pyobjc-framework-Vision pyobjc-framework-Cocoa
# the Pi's image model, so pictures get the same vectors on both
mkdir -p "$DIR/models/clip"
[ -f "$DIR/models/clip/vision.onnx" ] || ssh pi@192.168.3.200 'sudo cat /var/lib/grabber/models/clip/vision.onnx' > "$DIR/models/clip/vision.onnx"
if [ ! -f "$DIR/token" ]; then
  python3 -c "import secrets; print(secrets.token_hex(24))" > "$DIR/token"
  chmod 600 "$DIR/token"
  echo "New token in $DIR/token: add it to the Pi's /etc/grabber.env as COMPUTE_TOKEN=... and restart grabber"
fi
# `task publish ...` / `task board`: one-line commands for the board
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/mac_worker.py" "$@"\n' "$DIR" "$DIR" > "$DIR/task"
chmod +x "$DIR/task"
PLIST="$HOME/Library/LaunchAgents/site.shiguang.compute.plist"
sed "s#__DIR__#$DIR#g" "$(dirname "$0")/site.shiguang.compute.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/site.shiguang.compute" 2>/dev/null || true
while launchctl print "gui/$(id -u)/site.shiguang.compute" >/dev/null 2>&1; do sleep 1; done  # bootout is async
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "running; log: $DIR/worker.log"

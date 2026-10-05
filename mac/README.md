# 拾光 on the Mac mini (compute worker)

The Mac mini (M2 Pro) works on the Pi's task board: full subtitles with Whisper large-v3-turbo on the GPU (~25x
realtime, against ~1x for whisper small on the Pi), what covers and video keyframes look like (the Pi's own CLIP
model, ~30 ms a picture against ~3 s on the Pi) and the text on covers (macOS Vision). The board and everything else
stay on the Pi; the Mac claims tasks (`/api/tasks/*` on the Pi, LAN only, token), hands results back, and can publish
tasks itself. When the Mac is off, asleep or a game is in front, its tasks go back on the board and the Pi does them,
slowly. Files put in `~/拾光投递` become 随记 of the account in `config.json` (e-books — EPUB, PDF, MOBI, AZW3 — go onto its 书架). Install: `./install.sh` (needs Homebrew `uv` and `ffmpeg`).

## Everything that was changed on the Mac (2026-10-04)

| What | Where | Size |
|---|---|---|
| Homebrew `ffmpeg` (+ dependencies dav1d, mpg123, lame, libvmaf, libvpx, opus, sdl3, sdl2-compat, svt-av1, x264, x265) | `/usr/local/Homebrew` | ~105 MB with iperf3 |
| Homebrew `iperf3` (only used to measure the Wi-Fi) | `/usr/local/Homebrew` | <1 MB |
| Worker folder: Python venv (mlx-whisper, numpy, requests, opencc, onnxruntime, pillow, pyobjc Vision/Cocoa), `mac_worker.py`, `task` (one-line commands: `~/shiguang-compute/task publish transcribe job:446:0 --force`, `task board`), `token`, `config.json` (drop folder account), `models/clip/vision.onnx` (the Pi's image model, 88 MB), `worker.log`, `outbox.jsonl` (tasks waiting for the Pi, only while it's unreachable), optional `game-apps.txt` (more apps that count as games), `bench/` (two test sound clips) | `~/shiguang-compute` | ~1.3 GB |
| Drop folder: files put here become 随记, then move to `已投递` | `~/拾光投递` | your files |
| AI requests from the Pi (2026-10-05): two claim loops run the Claude Code already installed here (`~/.local/bin/claude -p`, logged in with the Claude subscription; nothing new installed), in an empty folder `claude-cwd/`, through the Mac's own proxy (`"proxy"` in `config.json`). Uses the subscription's limits; when they're used up the loops say they're paused and the Pi asks DeepSeek | `~/shiguang-compute/claude-cwd`, `config.json` | – |
| Whisper large-v3-turbo model (MLX) | `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo` (+ its files in `blobs/`) | ~1.5 GB |
| Login item that keeps the worker running (`launchd`, Nice 10, restarts if it stops) | `~/Library/LaunchAgents/site.shiguang.compute.plist` | – |

Not changed: no `sudo` on the Mac, no system settings (`pmset`, sleep, firewall), nothing outside the paths above.
Other models already in `~/.cache/huggingface` (e.g. Fun-ASR-Nano) were there before and aren't the worker's.
If macOS asked whether Python may "find devices on the local network", that answer is under System Settings →
Privacy & Security → Local Network.

On the Pi, for the Mac: `COMPUTE_TOKEN=` in `/etc/grabber.env` (same value as `~/shiguang-compute/token`).

## Undo

```sh
# 1. stop the worker and remove the login item
launchctl bootout gui/$(id -u)/site.shiguang.compute
rm ~/Library/LaunchAgents/site.shiguang.compute.plist

# 2. the Whisper model (removes its files from blobs/ too; uses the worker's venv, so do this before step 3)
~/shiguang-compute/.venv/bin/python - <<'EOF'
from huggingface_hub import scan_cache_dir
c = scan_cache_dir()
revs = [r.commit_hash for repo in c.repos if repo.repo_id == "mlx-community/whisper-large-v3-turbo" for r in repo.revisions]
c.delete_revisions(*revs).execute()
EOF

# 3. the worker folder (venv, token, log, test clips)
rm -rf ~/shiguang-compute
# the drop folder holds your own files: look before removing it
open ~/拾光投递

# 4. Homebrew packages (only if nothing else of yours uses them)
brew uninstall ffmpeg iperf3
brew uninstall dav1d mpg123 lame libvmaf libvpx opus sdl3 sdl2-compat svt-av1 x264 x265  # what ffmpeg pulled in

# 5. on the Pi: remove the COMPUTE_TOKEN line from /etc/grabber.env and restart grabber
ssh pi@192.168.3.200 "sudo sed -i '/COMPUTE_TOKEN/d; /Mac mini compute worker/d' /etc/grabber.env && sudo systemctl restart grabber"
```

Without the token the Pi refuses `/api/tasks/*` and goes back to doing everything itself (speech-to-text in
idle time with whisper small). Subtitles the Mac already made stay (`.srt` next to the videos).

# 自修复: causes seen before

Read by the healer (mac/mac_worker.py) before it diagnoses an incident. Each entry: what it looks like, the real
cause, how to tell it apart, what to do. Add an entry when a diagnosis turned out wrong or a new cause shows up.

## "database disk image is malformed", or new rows missing / old data coming back

- **Cause seen (2026-10-09)**: something opened `grabber.db` where SQLite's locks don't reach: through the
  `/mnt/media` mergerfs view instead of `/mnt/disk1/.grabber/grabber.db`, or a script on another machine. That
  connection thought it was alone and, on closing, checkpointed and deleted `grabber.db-wal`/`-shm` while the web
  and worker processes still had them open. They kept writing into the deleted WAL; new connections saw an older
  database; pages changed under the services gave "malformed". It was NOT the disk, power or SQLite.
- **How to tell**: the evidence shows `grabber.db-wal (deleted)` in the services' open files (or a `db` incident
  "数据库文件在 … 进程运行时被删除了"). The disks' SMART data / kernel log show no I/O errors.
- **What to do (a person, not a code fix)**: don't restart the services first (closing would checkpoint the
  deleted WAL into a database that moved on). Freeze them (`kill -STOP`), copy `/proc/<worker pid>/fd/<n>` (the
  deleted WAL) and `grabber.db` to a new folder, replay the WAL into the copy, `PRAGMA integrity_check`, then stop
  the services, put the checked copy in place and start them. Keep the old files.
- Only blame the disk when the evidence has I/O errors, SMART reallocated/pending sectors, or the disk dropping off
  USB (kernel log).

## Download failures that are not bugs

- Cloudflare "Just a moment..." / 403 `cf-mitigated`: the site's bot challenge (missav etc.); no code fix.
- YouTube "Sign in to confirm you're not a bot": the exit IP is flagged; the code already retries through other exits.
- B站 HTTP 412 on the space API: rate limiting, retried; persistent failures usually need fresh cookies.
- `network:` errors, timeouts while proxy-a is out of quota: the proxy, not the code.

## AI calls

- DeepSeek 402: balance used up (top up). Claude worker paused: the Mac is gaming or out of quota; falls back on its own.

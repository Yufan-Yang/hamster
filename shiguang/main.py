"""Process entry points: the web page, the worker, a job, a task."""
import threading
import time

from . import migrations, board, channels, heal, library, pipeline, search, telegram, usage, web
from .core import (DOWNLOAD_WORKERS, EXTERNAL_PORT, INCOMPLETE, NOTE_MAX_UPLOAD, PORT, TG_TOKEN, app, finishing_threads, init_db, listen_bell)


def main_web():
    init_db()
    web.setup_app()
    listen_bell("web")
    web.ensure_admin()
    heal.db_watch("web")
    threading.Thread(target=library.warm_probes, daemon=True).start()
    if (search.CLIP_DIR / "text.onnx").exists():
        threading.Thread(target=search.clip_text, args=("拾光",), daemon=True).start()  # first search needn't wait for it
    from waitress import serve
    serve(app, listen=f"0.0.0.0:{PORT} 127.0.0.1:{EXTERNAL_PORT}", threads=16, channel_timeout=300,
          ident="shiguang", max_request_body_size=NOTE_MAX_UPLOAD, trusted_proxy="127.0.0.1", trusted_proxy_count=1,
          trusted_proxy_headers="x-forwarded-for", clear_untrusted_proxy_headers=True)


def main_worker():
    init_db(reset=True)
    web.setup_app()
    listen_bell("worker")
    INCOMPLETE.mkdir(parents=True, exist_ok=True)
    heal.db_watch("worker")
    for _ in range(DOWNLOAD_WORKERS):
        threading.Thread(target=pipeline.worker_loop, daemon=True).start()
    threading.Thread(target=pipeline.sweep_loop, daemon=True).start()
    threading.Thread(target=channels.sub_loop, daemon=True).start()
    threading.Thread(target=board.now_loop, daemon=True).start()
    migrations.run_once_jobs()  # data jobs after an upgrade (each once)
    board.publish_pending_notes()
    board.reindex_old_model()
    threading.Thread(target=board.light_loop, daemon=True).start()
    threading.Thread(target=board.light_loop, args=("ai",), daemon=True).start()
    threading.Thread(target=board.light_loop, args=("ai-quick",), daemon=True).start()
    threading.Thread(target=board.heavy_loop, daemon=True).start()
    threading.Thread(target=usage.traffic_loop, daemon=True).start()
    if TG_TOKEN:
        threading.Thread(target=telegram.tg_loop, daemon=True).start()
        threading.Thread(target=telegram.tg_progress_loop, daemon=True).start()
    while True:
        time.sleep(3600)


def main_run_job(job_id):
    init_db()
    web.setup_app()
    pipeline.run_job(job_id)
    # finishing touches run in threads: let them complete (at most 10 minutes in all). Only our own threads:
    # libraries leave pool threads around that never end, and waiting on each of those kept the process for an hour
    deadline = time.time() + 600
    for t in finishing_threads:
        t.join(timeout=max(0, deadline - time.time()))


def main(argv):
    """grabber.py [web | worker | run-job N | task N WORKER]"""
    mode = argv[1] if len(argv) > 1 else "web"
    if mode == "worker":
        main_worker()
    elif mode == "run-job":
        main_run_job(int(argv[2]))
    elif mode == "task":
        init_db()
        board.run_task_process(int(argv[2]), argv[3] if len(argv) > 3 else board.PI_WORKERS["cpu"])
    else:
        main_web()

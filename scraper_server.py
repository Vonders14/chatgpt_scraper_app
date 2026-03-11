"""
scraper_server.py
-----------------
Flask web server that wraps scraper_core.py entirely untouched.
Receives domain lists from the HTML UI, runs crawl_site() per domain,
and streams live progress back via Server-Sent Events.

After each site crawl completes, the merged .txt file produced by
scraper_core is copied into FLAT_OUTPUT_DIR as a single flat file:
    ALL_TEXT_FILES/<domain>.txt

scraper_core.py is NOT modified in any way.

Run:
    pip install flask
    python scraper_server.py

Then open:  http://localhost:5000
"""

import json
import os
import queue
import re
import shutil
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

# Import everything from the original script — nothing is changed there
from scraper_core import (
    ScrapeConfig,
    SessionFactory,
    crawl_site,
    normalize_url,
    get_registered_domain,
    get_hostname,
    safe_filename,
)

# ── Config ────────────────────────────────────────────────────────────────────

# Temp working dir — scraper_core writes its nested structure here
WORK_DIR = r"C:\Users\Temp\Documents\ChatGPTSc\scraped_text_work"

# Final flat output dir — one .txt per domain lands here
FLAT_OUTPUT_DIR = r"C:\Users\Temp\Documents\ChatGPTSc\ALL_TEXT_FILES"

PORT = 5000

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".", static_url_path="")

_job_lock    = threading.Lock()
_job_running = False
_job_queue: queue.Queue = queue.Queue()


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_domain_list(raw: str) -> list[str]:
    """Parse newline / comma / space separated domain list into normalised URLs."""
    tokens = re.split(r"[\n\r,;\t ]+", raw.strip())
    urls = []
    seen_keys = set()
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        u = normalize_url(t)
        if not u:
            continue
        key = get_registered_domain(u) or get_hostname(u) or u
        if key not in seen_keys:
            seen_keys.add(key)
            urls.append(u)
    return urls


def push_event(event_type: str, data: dict):
    _job_queue.put({"event": event_type, "data": data})


def copy_to_flat(site_result, cfg: ScrapeConfig) -> str | None:
    """
    After scraper_core writes  WORK_DIR/<site_key>/merged/<site_key>.txt
    copy it to                 FLAT_OUTPUT_DIR/<site_key>.txt

    Handles duplicates the same way the original flatten script did:
    if <site_key>.txt already exists, try <site_key>_1.txt, _2.txt, ...

    Returns the final filename used, or None if no source file found.
    """
    site_key    = site_result.site_key
    merged_path = Path(cfg.output_dir) / site_key / cfg.merged_txt_dirname / f"{site_key}.txt"

    if not merged_path.exists() or merged_path.stat().st_size == 0:
        return None

    flat_dir = Path(FLAT_OUTPUT_DIR)
    flat_dir.mkdir(parents=True, exist_ok=True)

    dest = flat_dir / f"{site_key}.txt"
    if dest.exists():
        counter = 1
        while (flat_dir / f"{site_key}_{counter}.txt").exists():
            counter += 1
        dest = flat_dir / f"{site_key}_{counter}.txt"

    shutil.copy2(merged_path, dest)
    return dest.name


def run_job(urls: list[str], cfg: ScrapeConfig):
    """Run the crawl in a background thread, pushing SSE events."""
    global _job_running

    push_event("run_start", {
        "total":      len(urls),
        "output_dir": FLAT_OUTPUT_DIR,
    })

    session_factory = SessionFactory(cfg)

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {ex.submit(crawl_site, u, cfg, session_factory): u for u in urls}
        for fut in as_completed(futures):
            root = futures[fut]
            try:
                site_result, page_results = fut.result()

                # Copy the merged file into the flat output folder
                flat_filename = None
                if site_result.status not in ("skipped_existing", "invalid_url", "no_output"):
                    flat_filename = copy_to_flat(site_result, cfg)
                elif site_result.status == "skipped_existing":
                    # File was already scraped — check if it's already in flat dir too
                    existing = Path(FLAT_OUTPUT_DIR) / f"{site_result.site_key}.txt"
                    flat_filename = existing.name if existing.exists() else None

                d = asdict(site_result)
                d["page_results"]  = [asdict(p) for p in page_results]
                d["flat_filename"] = flat_filename
                push_event("site_done", d)

            except Exception as e:
                push_event("site_error", {"root_url": root, "error": str(e)[:300]})

    push_event("run_done", {"total": len(urls)})

    with _job_lock:
        _job_running = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "scraper_ui.html")


@app.route("/start", methods=["POST"])
def start():
    global _job_running

    with _job_lock:
        if _job_running:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 409

    data       = request.get_json(force=True)
    raw        = data.get("domains", "")
    urls       = parse_domain_list(raw)

    if not urls:
        return jsonify({"ok": False, "error": "No valid domains found."}), 400

    # scraper_core writes its nested structure into WORK_DIR
    cfg = ScrapeConfig(
        input_path         = "__ui__",
        output_dir         = WORK_DIR,
        max_pages_per_site = int(data.get("max_pages",   25)),
        max_depth          = int(data.get("max_depth",    2)),
        max_seconds_per_site = int(data.get("max_seconds", 90)),
        max_workers        = int(data.get("max_workers",  6)),
        save_per_page_files = bool(data.get("save_per_page", False)),
        resume             = bool(data.get("resume", True)),
    )

    # Drain leftover SSE events from prior run
    while not _job_queue.empty():
        try:
            _job_queue.get_nowait()
        except queue.Empty:
            break

    with _job_lock:
        _job_running = True

    threading.Thread(target=run_job, args=(urls, cfg), daemon=True).start()
    return jsonify({"ok": True, "queued": len(urls)})


@app.route("/events")
def events():
    """Server-Sent Events stream."""
    def generate():
        while True:
            try:
                item = _job_queue.get(timeout=25)
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item["event"] == "run_done":
                    break
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/status")
def status():
    try:
        txt_count = sum(
            1 for f in Path(FLAT_OUTPUT_DIR).glob("*.txt")
        )
    except Exception:
        txt_count = 0
    return jsonify({
        "running":    _job_running,
        "output_dir": FLAT_OUTPUT_DIR,
        "txt_count":  txt_count,
    })


# ── Boot ──────────────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"✅  Scraper UI    →  http://localhost:{PORT}")
    print(f"📁  Flat output   →  {FLAT_OUTPUT_DIR}")
    print(f"🔧  Work dir      →  {WORK_DIR}")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(port=PORT, debug=False, threaded=True)
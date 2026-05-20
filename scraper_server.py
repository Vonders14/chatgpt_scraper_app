"""
scraper_server.py
-----------------
Flask web server wrapping scraper_core.py (untouched).

scraper_core always writes nested output:
    WORK_DIR/<site_key>/merged/<site_key>.txt

After each site finishes, copy_to_flat() copies that .txt to:
    ALL_TEXT_FILES/<site_key>.txt

ALL_TEXT_FILES ends up with only flat .txt files + a logs/ subfolder.
WORK_DIR is a temp area and can be deleted any time.

Run:
    pip install flask
    python scraper_server.py
"""

import csv
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from scraper_core import (
    ScrapeConfig,
    SessionFactory,
    crawl_site,
    normalize_url,
    get_registered_domain,
    get_hostname,
    safe_filename,
)

# ── Config ─────────────────────────────────────────────────────────────────────

WORK_DIR = r"C:\Users\Temp\Documents\ChatGPTSc\scraped_text_work"
DEFAULT_FLAT_DIR = r"C:\Users\Temp\Documents\ChatGPTSc\ALL_TEXT_FILES"
PORT = 5000

# ── App ────────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=".", static_url_path="")

_runs: dict = {}
_runs_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_domain_list(raw: str) -> list[str]:
    tokens = re.split(r"[\n\r,;\t ]+", raw.strip())
    urls, seen = [], set()
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        u = normalize_url(t)
        if not u:
            continue
        key = get_registered_domain(u) or get_hostname(u) or u
        if key not in seen:
            seen.add(key)
            urls.append(u)
    return urls


def write_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_summary_csv(results: list[dict], run_id: str, flat_dir: str):
    logs_dir = Path(flat_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"summary_{run_id}.csv"
    if not results:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def setup_logging(run_id: str, flat_dir: str):
    logs_dir = Path(flat_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_path    = logs_dir / f"run_{run_id}.log"
    jsonl_path  = logs_dir / f"run_{run_id}.jsonl"
    master_path = logs_dir / "scrape_master_log.jsonl"

    logger = logging.getLogger(f"scraper.{run_id}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)

    return logger, jsonl_path, master_path


def copy_to_flat(site_result, work_cfg: ScrapeConfig, flat_dir: str) -> str | None:
    """
    scraper_core writes:  WORK_DIR/<site_key>/merged/<site_key>.txt
    Copy that to:         flat_dir/<site_key>.txt
    Returns filename or None.
    """
    site_key = site_result.site_key
    src = Path(work_cfg.output_dir) / site_key / work_cfg.merged_txt_dirname / f"{site_key}.txt"

    if not src.exists() or src.stat().st_size == 0:
        return None

    flat = Path(flat_dir)
    flat.mkdir(parents=True, exist_ok=True)

    dest = flat / f"{site_key}.txt"
    if dest.exists():
        counter = 1
        while (flat / f"{site_key}_{counter}.txt").exists():
            counter += 1
        dest = flat / f"{site_key}_{counter}.txt"

    shutil.copy2(src, dest)
    return dest.name


# ── Job runner ─────────────────────────────────────────────────────────────────

def run_job(run_id: str, urls: list[str], work_cfg: ScrapeConfig, flat_dir: str):
    event_q = _runs[run_id]["queue"]

    def push(event_type: str, data: dict):
        event_q.put({"event": event_type, "data": data})

    logger, jsonl_path, master_path = setup_logging(run_id, flat_dir)
    logger.info("Run started. run_id=%s sites=%d flat_dir=%s", run_id, len(urls), flat_dir)

    push("run_start", {"run_id": run_id, "total": len(urls), "output_dir": flat_dir})
    write_jsonl(master_path, {
        "type": "run_start", "run_id": run_id,
        "timestamp": utcnow(), "sites": len(urls),
    })

    session_factory = SessionFactory(work_cfg)
    site_results = []

    with ThreadPoolExecutor(max_workers=work_cfg.max_workers) as ex:
        futures = {ex.submit(crawl_site, u, work_cfg, session_factory): u for u in urls}
        for fut in as_completed(futures):
            root = futures[fut]
            try:
                site_result, page_results = fut.result()
                result_dict = asdict(site_result)
                site_results.append(result_dict)

                # Copy .txt from nested WORK_DIR into flat output
                flat_filename = None
                if site_result.status == "skipped_existing":
                    existing = Path(flat_dir) / f"{site_result.site_key}.txt"
                    flat_filename = existing.name if existing.exists() else None
                elif site_result.status not in ("invalid_url", "no_output"):
                    flat_filename = copy_to_flat(site_result, work_cfg, flat_dir)

                logger.info(
                    "DONE %s | status=%s | saved=%d | chars=%d | sec=%.2f | flat=%s",
                    site_result.site_key, site_result.status,
                    site_result.pages_saved, site_result.total_chars,
                    site_result.duration_sec, flat_filename or "none",
                )

                write_jsonl(jsonl_path, {
                    "type": "site_result", "run_id": run_id, "site": result_dict,
                })
                write_jsonl(master_path, {
                    "type": "site_result", "run_id": run_id,
                    "timestamp": utcnow(), "site": result_dict,
                })

                d = result_dict.copy()
                d["flat_filename"] = flat_filename
                push("site_done", d)

            except Exception as e:
                msg = str(e)[:300]
                logger.exception("Fatal error for %s: %s", root, msg)
                write_jsonl(jsonl_path, {
                    "type": "fatal_error", "run_id": run_id,
                    "root": root, "error": msg,
                })
                push("site_error", {"root_url": root, "error": msg})

    write_summary_csv(site_results, run_id, flat_dir)
    write_jsonl(master_path, {
        "type": "run_complete", "run_id": run_id, "timestamp": utcnow(),
        "total_sites": len(site_results),
        "total_pages_saved": sum(r.get("pages_saved", 0) for r in site_results),
        "total_chars": sum(r.get("total_chars", 0) for r in site_results),
    })
    logger.info("Run complete. %d sites processed.", len(site_results))

    push("run_done", {"run_id": run_id, "total": len(urls)})
    with _runs_lock:
        if run_id in _runs:
            _runs[run_id]["running"] = False


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "scraper_ui.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True)
    urls = parse_domain_list(data.get("domains", ""))
    if not urls:
        return jsonify({"ok": False, "error": "No valid domains found."}), 400

    flat_dir = (data.get("output_dir") or "").strip() or DEFAULT_FLAT_DIR
    Path(flat_dir).mkdir(parents=True, exist_ok=True)
    Path(WORK_DIR).mkdir(parents=True, exist_ok=True)

    run_id = str(uuid.uuid4())[:8]

    work_cfg = ScrapeConfig(
        input_path           = "__ui__",
        output_dir           = WORK_DIR,
        max_pages_per_site   = int(data.get("max_pages",   25)),
        max_depth            = int(data.get("max_depth",    2)),
        max_seconds_per_site = int(data.get("max_seconds", 90)),
        max_workers          = int(data.get("max_workers",  6)),
        save_per_page_files  = False,
        resume               = bool(data.get("resume", True)),
    )

    with _runs_lock:
        _runs[run_id] = {"queue": queue.Queue(), "running": True}

    threading.Thread(
        target=run_job, args=(run_id, urls, work_cfg, flat_dir), daemon=True
    ).start()

    return jsonify({"ok": True, "run_id": run_id, "queued": len(urls)})


@app.route("/events/<run_id>")
def events(run_id: str):
    run = _runs.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run_id"}), 404

    def generate():
        event_q = run["queue"]
        while True:
            try:
                item = event_q.get(timeout=25)
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                if item["event"] == "run_done":
                    break
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/events")
def events_legacy():
    with _runs_lock:
        running_ids = [rid for rid, r in _runs.items() if r.get("running")]
    if not running_ids:
        return Response(
            'event: run_done\ndata: {"run_id":"none","total":0}\n\n',
            mimetype="text/event-stream",
        )
    return events(running_ids[-1])


@app.route("/status")
def status():
    try:
        txt_count = sum(1 for _ in Path(DEFAULT_FLAT_DIR).glob("*.txt"))
    except Exception:
        txt_count = 0
    with _runs_lock:
        is_running = any(r.get("running") for r in _runs.values())
    return jsonify({
        "running":    is_running,
        "output_dir": DEFAULT_FLAT_DIR,
        "txt_count":  txt_count,
    })


# ── Boot ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"  Scraper UI  ->  http://localhost:{PORT}")
    print(f"  Flat output ->  {DEFAULT_FLAT_DIR}")
    print(f"  Work dir    ->  {WORK_DIR}")
    threading.Thread(
        target=lambda: (time.sleep(1.2), webbrowser.open(f"http://localhost:{PORT}")),
        daemon=True,
    ).start()
    app.run(port=PORT, debug=False, threaded=True)
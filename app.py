import os
import re
import json
import time
import math
import hashlib
import random
import logging
import threading
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse
from flask import Flask, render_template, request, jsonify, Response
import queue

import requests
from bs4 import BeautifulSoup

try:
    import tldextract
    HAS_TLDEXTRACT = True
except Exception:
    tldextract = None
    HAS_TLDEXTRACT = False
import chardet
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

# PDF parsing
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except Exception:
    PdfReader = None
    HAS_PYPDF = False

# Optional: best-in-class text extraction for messy HTML
try:
    import trafilatura  # type: ignore
    HAS_TRAFILATURA = True
except Exception:
    HAS_TRAFILATURA = False


app = Flask(__name__)

# Global state for tracking runs
active_runs = {}  # run_id -> run info dict (includes 'stop_flag' threading.Event)
event_queues = {}  # run_id -> queue of SSE events
run_history = []  # list of completed run summaries (newest first), capped at 50


# -----------------------------
# Config
# -----------------------------
@dataclass
class ScrapeConfig:
    output_dir: str = r"C:\Users\Temp\Documents\ChatGPTSc\ALL_TEXT_FILES"
    merged_txt_dirname: str = "merged"
    per_page_dirname: str = "pages"
    logs_dirname: str = "logs"
    save_per_page_files: bool = False

    max_pages_per_site: int = 50
    max_depth: int = 3
    max_seconds_per_site: int = 180
    max_bytes_per_response: int = 7_000_000
    allow_subdomains: bool = True 

    skip_url_patterns: tuple = (
        r"/wp-json",
        r"/xmlrpc\.php",
        r"/cdn-cgi/",
        r"/account",
        r"/login",
        r"/signup",
        r"/sign-in",
        r"/cart",
        r"/checkout",
        r"/privacy",
        r"/terms",
        r"/cookie",
        r"/careers/apply",
    )

    user_agent: str = "Mozilla/5.0 (compatible; WisniaCapitalScraper/1.0; +https://wisniacapital.co)"
    timeout_connect: int = 10
    timeout_read: int = 20
    verify_tls: bool = True

    per_domain_delay_range: tuple = (0.3, 0.6)
    max_workers: int = 6

    resume: bool = True
    run_id: str = ""


# -----------------------------
# Helpers
# -----------------------------
def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    return s[:max_len].strip("_") or "file"

def normalize_url(url: str):
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    u = re.sub(r"^mailto:", "", u, flags=re.I).strip()
    u = re.sub(r"^https?://https?://", "https://", u, flags=re.I)
    if not re.match(r"^https?://", u, flags=re.I):
        u = "https://" + u
    try:
        p = urlparse(u)
        if not p.netloc:
            return None
        p = p._replace(fragment="")
        path = re.sub(r"/{2,}", "/", p.path or "/")
        p = p._replace(path=path)
        return urlunparse(p)
    except Exception:
        return None

def get_registered_domain(url: str) -> str:
    if not HAS_TLDEXTRACT:
        host = get_hostname(url)
        parts = [part for part in host.split(".") if part]
        if len(parts) >= 2:
            return ".".join(parts[-2:]).lower()
        return host.lower()

    ext = tldextract.extract(url)
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return (ext.domain or "").lower()

def get_hostname(url: str) -> str:
    return (urlparse(url).netloc or "").lower()

def is_same_site(candidate_url: str, root_url: str, allow_subdomains: bool) -> bool:
    c_host = get_hostname(candidate_url)
    r_host = get_hostname(root_url)
    if not c_host or not r_host:
        return False
    if allow_subdomains:
        return get_registered_domain(candidate_url) == get_registered_domain(root_url)
    return c_host == r_host

def looks_like_binary(resp: requests.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ct:
        return False
    if any(x in ct for x in ["text/", "application/json", "application/xml", "application/xhtml+xml"]):
        return False
    return True

def should_skip_url(url: str, patterns: tuple) -> bool:
    for pat in patterns:
        if re.search(pat, url, flags=re.I):
            return True
    return False

# ISO 639-1 codes for the most common non-English languages on corporate sites.
# Matches /xx/ at start of path or as a path segment (e.g. /ar/, /fr/, /es/, /de/, /zh/).
_NON_ENGLISH_LANG_RE = re.compile(
    r"/(ar|fr|es|de|it|pt|nl|pl|ru|tr|zh|zh-cn|zh-tw|ja|ko|hi|sv|no|da|fi|cs|el|he|th|vi|id|ms|ro|hu|uk|bg|hr|sk|sl|et|lv|lt|sr|ca|eu|gl)(/|$)",
    re.I,
)

def is_non_english_path(url: str) -> bool:
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return False
    return bool(_NON_ENGLISH_LANG_RE.search(path))


# -----------------------------
# HTTP session with retries
# -----------------------------
class SessionFactory:
    def __init__(self, cfg: ScrapeConfig):
        self.cfg = cfg
        self.local = threading.local()

    def get(self) -> requests.Session:
        if getattr(self.local, "session", None) is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            })
            self.local.session = s
        return self.local.session

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def fetch_url(session: requests.Session, url: str, cfg: ScrapeConfig) -> requests.Response:
    try:
        resp = session.get(
            url,
            timeout=(cfg.timeout_connect, cfg.timeout_read),
            allow_redirects=True,
            verify=cfg.verify_tls,
            stream=True
        )
    except requests.exceptions.SSLError:
        # Some sites have broken/incomplete cert chains that browsers tolerate.
        # Retry once with verification disabled so we don't lose the page entirely.
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logging.warning("SSL verify failed for %s — retrying with verify=False", url)
        resp = session.get(
            url,
            timeout=(cfg.timeout_connect, cfg.timeout_read),
            allow_redirects=True,
            verify=False,
            stream=True
        )
    resp.raise_for_status()
    content = b""
    total = 0
    for chunk in resp.iter_content(chunk_size=128 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > cfg.max_bytes_per_response:
            raise requests.RequestException(f"Response too large (> {cfg.max_bytes_per_response} bytes): {url}")
        content += chunk
    resp._content = content
    return resp


# -----------------------------
# Extraction
# -----------------------------
def extract_text_from_html(html_bytes: bytes, base_url: str):
    discovered = []
    if HAS_TRAFILATURA:
        try:
            html_str = html_bytes.decode("utf-8", errors="ignore")
            extracted = trafilatura.extract(html_str, include_links=False, include_images=False) or ""
            soup = BeautifulSoup(html_str, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href")
                if href:
                    discovered.append(urljoin(base_url, href))
            cleaned = clean_text(extracted)
            return cleaned, discovered
        except Exception:
            pass

    enc = chardet.detect(html_bytes).get("encoding") or "utf-8"
    html_str = html_bytes.decode(enc, errors="ignore")
    soup = BeautifulSoup(html_str, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()
    for a in soup.select("a[href]"):
        href = a.get("href")
        if href:
            discovered.append(urljoin(base_url, href))
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    return clean_text(text), discovered

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    if not HAS_PYPDF:
        return ""

    try:
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return clean_text("\n\n".join(parts))
    except Exception:
        return ""

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


# -----------------------------
# Crawl
# -----------------------------
@dataclass
class PageResult:
    url: str
    status: str
    content_type: str
    bytes: int
    extracted_chars: int
    error: str = ""

@dataclass
class SiteResult:
    root_url: str
    site_key: str
    pages_attempted: int
    pages_saved: int
    total_chars: int
    duration_sec: float
    status: str
    errors: list


def crawl_site(root_url: str, cfg: ScrapeConfig, session_factory: SessionFactory, event_q: queue.Queue = None, stop_event: threading.Event = None):
    def emit(event_type, data):
        if event_q:
            event_q.put({"type": event_type, "data": data})

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    t0 = time.time()
    errors = []
    page_results = []

    root_url = normalize_url(root_url) or ""
    if not root_url:
        sr = SiteResult(root_url="", site_key="invalid", pages_attempted=0, pages_saved=0, total_chars=0,
                        duration_sec=0.0, status="invalid_url", errors=["Invalid root URL"])
        emit("site_done", asdict(sr))
        return sr, page_results

    site_key = safe_filename(get_registered_domain(root_url) or get_hostname(root_url) or sha1(root_url)[:10])
    os.makedirs(cfg.output_dir, exist_ok=True)

    merged_path = os.path.join(cfg.output_dir, f"{site_key}.txt")

    if cfg.resume and os.path.exists(merged_path) and os.path.getsize(merged_path) > 2000:
        sr = SiteResult(root_url=root_url, site_key=site_key, pages_attempted=0, pages_saved=0, total_chars=0,
                        duration_sec=0.0, status="skipped_existing", errors=[])
        emit("site_done", asdict(sr))
        return sr, page_results

    emit("site_start", {"site_key": site_key, "root_url": root_url})

    bfs_queue = [(root_url, 0)]
    seen = set()
    saved_texts = []

    def polite_delay():
        lo, hi = cfg.per_domain_delay_range
        time.sleep(random.uniform(lo, hi))

    while bfs_queue:
        if stopped():
            errors.append("Stopped by user")
            break
        if time.time() - t0 > cfg.max_seconds_per_site:
            errors.append("Site time limit exceeded")
            break

        url, depth = bfs_queue.pop(0)
        if depth > cfg.max_depth:
            continue

        url = normalize_url(url) or ""
        if not url or url in seen:
            continue
        seen.add(url)

        if should_skip_url(url, cfg.skip_url_patterns):
            continue
        if is_non_english_path(url):
            continue
        if not is_same_site(url, root_url, cfg.allow_subdomains):
            continue
        if len(page_results) >= cfg.max_pages_per_site:
            break

        polite_delay()
        sess = session_factory.get()

        try:
            resp = fetch_url(sess, url, cfg)
            ct = (resp.headers.get("Content-Type") or "").lower()
            content_len = len(resp.content)

            if looks_like_binary(resp) and "application/pdf" not in ct:
                page_results.append(PageResult(url=url, status="skipped_binary", content_type=ct, bytes=content_len, extracted_chars=0))
                continue

            extracted = ""
            discovered = []

            if "application/pdf" in ct or url.lower().endswith(".pdf"):
                extracted = extract_text_from_pdf(resp.content)
            else:
                extracted, discovered = extract_text_from_html(resp.content, url)

            extracted = extracted.strip()
            if extracted:
                saved_texts.append(f"URL: {url}\n\n{extracted}\n\n" + ("-" * 80) + "\n")
                pr = PageResult(url=url, status="ok", content_type=ct, bytes=content_len, extracted_chars=len(extracted))
                page_results.append(pr)
                emit("page_done", {"site_key": site_key, "url": url, "status": "ok", "chars": len(extracted)})
            else:
                page_results.append(PageResult(url=url, status="no_text", content_type=ct, bytes=content_len, extracted_chars=0))
                emit("page_done", {"site_key": site_key, "url": url, "status": "no_text", "chars": 0})

            for link in discovered:
                n = normalize_url(link)
                if not n:
                    continue
                if should_skip_url(n, cfg.skip_url_patterns):
                    continue
                if is_non_english_path(n):
                    continue
                if n not in seen and is_same_site(n, root_url, cfg.allow_subdomains):
                    boost = 0
                    path = (urlparse(n).path or "").lower()
                    if any(k in path for k in ["about", "team", "leadership", "portfolio", "projects", "services", "strategy", "invest", "company"]):
                        boost = -1
                    bfs_queue.append((n, depth + 1))
                    if boost < 0:
                        bfs_queue = bfs_queue[-1:] + bfs_queue[:-1]

        except requests.HTTPError as e:
            msg = f"HTTPError {getattr(e.response,'status_code',None)} for {url}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="http_error", content_type="", bytes=0, extracted_chars=0, error=msg))
            emit("page_done", {"site_key": site_key, "url": url, "status": "http_error", "chars": 0})
        except requests.RequestException as e:
            msg = f"RequestException for {url}: {str(e)[:240]}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="request_error", content_type="", bytes=0, extracted_chars=0, error=msg))
            emit("page_done", {"site_key": site_key, "url": url, "status": "request_error", "chars": 0})
        except Exception as e:
            msg = f"Unexpected error for {url}: {str(e)[:240]}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="error", content_type="", bytes=0, extracted_chars=0, error=msg))
            emit("page_done", {"site_key": site_key, "url": url, "status": "error", "chars": 0})

    merged_text = "\n".join(saved_texts).strip()
    if merged_text:
        with open(merged_path, "w", encoding="utf-8") as f:
            f.write(f"ROOT: {root_url}\n")
            f.write(f"SCRAPED_AT_UTC: {datetime.utcnow().isoformat()}Z\n")
            f.write(f"PAGES_INCLUDED: {sum(1 for pr in page_results if pr.status=='ok')}\n")
            f.write("\n" + ("=" * 80) + "\n\n")
            f.write(merged_text)

    duration = time.time() - t0
    if stopped():
        final_status = "stopped"
    elif merged_text:
        final_status = "ok"
    else:
        final_status = "no_output"
    sr = SiteResult(
        root_url=root_url,
        site_key=site_key,
        pages_attempted=len(page_results),
        pages_saved=sum(1 for pr in page_results if pr.status == "ok"),
        total_chars=len(merged_text),
        duration_sec=round(duration, 2),
        status=final_status,
        errors=errors[:20],
    )
    emit("site_done", asdict(sr))
    return sr, page_results


def write_jsonl(path: str, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def setup_run_logging(cfg: ScrapeConfig):
    """Set up per-run log file + master log. Returns (log_path, jsonl_path, master_jsonl_path)."""
    logs_dir = os.path.join(cfg.output_dir, cfg.logs_dirname)
    os.makedirs(logs_dir, exist_ok=True)

    log_path = os.path.join(logs_dir, f"scrape_run_{cfg.run_id}.log")
    jsonl_path = os.path.join(logs_dir, f"scrape_run_{cfg.run_id}.jsonl")
    master_jsonl_path = os.path.join(cfg.output_dir, "scrape_master_log.jsonl")

    # Set up file logger for this run
    logger = logging.getLogger(f"run_{cfg.run_id}")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)
    logger.info("Run started. run_id=%s output_dir=%s", cfg.run_id, cfg.output_dir)

    return log_path, jsonl_path, master_jsonl_path, logger


# -----------------------------
# Background runner
# -----------------------------
def run_scrape_job(run_id: str, sites: list, cfg: ScrapeConfig):
    event_q = event_queues[run_id]
    stop_event = active_runs[run_id]["stop_flag"]
    session_factory = SessionFactory(cfg)

    log_path, jsonl_path, master_jsonl_path, logger = setup_run_logging(cfg)

    # De-dupe
    uniq = {}
    for u in sites:
        n = normalize_url(u)
        if n:
            key = get_registered_domain(n) or get_hostname(n) or n
            if key not in uniq:
                uniq[key] = n

    target_urls = list(uniq.values())
    logger.info("Total input sites=%d | unique_sites=%d", len(sites), len(target_urls))
    event_q.put({"type": "run_info", "data": {"total_sites": len(target_urls), "run_id": run_id}})

    # Log run start to master
    write_jsonl(master_jsonl_path, {
        "type": "run_start",
        "run_id": run_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "sites_requested": len(sites),
        "sites_unique": len(target_urls),
        "target_urls": target_urls,
    })

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {ex.submit(crawl_site, u, cfg, session_factory, event_q, stop_event): u for u in target_urls}
        for fut in as_completed(futures):
            if stop_event.is_set():
                # Cancel anything not yet started; in-flight tasks will see the flag and exit early.
                for f in futures:
                    if not f.done():
                        f.cancel()
            root = futures[fut]
            try:
                site_result, page_results = fut.result()
                result_dict = asdict(site_result)
                results.append(result_dict)

                # Per-run JSONL
                write_jsonl(jsonl_path, {
                    "type": "site_result",
                    "run_id": run_id,
                    "site": result_dict,
                    "pages": [asdict(p) for p in page_results[:500]],
                })

                # Master JSONL
                write_jsonl(master_jsonl_path, {
                    "type": "site_result",
                    "run_id": run_id,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "site": result_dict,
                })

                logger.info(
                    "DONE %s | status=%s | saved=%d | attempted=%d | chars=%d | sec=%.2f",
                    site_result.site_key, site_result.status, site_result.pages_saved,
                    site_result.pages_attempted, site_result.total_chars, site_result.duration_sec
                )

            except Exception as e:
                msg = f"Fatal error for {root}: {str(e)[:300]}"
                logger.exception(msg)
                write_jsonl(jsonl_path, {"type": "fatal_error", "run_id": run_id, "root": root, "error": msg})
                write_jsonl(master_jsonl_path, {"type": "fatal_error", "run_id": run_id, "timestamp": datetime.utcnow().isoformat() + "Z", "root": root, "error": msg})
                event_q.put({"type": "error", "data": {"error": str(e)[:300]}})

    # Write summary CSV for this run
    if results:
        import pandas as pd
        summary_df = pd.DataFrame(results)
        summary_path = os.path.join(cfg.output_dir, cfg.logs_dirname, f"summary_{cfg.run_id}.csv")
        summary_df.to_csv(summary_path, index=False, encoding="utf-8")
        logger.info("Summary CSV: %s", summary_path)

    # Log run complete to master
    write_jsonl(master_jsonl_path, {
        "type": "run_complete",
        "run_id": run_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "total_sites": len(results),
        "total_pages_saved": sum(r.get("pages_saved", 0) for r in results),
        "total_chars": sum(r.get("total_chars", 0) for r in results),
    })

    logger.info("Run complete. %d sites processed.", len(results))
    logger.info("Run log: %s", log_path)
    logger.info("Run JSONL: %s", jsonl_path)
    logger.info("Master log: %s", master_jsonl_path)

    # Build history summary
    ok_count   = sum(1 for r in results if r.get("status") == "ok")
    err_count  = sum(1 for r in results if r.get("status") in ("error", "no_output", "invalid_url"))
    skip_count = sum(1 for r in results if r.get("status") == "skipped_existing")
    stop_count = sum(1 for r in results if r.get("status") == "stopped")
    was_stopped = active_runs.get(run_id, {}).get("stop_flag") and active_runs[run_id]["stop_flag"].is_set()

    history_entry = {
        "run_id": run_id,
        "started": active_runs.get(run_id, {}).get("started"),
        "finished": datetime.utcnow().isoformat() + "Z",
        "status": "stopped" if was_stopped else "complete",
        "total_sites": len(results),
        "saved": ok_count,
        "failed": err_count,
        "skipped": skip_count,
        "stopped": stop_count,
        "total_chars": sum(r.get("total_chars", 0) for r in results),
        "output_dir": cfg.output_dir,
    }
    run_history.insert(0, history_entry)
    del run_history[50:]

    if run_id in active_runs:
        active_runs[run_id]["status"] = "stopped" if was_stopped else "complete"
        # Keep entry around briefly so the UI can fetch final status; trim on next run.

    event_q.put({"type": "run_complete", "data": {"total": len(results), "results": results, "stopped": bool(was_stopped)}})
    event_q.put(None)  # sentinel


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def start_scrape():
    data = request.json
    sites_raw = data.get("sites", "")
    # Parse: split by whitespace, commas, newlines
    sites = re.split(r"[\s,]+", sites_raw.strip())
    sites = [s.strip() for s in sites if s.strip()]

    if not sites:
        return jsonify({"error": "No sites provided"}), 400

    run_id = str(uuid.uuid4())[:8]
    cfg = ScrapeConfig(
        output_dir=data.get("output_dir", "scraped_text"),
        max_pages_per_site=int(data.get("max_pages", 50)),
        max_depth=int(data.get("max_depth", 3)),
        max_seconds_per_site=int(data.get("max_seconds", 180)),
        max_workers=int(data.get("max_workers", 6)),
        resume=data.get("resume", True),
        run_id=run_id,
    )
    os.makedirs(cfg.output_dir, exist_ok=True)

    event_q = queue.Queue()
    event_queues[run_id] = event_q
    active_runs[run_id] = {
        "status": "running",
        "sites": sites,
        "started": datetime.utcnow().isoformat(),
        "stop_flag": threading.Event(),
        "output_dir": cfg.output_dir,
    }

    t = threading.Thread(target=run_scrape_job, args=(run_id, sites, cfg), daemon=True)
    t.start()

    return jsonify({"run_id": run_id, "sites_count": len(sites)})


@app.route("/api/stop/<run_id>", methods=["POST"])
def stop_run(run_id):
    run = active_runs.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run_id"}), 404
    run["stop_flag"].set()
    run["status"] = "stopping"
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/runs")
def list_runs():
    # Active + historical (newest first)
    active = [
        {
            "run_id": rid,
            "status": r.get("status"),
            "started": r.get("started"),
            "sites_requested": len(r.get("sites", [])),
            "output_dir": r.get("output_dir"),
            "active": True,
        }
        for rid, r in active_runs.items()
    ]
    return jsonify({"active": active, "history": run_history[:50]})


@app.route("/api/open_file", methods=["POST"])
def open_file():
    """Open a scraped .txt file in the OS default viewer."""
    data = request.json or {}
    path = data.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/events/<run_id>")
def stream_events(run_id):
    event_q = event_queues.get(run_id)
    if not event_q:
        return jsonify({"error": "Unknown run_id"}), 404

    def generate():
        while True:
            try:
                msg = event_q.get(timeout=120)
                if msg is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


if __name__ == "__main__":
    import webbrowser
    os.makedirs("templates", exist_ok=True)
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5001")).start()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)

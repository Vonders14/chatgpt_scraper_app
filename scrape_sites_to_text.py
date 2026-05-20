import os
import re
import json
import time
import math
import hashlib
import random
import logging
import argparse
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

import pandas as pd
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


# -----------------------------
# Config
# -----------------------------
@dataclass
class ScrapeConfig:
    # Input
    input_path: str
    website_column: str | None = None  # if None, will auto-detect

    # Output
    output_dir: str = "scraped_text"
    merged_txt_dirname: str = "merged"
    per_page_dirname: str = "pages"
    logs_dirname: str = "logs"
    save_per_page_files: bool = False

    # Crawl limits
    max_pages_per_site: int = 25
    max_depth: int = 2
    max_seconds_per_site: int = 90
    max_bytes_per_response: int = 7_000_000  # 7MB
    allow_subdomains: bool = True

    # Content filtering
    skip_url_patterns: tuple[str, ...] = (
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

    # Networking
    user_agent: str = "Mozilla/5.0 (compatible; WisniaCapitalScraper/1.0; +https://wisniacapital.co)"
    timeout_connect: int = 10
    timeout_read: int = 20
    verify_tls: bool = True  # set False only if you absolutely must

    # Politeness / stability
    per_domain_delay_range: tuple[float, float] = (0.4, 1.2)  # random jitter delay between requests
    max_workers: int = 6

    # Resume / bookkeeping
    resume: bool = True
    run_id: str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")


# -----------------------------
# Logging setup
# -----------------------------
def setup_logging(cfg: ScrapeConfig) -> tuple[str, str]:
    os.makedirs(cfg.output_dir, exist_ok=True)
    logs_dir = os.path.join(cfg.output_dir, cfg.logs_dirname)
    os.makedirs(logs_dir, exist_ok=True)

    log_path = os.path.join(logs_dir, f"scrape_run_{cfg.run_id}.log")
    jsonl_path = os.path.join(logs_dir, f"scrape_run_{cfg.run_id}.jsonl")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler()
        ],
    )
    logging.info("Run started. run_id=%s", cfg.run_id)
    return log_path, jsonl_path


# -----------------------------
# Helpers
# -----------------------------
def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    return s[:max_len].strip("_") or "file"

def normalize_url(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None

    # Fix common spreadsheet junk
    u = re.sub(r"^mailto:", "", u, flags=re.I).strip()
    u = re.sub(r"^https?://https?://", "https://", u, flags=re.I)

    # If scheme missing, assume https
    if not re.match(r"^https?://", u, flags=re.I):
        u = "https://" + u

    try:
        p = urlparse(u)
        if not p.netloc:
            return None
        # Remove fragments
        p = p._replace(fragment="")
        # Normalize path (keep as-is, but collapse //)
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
        # compare registrable domains
        return get_registered_domain(candidate_url) == get_registered_domain(root_url)
    return c_host == r_host

def looks_like_binary(resp: requests.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    # allow pdf separately
    if "application/pdf" in ct:
        return False
    # allow typical text
    if any(x in ct for x in ["text/", "application/json", "application/xml", "application/xhtml+xml"]):
        return False
    # unknown: sniff content
    return True

def should_skip_url(url: str, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        if re.search(pat, url, flags=re.I):
            return True
    return False

def detect_website_column(df: pd.DataFrame) -> str:
    # Strong guesses first
    candidates = [
        "website", "Website", "WEBSITE", "site", "Site", "url", "URL",
        "primary_website", "Primary Website", "company_website", "Company Website",
        "domain", "Domain", "homepage", "Homepage",
    ]
    for c in candidates:
        if c in df.columns:
            return c

    # Fallback: find first column containing many URL-ish values
    urlish = re.compile(r"(https?://|www\.|\.com\b|\.net\b|\.org\b|\.io\b|\.co\b)", re.I)
    best_col = None
    best_score = -1
    for col in df.columns:
        series = df[col].astype(str).fillna("")
        sample = series.head(200)
        score = sum(1 for v in sample if urlish.search(v or ""))
        if score > best_score:
            best_score = score
            best_col = col
    if best_col is None:
        raise ValueError("Could not detect a website column. Please pass --website_column.")
    return best_col

def read_input_table(path: str) -> pd.DataFrame:
    lower = path.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(path)
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return pd.read_excel(path)
    raise ValueError("Unsupported input type. Use .csv or .xlsx/.xls")


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
    # stream=True so we can enforce max_bytes
    resp = session.get(
        url,
        timeout=(cfg.timeout_connect, cfg.timeout_read),
        allow_redirects=True,
        verify=cfg.verify_tls,
        stream=True
    )
    resp.raise_for_status()
    # enforce max bytes
    content = b""
    total = 0
    for chunk in resp.iter_content(chunk_size=128 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > cfg.max_bytes_per_response:
            raise requests.RequestException(f"Response too large (> {cfg.max_bytes_per_response} bytes): {url}")
        content += chunk
    # attach the consumed content back (requests doesn't keep it when streamed)
    resp._content = content
    return resp


# -----------------------------
# Extraction
# -----------------------------
def extract_text_from_html(html_bytes: bytes, base_url: str) -> tuple[str, list[str]]:
    # Returns (text, discovered_links)
    discovered = []

    # Try trafilatura first if available
    if HAS_TRAFILATURA:
        try:
            html_str = html_bytes.decode("utf-8", errors="ignore")
            extracted = trafilatura.extract(html_str, include_links=False, include_images=False) or ""
            # Still parse links with bs4 for crawling
            soup = BeautifulSoup(html_str, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href")
                if href:
                    discovered.append(urljoin(base_url, href))
            cleaned = clean_text(extracted)
            return cleaned, discovered
        except Exception:
            pass

    # Fallback: BeautifulSoup main-ish text
    enc = chardet.detect(html_bytes).get("encoding") or "utf-8"
    html_str = html_bytes.decode(enc, errors="ignore")
    soup = BeautifulSoup(html_str, "lxml")

    # Remove junk tags
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()

    # Links
    for a in soup.select("a[href]"):
        href = a.get("href")
        if href:
            discovered.append(urljoin(base_url, href))

    # Prefer <main> then <article>, else body
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator="\n", strip=True)
    return clean_text(text), discovered

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    if not HAS_PYPDF:
        return ""

    try:
        reader = PdfReader(io_bytes_to_filelike(pdf_bytes))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return clean_text("\n\n".join(parts))
    except Exception:
        return ""

def io_bytes_to_filelike(b: bytes):
    # avoid importing io at top for clarity
    import io
    return io.BytesIO(b)

def clean_text(s: str) -> str:
    if not s:
        return ""
    # normalize whitespace
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # collapse huge blank runs
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    # remove repeated spaces
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
    error: str | None = None

@dataclass
class SiteResult:
    root_url: str
    site_key: str
    pages_attempted: int
    pages_saved: int
    total_chars: int
    duration_sec: float
    status: str
    errors: list[str]


def crawl_site(root_url: str, cfg: ScrapeConfig, session_factory: SessionFactory) -> tuple[SiteResult, list[PageResult]]:
    t0 = time.time()
    errors: list[str] = []
    page_results: list[PageResult] = []

    root_url = normalize_url(root_url) or ""
    if not root_url:
        sr = SiteResult(root_url="", site_key="invalid", pages_attempted=0, pages_saved=0, total_chars=0,
                        duration_sec=0.0, status="invalid_url", errors=["Invalid root URL"])
        return sr, page_results

    site_key = safe_filename(get_registered_domain(root_url) or get_hostname(root_url) or sha1(root_url)[:10])
    out_base = os.path.join(cfg.output_dir, site_key)
    merged_dir = os.path.join(out_base, cfg.merged_txt_dirname)
    pages_dir = os.path.join(out_base, cfg.per_page_dirname)
    os.makedirs(merged_dir, exist_ok=True)
    if cfg.save_per_page_files:
        os.makedirs(pages_dir, exist_ok=True)

    merged_path = os.path.join(merged_dir, f"{site_key}.txt")

    # Resume: if merged exists and non-trivial, skip
    if cfg.resume and os.path.exists(merged_path) and os.path.getsize(merged_path) > 2000:
        sr = SiteResult(root_url=root_url, site_key=site_key, pages_attempted=0, pages_saved=0, total_chars=0,
                        duration_sec=0.0, status="skipped_existing", errors=[])
        return sr, page_results

    # BFS crawl
    queue: list[tuple[str, int]] = [(root_url, 0)]
    seen: set[str] = set()
    saved_texts: list[str] = []

    def polite_delay():
        lo, hi = cfg.per_domain_delay_range
        time.sleep(random.uniform(lo, hi))

    while queue:
        # time guard
        if time.time() - t0 > cfg.max_seconds_per_site:
            errors.append("Site time limit exceeded")
            break

        url, depth = queue.pop(0)
        if depth > cfg.max_depth:
            continue

        url = normalize_url(url) or ""
        if not url or url in seen:
            continue
        seen.add(url)

        if should_skip_url(url, cfg.skip_url_patterns):
            continue

        if not is_same_site(url, root_url, cfg.allow_subdomains):
            continue

        if len(page_results) >= cfg.max_pages_per_site:
            break

        # Polite jitter delay
        polite_delay()

        sess = session_factory.get()

        try:
            resp = fetch_url(sess, url, cfg)
            ct = (resp.headers.get("Content-Type") or "").lower()
            content_len = len(resp.content)

            # If it looks like binary and not PDF, skip
            if looks_like_binary(resp) and "application/pdf" not in ct:
                page_results.append(PageResult(url=url, status="skipped_binary", content_type=ct, bytes=content_len, extracted_chars=0))
                continue

            extracted = ""
            discovered: list[str] = []

            if "application/pdf" in ct or url.lower().endswith(".pdf"):
                extracted = extract_text_from_pdf(resp.content)
            else:
                extracted, discovered = extract_text_from_html(resp.content, url)

            extracted = extracted.strip()
            if extracted:
                # Save (optional) per page
                if cfg.save_per_page_files:
                    fname = safe_filename(urlparse(url).path.strip("/").replace("/", "_") or "home")
                    page_path = os.path.join(pages_dir, f"{fname}_{sha1(url)[:10]}.txt")
                    with open(page_path, "w", encoding="utf-8") as f:
                        f.write(f"URL: {url}\n\n")
                        f.write(extracted)

                saved_texts.append(f"URL: {url}\n\n{extracted}\n\n" + ("-" * 80) + "\n")
                page_results.append(PageResult(url=url, status="ok", content_type=ct, bytes=content_len, extracted_chars=len(extracted)))
            else:
                page_results.append(PageResult(url=url, status="no_text", content_type=ct, bytes=content_len, extracted_chars=0))

            # Enqueue discovered links
            # Normalize + filter obvious junk early
            for link in discovered:
                n = normalize_url(link)
                if not n:
                    continue
                # skip anchors / mailto already stripped
                if should_skip_url(n, cfg.skip_url_patterns):
                    continue
                if n not in seen and is_same_site(n, root_url, cfg.allow_subdomains):
                    # prefer "about", "projects", "portfolio", "team", "services"
                    boost = 0
                    path = (urlparse(n).path or "").lower()
                    if any(k in path for k in ["about", "team", "leadership", "portfolio", "projects", "services", "strategy", "invest", "company"]):
                        boost = -1
                    queue.append((n, depth + 1))
                    # stable-ish ordering with heuristic boost
                    if boost < 0:
                        queue = queue[-1:] + queue[:-1]

        except requests.HTTPError as e:
            msg = f"HTTPError {getattr(e.response,'status_code',None)} for {url}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="http_error", content_type="", bytes=0, extracted_chars=0, error=msg))
            continue
        except requests.RequestException as e:
            msg = f"RequestException for {url}: {str(e)[:240]}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="request_error", content_type="", bytes=0, extracted_chars=0, error=msg))
            continue
        except Exception as e:
            msg = f"Unexpected error for {url}: {str(e)[:240]}"
            errors.append(msg)
            page_results.append(PageResult(url=url, status="error", content_type="", bytes=0, extracted_chars=0, error=msg))
            continue

    # Write merged output
    merged_text = "\n".join(saved_texts).strip()
    if merged_text:
        with open(merged_path, "w", encoding="utf-8") as f:
            f.write(f"ROOT: {root_url}\n")
            f.write(f"SCRAPED_AT_UTC: {datetime.utcnow().isoformat()}Z\n")
            f.write(f"PAGES_INCLUDED: {sum(1 for pr in page_results if pr.status=='ok')}\n")
            f.write("\n" + ("=" * 80) + "\n\n")
            f.write(merged_text)

    duration = time.time() - t0
    sr = SiteResult(
        root_url=root_url,
        site_key=site_key,
        pages_attempted=len(page_results),
        pages_saved=sum(1 for pr in page_results if pr.status == "ok"),
        total_chars=len(merged_text),
        duration_sec=round(duration, 2),
        status="ok" if merged_text else "no_output",
        errors=errors[:20],
    )
    return sr, page_results


# -----------------------------
# Orchestration
# -----------------------------
def write_jsonl(path: str, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def run(cfg: ScrapeConfig):
    log_path, jsonl_path = setup_logging(cfg)

    df = read_input_table(cfg.input_path)
    if df.empty:
        raise ValueError("Input file is empty.")

    website_col = cfg.website_column or detect_website_column(df)
    if website_col not in df.columns:
        raise ValueError(f"website_column '{website_col}' not found in file columns.")

    logging.info("Using website column: %s", website_col)

    # Build URL list
    urls: list[str] = []
    for v in df[website_col].tolist():
        u = normalize_url(str(v)) if pd.notna(v) else None
        if u:
            urls.append(u)

    # De-dupe by registrable domain
    uniq = {}
    for u in urls:
        key = get_registered_domain(u) or get_hostname(u) or u
        if key not in uniq:
            uniq[key] = u
    target_urls = list(uniq.values())
    logging.info("Total input rows=%d | urls_found=%d | unique_sites=%d", len(df), len(urls), len(target_urls))

    os.makedirs(cfg.output_dir, exist_ok=True)

    session_factory = SessionFactory(cfg)

    # Parallel crawl
    from concurrent.futures import ThreadPoolExecutor, as_completed

    summary_rows = []
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        futures = {ex.submit(crawl_site, u, cfg, session_factory): u for u in target_urls}
        for fut in as_completed(futures):
            root = futures[fut]
            try:
                site_result, page_results = fut.result()

                summary_rows.append(asdict(site_result))
                write_jsonl(jsonl_path, {
                    "type": "site_result",
                    "run_id": cfg.run_id,
                    "site": asdict(site_result),
                    "pages": [asdict(p) for p in page_results[:500]],  # cap
                })

                logging.info(
                    "DONE %s | status=%s | saved=%d | attempted=%d | chars=%d | sec=%.2f",
                    site_result.site_key, site_result.status, site_result.pages_saved,
                    site_result.pages_attempted, site_result.total_chars, site_result.duration_sec
                )
            except Exception as e:
                msg = f"Fatal error for {root}: {str(e)[:300]}"
                logging.exception(msg)
                write_jsonl(jsonl_path, {"type": "fatal_error", "run_id": cfg.run_id, "root": root, "error": msg})

    # Write summary CSV
    summary_df = pd.DataFrame(summary_rows)
    summary_path = os.path.join(cfg.output_dir, cfg.logs_dirname, f"summary_{cfg.run_id}.csv")
    summary_df.to_csv(summary_path, index=False, encoding="utf-8")
    logging.info("Summary written: %s", summary_path)
    logging.info("Run log: %s", log_path)
    logging.info("Run JSONL: %s", jsonl_path)


# -----------------------------
# CLI
# -----------------------------
def parse_args() -> ScrapeConfig:
    p = argparse.ArgumentParser(description="Scrape websites from CSV/Excel and save merged text files per site.")
    p.add_argument("--input", required=True, help="Path to input .csv or .xlsx")
    p.add_argument("--website_column", default=None, help="Column name containing website URLs (optional)")
    p.add_argument("--output_dir", default="scraped_text", help="Output directory")
    p.add_argument("--max_pages", type=int, default=25)
    p.add_argument("--max_depth", type=int, default=2)
    p.add_argument("--max_seconds_per_site", type=int, default=90)
    p.add_argument("--max_workers", type=int, default=6)
    p.add_argument("--save_per_page", action="store_true")
    p.add_argument("--no_resume", action="store_true")
    p.add_argument("--no_subdomains", action="store_true")
    p.add_argument("--insecure_tls", action="store_true", help="Disable TLS verification (not recommended)")

    args = p.parse_args()

    return ScrapeConfig(
        input_path=args.input,
        website_column=args.website_column,
        output_dir=args.output_dir,
        max_pages_per_site=args.max_pages,
        max_depth=args.max_depth,
        max_seconds_per_site=args.max_seconds_per_site,
        max_workers=args.max_workers,
        save_per_page_files=args.save_per_page,
        resume=(not args.no_resume),
        allow_subdomains=(not args.no_subdomains),
        verify_tls=(not args.insecure_tls),
    )


if __name__ == "__main__":
    cfg = parse_args()
    run(cfg)

    

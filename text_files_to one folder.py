import os
import shutil
from pathlib import Path
import re

# ========= CONFIG =========
SOURCE_ROOT = r"C:\Users\Temp\Documents\ChatGPTSc\scraped_text - Copy"
OUTPUT_DIR = r"C:\Users\Temp\Documents\ChatGPTSc\ALL_TEXT_FILES"
EXTENSIONS = {".txt"}
# ==========================


def safe_filename(name: str, max_len: int = 140) -> str:
    name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return name[:max_len].strip("_") or "file"


def extract_domain_from_path(src_path: Path, root: Path) -> str:
    """Extract domain name from file path."""
    # Try to get domain from parent folder name (most reliable)
    parent = src_path.parent.name
    # Check if parent looks like a domain (contains dot and common TLD)
    if re.search(r'\.(com|net|org|io|co|us|edu|gov|de|uk)$', parent, re.I):
        return parent
    
    # Try grandparent folder
    grandparent = src_path.parent.parent.name
    if re.search(r'\.(com|net|org|io|co|us|edu|gov|de|uk)$', grandparent, re.I):
        return grandparent
    
    # Fallback: extract from filename (remove .txt extension)
    filename = src_path.stem
    if re.search(r'\.(com|net|org|io|co|us|edu|gov|de|uk)$', filename, re.I):
        return filename
    
    # Last resort: use parent folder name as-is
    return parent if parent else "unknown"


def flatten_text_files():
    source_root = Path(SOURCE_ROOT)
    output_dir = Path(OUTPUT_DIR)

    output_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    seen_domains = {}  # Track duplicates

    for root, _, files in os.walk(source_root):
        # Skip logs folder
        if "logs" in root.lower():
            continue
            
        for file in files:
            ext = Path(file).suffix.lower()
            if ext not in EXTENSIONS:
                continue

            src_path = Path(root) / file
            
            # Extract domain name
            domain = extract_domain_from_path(src_path, source_root)
            domain = safe_filename(domain)
            
            # Handle duplicates
            if domain in seen_domains:
                seen_domains[domain] += 1
                new_name = f"{domain}_{seen_domains[domain]}.txt"
            else:
                seen_domains[domain] = 0
                new_name = f"{domain}.txt"
            
            dest_path = output_dir / new_name

            shutil.copy2(src_path, dest_path)
            copied += 1

    print(f"✅ Done. Copied {copied} text files into:")
    print(output_dir)


if __name__ == "__main__":
    flatten_text_files()
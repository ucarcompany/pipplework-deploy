"""Pipeline configuration."""
import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("PIPELINE_BASE", "/opt/pipplework"))
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
CLEANED_DIR = DATA_DIR / "cleaned"
REJECTED_DIR = DATA_DIR / "rejected"
DB_PATH = DATA_DIR / "pipeline.db"
FRONTEND_DIR = BASE_DIR / "frontend"

# Ensure directories exist
for d in [RAW_DIR, CLEANED_DIR, REJECTED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- Crawl settings ---
CRAWL_DELAY_RANGE = (8, 20)          # seconds between requests
MAX_CONCURRENT_DOWNLOADS = 2
REQUEST_TIMEOUT = 60                   # seconds
MAX_RETRIES = 3

# --- Cleaning thresholds ---
MIN_FILE_SIZE = 100                    # bytes
MAX_FILE_SIZE = 100 * 1024 * 1024     # 100 MB
MIN_FACE_COUNT = 10
MAX_FACE_COUNT = 2_000_000
MIN_VERTEX_COUNT = 4
DEGENERATE_AREA_THRESHOLD = 1e-10     # faces smaller than this are degenerate
MAX_DEGENERATE_RATIO = 0.3            # reject if >30% faces degenerate
DUPLICATE_CHECK = True

# --- Server ---
HOST = "127.0.0.1"
PORT = 9800

# --- User-Agent rotation ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

"""
Thingiverse crawler.

Strategy:
- Use curl_cffi with Chrome TLS impersonation to bypass Cloudflare
- Extract bearer token from page HTML for internal API calls
- Discover models via search/popular endpoints
- Download STL files from CDN
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from backend.config import RAW_DIR
from backend.crawler.engine import BaseCrawler, CrawlResult
from backend.models import CrawlSource

logger = logging.getLogger("crawler.thingiverse")

BASE_URL = "https://www.thingiverse.com"
API_URL = "https://api.thingiverse.com"


class ThingiverseCrawler(BaseCrawler):

    def __init__(self):
        super().__init__()
        self._bearer_token: str | None = None

    # --- Token extraction ---

    async def _extract_bearer_token(self) -> str | None:
        """Extract the internal API bearer token from Thingiverse page source."""
        result = await self.fetch(BASE_URL)
        if not result.success:
            logger.error(f"Failed to load Thingiverse homepage: {result.error}")
            return None

        html = result.data.text
        # Look for token patterns in page source
        patterns = [
            r'"access_token"\s*:\s*"([a-f0-9]{32,})"',
            r'token\s*[:=]\s*["\']([a-f0-9]{32,})["\']',
            r'bearer\s+([a-f0-9]{32,})',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                token = m.group(1)
                logger.info(f"Extracted Thingiverse bearer token: {token[:8]}...")
                return token

        # Try extracting from __NEXT_DATA__ or similar
        soup = BeautifulSoup(html, "lxml")
        for script in soup.find_all("script"):
            text = script.string or ""
            for pat in patterns:
                m = re.search(pat, text)
                if m:
                    return m.group(1)

        logger.warning("Could not extract Thingiverse bearer token")
        return None

    async def _api_headers(self) -> dict:
        if not self._bearer_token:
            self._bearer_token = await self._extract_bearer_token()
        headers = {
            "Accept": "application/json",
            "Referer": BASE_URL + "/",
            "Origin": BASE_URL,
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    # --- Discovery ---

    async def discover_models(self, query: str = "", limit: int = 10) -> list[dict]:
        """Discover models via API or page scraping."""
        models = []

        # Approach 1: Try internal API
        api_headers = await self._api_headers()
        if query:
            api_url = f"{API_URL}/search/{query}?type=things&per_page={limit}&sort=popular"
        else:
            api_url = f"{API_URL}/popular?per_page={limit}"

        result = await self.fetch_json(api_url, headers=api_headers)
        if result.success:
            hits = result.data if isinstance(result.data, list) else result.data.get("hits", [])
            for item in hits[:limit]:
                models.append({
                    "source_id": str(item.get("id", "")),
                    "name": item.get("name", "Unknown"),
                    "author": item.get("creator", {}).get("name", "") if isinstance(item.get("creator"), dict) else "",
                    "url": f"{BASE_URL}/thing:{item.get('id', '')}",
                    "thumbnail": item.get("thumbnail", ""),
                })
            if models:
                logger.info(f"Thingiverse API returned {len(models)} models")
                return models

        # Approach 2: Scrape search results page
        logger.info("Falling back to page scraping for Thingiverse")
        if query:
            page_url = f"{BASE_URL}/search?q={query}&type=things&sort=popular"
        else:
            page_url = f"{BASE_URL}/explore/popular"

        result = await self.fetch(page_url)
        if not result.success:
            logger.error(f"Thingiverse page scrape failed: {result.error}")
            return models

        soup = BeautifulSoup(result.data.text, "lxml")

        # Extract from __NEXT_DATA__
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                props = data.get("props", {}).get("pageProps", {})
                things = props.get("things", props.get("results", []))
                if isinstance(things, dict):
                    things = things.get("hits", things.get("things", []))
                for item in things[:limit]:
                    models.append({
                        "source_id": str(item.get("id", item.get("thing_id", ""))),
                        "name": item.get("name", "Unknown"),
                        "author": item.get("creator", {}).get("name", "") if isinstance(item.get("creator"), dict) else "",
                        "url": f"{BASE_URL}/thing:{item.get('id', item.get('thing_id', ''))}",
                        "thumbnail": item.get("thumbnail", ""),
                    })
            except json.JSONDecodeError:
                pass

        # Approach 3: Extract thing IDs from links
        if not models:
            thing_links = soup.find_all("a", href=re.compile(r"/thing:\d+"))
            seen_ids = set()
            for link in thing_links:
                m = re.search(r"/thing:(\d+)", link.get("href", ""))
                if m and m.group(1) not in seen_ids:
                    seen_ids.add(m.group(1))
                    models.append({
                        "source_id": m.group(1),
                        "name": link.get_text(strip=True) or f"Thing {m.group(1)}",
                        "author": "",
                        "url": f"{BASE_URL}/thing:{m.group(1)}",
                        "thumbnail": "",
                    })
                if len(models) >= limit:
                    break

        logger.info(f"Thingiverse scraping found {len(models)} models")
        return models

    # --- File download ---

    async def get_model_files(self, thing_id: str) -> list[dict]:
        """Get downloadable file list for a Thingiverse thing."""
        files = []

        # Try API first
        api_headers = await self._api_headers()
        result = await self.fetch_json(
            f"{API_URL}/things/{thing_id}/files",
            headers=api_headers,
        )
        if result.success and isinstance(result.data, list):
            for f in result.data:
                url = f.get("direct_url") or f.get("download_url") or f.get("public_url", "")
                if url:
                    files.append({
                        "id": str(f.get("id", "")),
                        "name": f.get("name", "file"),
                        "url": url,
                        "size": f.get("size", 0),
                    })
            if files:
                return files

        # Fallback: scrape thing files page
        result = await self.fetch(f"{BASE_URL}/thing:{thing_id}/files")
        if result.success:
            soup = BeautifulSoup(result.data.text, "lxml")
            # Find download links
            for a in soup.find_all("a", href=re.compile(r"download|\.stl|\.obj|\.3mf", re.I)):
                href = a.get("href", "")
                if href.startswith("/"):
                    href = BASE_URL + href
                fname = href.split("/")[-1].split("?")[0] or "model.stl"
                files.append({
                    "id": fname,
                    "name": fname,
                    "url": href,
                    "size": 0,
                })

        return files

    async def download_model(self, model_info: dict, job_id: str) -> CrawlResult:
        """Download all files for a model, return the primary file path."""
        thing_id = model_info["source_id"]
        model_dir = RAW_DIR / job_id / f"thingiverse_{thing_id}"
        model_dir.mkdir(parents=True, exist_ok=True)

        files = await self.get_model_files(thing_id)
        if not files:
            return CrawlResult(False, error="No downloadable files found")

        downloaded = []
        for f_info in files:
            # Prefer STL/OBJ/GLB files
            fname = f_info["name"].lower()
            if not any(fname.endswith(ext) for ext in ('.stl', '.obj', '.glb', '.gltf', '.3mf', '.ply')):
                continue
            dest = model_dir / f_info["name"]
            result = await self.download_file(f_info["url"], dest)
            if result.success:
                downloaded.append(result.data)

        if not downloaded:
            # Try downloading any file
            for f_info in files[:3]:
                dest = model_dir / f_info["name"]
                result = await self.download_file(f_info["url"], dest)
                if result.success:
                    downloaded.append(result.data)
                    break

        if downloaded:
            return CrawlResult(True, downloaded[0])  # Return first downloaded file
        return CrawlResult(False, error="All file downloads failed")

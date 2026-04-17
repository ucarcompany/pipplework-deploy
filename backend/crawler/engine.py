"""
Base crawler engine with anti-bot countermeasures.

Strategy:
1. Primary: curl_cffi with Chrome TLS fingerprint impersonation (JA3/JA4)
2. Fallback: nodriver (undetected Chromium via CDP) for JS-heavy pages
3. Rate limiting with random jitter to mimic human behaviour
4. Cookie persistence across requests
"""
from __future__ import annotations
import asyncio
import hashlib
import random
import logging
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests
from backend.config import (
    CRAWL_DELAY_RANGE, REQUEST_TIMEOUT, MAX_RETRIES, USER_AGENTS, RAW_DIR,
)

logger = logging.getLogger("crawler")


class CrawlResult:
    def __init__(self, success: bool, data: Any = None, error: str = ""):
        self.success = success
        self.data = data
        self.error = error


class BaseCrawler:
    """HTTP crawler with TLS fingerprint impersonation."""

    def __init__(self):
        self._session: cffi_requests.Session | None = None
        self._last_request_time = 0.0
        self._request_count = 0

    # --- Session management ---

    def _get_session(self) -> cffi_requests.Session:
        if self._session is None:
            self._session = cffi_requests.Session(
                impersonate="chrome124",
                timeout=REQUEST_TIMEOUT,
            )
        return self._session

    def close(self):
        if self._session:
            self._session.close()
            self._session = None

    # --- Rate limiting ---

    async def _rate_limit(self):
        """Enforce random delay between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        delay = random.uniform(*CRAWL_DELAY_RANGE)
        if elapsed < delay:
            wait = delay - elapsed
            logger.debug(f"Rate-limiting: waiting {wait:.1f}s")
            await asyncio.sleep(wait)
        self._last_request_time = time.monotonic()
        self._request_count += 1

    # --- Core HTTP methods ---

    async def fetch(self, url: str, headers: dict | None = None,
                    allow_redirects: bool = True, retries: int = MAX_RETRIES) -> CrawlResult:
        """Fetch a URL with TLS impersonation and retry logic."""
        await self._rate_limit()
        session = self._get_session()
        default_headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if headers:
            default_headers.update(headers)

        last_error = ""
        for attempt in range(retries):
            try:
                resp = session.get(
                    url,
                    headers=default_headers,
                    allow_redirects=allow_redirects,
                )
                if resp.status_code == 200:
                    return CrawlResult(True, resp)
                if resp.status_code == 403:
                    logger.warning(f"403 Forbidden on attempt {attempt+1}: {url}")
                    # Rotate impersonation on 403
                    browsers = ["chrome124", "chrome120", "chrome119", "safari17_0", "edge101"]
                    self._session = cffi_requests.Session(
                        impersonate=random.choice(browsers),
                        timeout=REQUEST_TIMEOUT,
                    )
                    session = self._session
                    await asyncio.sleep(random.uniform(15, 30))
                    continue
                if resp.status_code == 429:
                    wait = random.uniform(30, 60)
                    logger.warning(f"429 Rate-limited, backing off {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue
                last_error = f"HTTP {resp.status_code}"
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Request error attempt {attempt+1}: {e}")
                await asyncio.sleep(random.uniform(5, 15))

        return CrawlResult(False, error=last_error)

    async def fetch_json(self, url: str, headers: dict | None = None) -> CrawlResult:
        """Fetch and parse JSON response."""
        result = await self.fetch(url, headers=headers)
        if not result.success:
            return result
        try:
            data = result.data.json()
            return CrawlResult(True, data)
        except Exception as e:
            return CrawlResult(False, error=f"JSON parse error: {e}")

    async def download_file(self, url: str, dest_path: Path,
                            headers: dict | None = None) -> CrawlResult:
        """Download a binary file to disk."""
        await self._rate_limit()
        session = self._get_session()
        dl_headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": url.rsplit("/", 1)[0] + "/",
        }
        if headers:
            dl_headers.update(headers)

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"download_file attempt {attempt+1}: {url[:100]}")
                resp = session.get(url, headers=dl_headers, allow_redirects=True,
                                   timeout=120)
                if resp.status_code == 200 and len(resp.content) > 0:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_bytes(resp.content)
                    return CrawlResult(True, {
                        "path": str(dest_path),
                        "size": len(resp.content),
                        "content_type": resp.headers.get("content-type", ""),
                    })
                last_error = f"HTTP {resp.status_code}, body_len={len(resp.content)}"
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(random.uniform(5, 15))

        return CrawlResult(False, error=f"Download failed: {last_error}")


class CDPInterceptor:
    """
    Advanced: Use nodriver (undetected Chromium) + CDP to intercept
    WebGL binary data streams from JavaScript-rendered pages.
    Falls back gracefully if nodriver/Chromium unavailable.
    """

    @staticmethod
    async def capture_3d_assets(url: str, output_dir: Path, timeout: int = 45) -> list[dict]:
        """
        Open URL in headless Chromium, intercept network responses for 3D assets.
        Returns list of captured file info dicts.
        """
        captured = []
        try:
            import nodriver as uc
        except ImportError:
            logger.warning("nodriver not available, skipping CDP interception")
            return captured

        browser = None
        try:
            browser = await uc.start(headless=True)
            page = await browser.get(url)

            target_extensions = ('.glb', '.gltf', '.bin', '.stl', '.obj', '.fbx', '.3mf')
            target_mimes = ('application/octet-stream', 'model/gltf-binary', 'model/gltf+json')

            collected_urls = set()

            # Give page time to load and trigger XHR
            await asyncio.sleep(5)

            # Simulate scroll to trigger LOD loading
            await page.scroll_down(300)
            await asyncio.sleep(3)
            await page.scroll_down(300)
            await asyncio.sleep(3)

            # Extract all network requests from performance log
            resources = await page.evaluate("""
                () => {
                    return performance.getEntriesByType('resource')
                        .filter(e => e.name.match(/\\.(glb|gltf|bin|stl|obj|fbx|3mf)/i)
                                    || e.name.includes('model')
                                    || e.name.includes('download'))
                        .map(e => e.name);
                }
            """)

            if resources:
                for res_url in resources:
                    if res_url not in collected_urls:
                        collected_urls.add(res_url)
                        fname = res_url.split("/")[-1].split("?")[0]
                        dest = output_dir / fname
                        # Download via the crawler session
                        crawler = BaseCrawler()
                        result = await crawler.download_file(res_url, dest)
                        crawler.close()
                        if result.success:
                            captured.append(result.data)

        except Exception as e:
            logger.error(f"CDP interception error: {e}")
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass

        return captured

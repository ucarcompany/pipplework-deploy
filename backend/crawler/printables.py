"""
Printables (Prusa) crawler.

Strategy:
- Use curl_cffi with TLS impersonation
- Discover models via HTML scraping of listing pages
- Extract model file data from Svelte Kit hydration scripts
- Derive STL download URLs from preview file paths
- Download STL files from files.printables.com CDN
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

logger = logging.getLogger("crawler.printables")

BASE_URL = "https://www.printables.com"


class PrintablesCrawler(BaseCrawler):

    # --- Discovery ---

    async def discover_models(self, query: str = "", limit: int = 10) -> list[dict]:
        """Discover models from Printables."""
        models = []

        # Approach 1: Try the GraphQL API
        gql_result = await self._graphql_search(query, limit)
        if gql_result:
            return gql_result[:limit]

        # Approach 2: Scrape listing pages
        if query:
            url = f"{BASE_URL}/search/models?q={query}&ctx=models"
        else:
            url = f"{BASE_URL}/model"

        result = await self.fetch(url)
        if not result.success:
            logger.error(f"Printables page fetch failed: {result.error}")
            return models

        soup = BeautifulSoup(result.data.text, "lxml")

        # Extract from __NEXT_DATA__
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag and next_data_tag.string:
            try:
                data = json.loads(next_data_tag.string)
                props = data.get("props", {}).get("pageProps", {})
                # Navigate nested structure for model listings
                items = self._extract_models_from_props(props)
                for item in items[:limit]:
                    models.append(item)
            except json.JSONDecodeError:
                logger.warning("Failed to parse __NEXT_DATA__")

        # Approach 3: Extract from HTML links
        if not models:
            model_links = soup.find_all("a", href=re.compile(r"/model/\d+"))
            seen = set()
            for link in model_links:
                href = link.get("href", "")
                m = re.search(r"/model/(\d+)(?:-([^/\"]+))?", href)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    name = (m.group(2) or "").replace("-", " ").strip() or link.get_text(strip=True)
                    models.append({
                        "source_id": m.group(1),
                        "name": name or f"Model {m.group(1)}",
                        "author": "",
                        "url": f"{BASE_URL}/model/{m.group(1)}",
                        "thumbnail": "",
                    })
                if len(models) >= limit:
                    break

        logger.info(f"Printables discovered {len(models)} models")
        return models

    async def _graphql_search(self, query: str, limit: int) -> list[dict]:
        """Try Printables GraphQL API."""
        gql_url = f"{BASE_URL}/graphql/"
        gql_query = {
            "operationName": "PrintList",
            "variables": {
                "limit": limit,
                "offset": 0,
                "categoryId": None,
                "publishedDateLimitDays": None,
                "hasMake": False,
                "competitionAwarded": False,
                "ordering": "-download_count" if not query else "-relevance",
                "q": query or "",
            },
            "query": """
                query PrintList($limit: Int!, $offset: Int!, $q: String,
                                $categoryId: ID, $ordering: String) {
                    prints(limit: $limit, offset: $offset, query: $q,
                           categoryId: $categoryId, ordering: $ordering) {
                        items {
                            id
                            name
                            slug
                            user { publicUsername }
                            image { filePath }
                            stls { id name fileSize filePath }
                        }
                    }
                }
            """,
        }

        session = self._get_session()
        try:
            resp = session.post(
                gql_url,
                json=gql_query,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": f"{BASE_URL}/",
                    "Origin": BASE_URL,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", {}).get("prints", {}).get("items", [])
                models = []
                for item in items:
                    models.append({
                        "source_id": str(item["id"]),
                        "name": item.get("name", "Unknown"),
                        "author": item.get("user", {}).get("publicUsername", ""),
                        "url": f"{BASE_URL}/model/{item['id']}-{item.get('slug', '')}",
                        "thumbnail": item.get("image", {}).get("filePath", "") if item.get("image") else "",
                        "stls": item.get("stls", []),
                    })
                if models:
                    logger.info(f"Printables GraphQL returned {len(models)} models")
                return models
        except Exception as e:
            logger.debug(f"GraphQL search failed: {e}")

        return []

    def _extract_models_from_props(self, props: dict) -> list[dict]:
        """Extract model list from Next.js pageProps."""
        models = []

        # Try various paths
        for key in ("prints", "models", "results", "data"):
            items = props.get(key)
            if isinstance(items, dict):
                items = items.get("items", items.get("hits", []))
            if isinstance(items, list) and items:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id", ""))
                    if not mid:
                        continue
                    models.append({
                        "source_id": mid,
                        "name": item.get("name", f"Model {mid}"),
                        "author": item.get("user", {}).get("publicUsername", "")
                                  if isinstance(item.get("user"), dict) else "",
                        "url": f"{BASE_URL}/model/{mid}",
                        "thumbnail": "",
                    })
                break

        return models

    # --- File download ---

    # Regex that matches both numeric (406501) and UUID paths in media URLs
    _MEDIA_PATH_RE = re.compile(
        r'media/prints/[\w-]+/(?:stls|3mfs|slas|previews|gcodes)/[^"\\<>\s]+'
    )

    async def get_model_files(self, model_id: str) -> list[dict]:
        """Get downloadable files for a Printables model.

        Strategy (tries each until files are found):
        1. Parse Svelte Kit hydration scripts on the model page
        2. Regex-scan all script/HTML content for media CDN paths
        3. Fetch the /files subpage and parse its Svelte data
        4. Try GraphQL query for file data
        5. Derive candidate URLs from previewFile and verify with HEAD
        """
        files: list[dict] = []

        # --- STEP 1: Model detail page ---
        result = await self.fetch(f"{BASE_URL}/model/{model_id}")
        if not result.success:
            logger.warning(f"Model page fetch failed for {model_id}: {result.error}")
            return files

        html = result.data.text
        logger.debug(f"Model page {model_id}: status OK, len={len(html)}")
        # Check for Cloudflare challenge
        if len(html) < 5000 and ("challenge" in html.lower() or "cf-" in html.lower()):
            logger.warning(f"Model page {model_id} appears to be a Cloudflare challenge page")
            return files

        files = self._extract_files_from_html(html, model_id)
        if files:
            verified = await self._verify_urls(files)
            if verified:
                return verified

        # --- STEP 2: Try /files subpage ---
        logger.debug(f"Trying /files subpage for {model_id}")
        files_result = await self.fetch(f"{BASE_URL}/model/{model_id}/files")
        if files_result.success:
            files = self._extract_files_from_html(files_result.data.text, model_id)
            if files:
                verified = await self._verify_urls(files)
                if verified:
                    return verified

        # --- STEP 3: GraphQL query for file data ---
        logger.debug(f"Trying GraphQL for files of model {model_id}")
        gql_files = await self._graphql_files(model_id)
        if gql_files:
            return gql_files

        # --- STEP 4: Derive candidate URLs from previewFile + HEAD verify ---
        logger.debug(f"Trying URL derivation for model {model_id}")
        derived = self._derive_candidates_from_page(html, model_id)
        if derived:
            verified = await self._verify_urls(derived)
            if verified:
                return verified

        logger.warning(f"No files found for model {model_id}")
        return files

    async def _graphql_files(self, model_id: str) -> list[dict]:
        """Try GraphQL to get file list for a specific model."""
        gql_url = f"{BASE_URL}/graphql/"
        query = {
            "operationName": "PrintFiles",
            "variables": {"id": model_id},
            "query": """
                query PrintFiles($id: ID!) {
                    print(id: $id) {
                        id
                        stls { id name filePath fileSize filePreviewPath }
                        slas { id name filePath fileSize }
                        gcodes { id name filePath fileSize }
                    }
                }
            """
        }
        session = self._get_session()
        try:
            resp = session.post(
                gql_url, json=query,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": f"{BASE_URL}/model/{model_id}",
                    "Origin": BASE_URL,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                print_data = data.get("data", {}).get("print", {})
                files = []
                for key in ("stls", "slas", "gcodes"):
                    items = print_data.get(key, []) or []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        fi = self._extract_file_from_item(item)
                        if fi:
                            files.append(fi)
                if files:
                    logger.info(f"GraphQL returned {len(files)} file(s) for model {model_id}")
                    return files
        except Exception as e:
            logger.debug(f"GraphQL files query failed for {model_id}: {e}")
        return []

    async def _verify_urls(self, files: list[dict]) -> list[dict]:
        """Verify file URLs are accessible with HEAD requests."""
        verified = []
        session = self._get_session()
        for f in files:
            url = f.get("url", "")
            if not url:
                continue
            try:
                resp = session.head(url, timeout=10, allow_redirects=True,
                                    headers={"Referer": f"{BASE_URL}/"})
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    cl = int(resp.headers.get("content-length", "0") or "0")
                    # Accept 3D model types, octet-stream, or any non-HTML with size > 0
                    if "html" not in ct and (cl > 100 or "model" in ct or "octet" in ct):
                        if cl > 0:
                            f["size"] = cl
                        verified.append(f)
                        logger.debug(f"Verified URL: {url} ({ct}, {cl} bytes)")
                    else:
                        logger.debug(f"URL rejected (ct={ct}, cl={cl}): {url}")
                else:
                    logger.debug(f"URL returned {resp.status_code}: {url}")
            except Exception as e:
                logger.debug(f"URL verify error: {url} — {e}")
        return verified

    def _derive_candidates_from_page(self, html: str, model_id: str) -> list[dict]:
        """Generate candidate file URLs from page data and verify them."""
        candidates = []
        soup = BeautifulSoup(html, "lxml")

        # Extract model data to get previewFile info
        preview_path = ""
        model_name = ""
        model_slug = ""
        for script in soup.find_all("script"):
            text = script.string or ""
            if '"body"' not in text or len(text) < 200:
                continue
            try:
                envelope = json.loads(text)
                body_str = envelope.get("body", "")
                if isinstance(body_str, str) and body_str:
                    body = json.loads(body_str)
                    model = body.get("data", {}).get("model", {})
                    if model and model.get("id"):
                        pf = model.get("previewFile") or {}
                        preview_path = pf.get("filePreviewPath", "")
                        model_name = model.get("name", "")
                        model_slug = model.get("slug", "")
                        break
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        if not preview_path:
            return candidates

        # For paths like media/prints/{uuid}/previews/{uuid}.png
        # Try replacing previews/ with stls/ and .png with various 3D extensions
        m = re.match(r'(media/prints/[\w-]+)/previews/([\w-]+)\.\w+', preview_path)
        if m:
            base_path = m.group(1)
            file_uuid = m.group(2)
            # Try various patterns
            for ext in ('.stl', '.3mf', '.obj'):
                for subdir in ('stls', '3mfs'):
                    url = f"https://files.printables.com/{base_path}/{subdir}/{file_uuid}{ext}"
                    candidates.append({
                        "id": "", "name": f"model{ext}",
                        "url": url, "size": 0,
                    })
            # Also try with model name in the filename
            if model_slug:
                safe_name = re.sub(r'[^\w-]', '-', model_slug)[:50]
                for ext in ('.stl', '.3mf'):
                    url = f"https://files.printables.com/{base_path}/stls/{file_uuid}/{safe_name}{ext}"
                    candidates.append({
                        "id": "", "name": f"{safe_name}{ext}",
                        "url": url, "size": 0,
                    })

        return candidates

    def _extract_files_from_html(self, html: str, model_id: str) -> list[dict]:
        """Extract downloadable file info from any Printables page HTML."""
        files: list[dict] = []
        soup = BeautifulSoup(html, "lxml")

        # ---- Method A: Svelte Kit hydration scripts ----
        for script in soup.find_all("script"):
            text = script.string or ""
            if len(text) < 100:
                continue

            # Look for Svelte data envelope: {"status":200,"body":"..."}
            if '"body"' in text:
                try:
                    envelope = json.loads(text)
                    body_str = envelope.get("body", "")
                    if isinstance(body_str, str) and body_str:
                        body = json.loads(body_str)
                        data_obj = body.get("data", {})
                        model = data_obj.get("model", {})
                        if model and model.get("id"):
                            files = self._extract_from_model_data(model, model_id)
                            if files:
                                return files
                        # /files page might have file list directly in data
                        for key in ("stls", "slas", "files", "modelFiles", "printFiles"):
                            fl = data_obj.get(key, [])
                            if isinstance(fl, list):
                                for f in fl:
                                    if not isinstance(f, dict):
                                        continue
                                    file_info = self._extract_file_from_item(f)
                                    if file_info:
                                        files.append(file_info)
                        if files:
                            logger.info(f"Found {len(files)} file(s) from Svelte data for {model_id}")
                            return files
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

        # ---- Method B: Regex scan ALL page content for media paths ----
        seen_urls = set()
        for m in self._MEDIA_PATH_RE.finditer(html):
            path = m.group()
            # Direct STL/3MF/OBJ reference
            if re.search(r'\.(stl|3mf|obj)$', path, re.I):
                url = f"https://files.printables.com/{path}"
                if url not in seen_urls:
                    seen_urls.add(url)
                    files.append({
                        "id": "", "name": path.split("/")[-1],
                        "url": url, "size": 0,
                    })
            # Preview path → derive STL
            derived = self._derive_stl_from_preview(path)
            if derived and derived["url"] not in seen_urls:
                seen_urls.add(derived["url"])
                files.append(derived)

        if files:
            logger.info(f"Found {len(files)} file(s) via regex scan for {model_id}")
            return files

        # ---- Method C: Direct <a> links ----
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(
                r'(?:files|media)\.printables\.com.*\.(stl|obj|3mf|glb|gltf)',
                href, re.I,
            ):
                fname = href.split("/")[-1].split("?")[0]
                files.append({
                    "id": fname, "name": fname, "url": href, "size": 0,
                })

        return files

    def _extract_from_model_data(self, model: dict, model_id: str) -> list[dict]:
        """Extract download URLs from parsed Svelte model JSON."""
        files: list[dict] = []

        # 1. Check stls/slas/files arrays if present (preferred — has direct filePath)
        for key in ("stls", "slas", "files", "gcodes"):
            items = model.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                file_info = self._extract_file_from_item(item)
                if file_info and file_info["url"] not in {f["url"] for f in files}:
                    files.append(file_info)

        if files:
            return files

        # 2. previewFile → derive STL (works for old-format models)
        preview = model.get("previewFile") or {}
        preview_path = preview.get("filePreviewPath", "")
        if preview_path:
            logger.debug(f"Model {model_id} previewFile path: {preview_path}")
            derived = self._derive_stl_from_preview(preview_path)
            if derived:
                files.append(derived)
                logger.info(f"Derived STL from previewFile for model {model_id}")
                return files

        # 3. Deep scan model JSON for any media paths with direct file extensions
        model_json = json.dumps(model)
        seen = set()
        for m in self._MEDIA_PATH_RE.finditer(model_json):
            path = m.group()
            if re.search(r'\.(stl|3mf|obj)$', path, re.I):
                url = f"https://files.printables.com/{path}"
                if url not in seen:
                    seen.add(url)
                    files.append({"id": "", "name": path.split("/")[-1], "url": url, "size": 0})
            derived = self._derive_stl_from_preview(path)
            if derived and derived["url"] not in seen:
                seen.add(derived["url"])
                files.append(derived)

        return files

    @staticmethod
    def _extract_file_from_item(item: dict) -> dict | None:
        """Extract a download URL from a file-info dict."""
        url = (
            item.get("filePath")
            or item.get("downloadUrl")
            or item.get("url")
            or item.get("directUrl")
            or ""
        )
        if not url:
            # Try to derive from filePreviewPath
            preview = item.get("filePreviewPath", "")
            if preview and "/stls/" in preview:
                if "_preview" in preview:
                    stl_path = re.sub(r'_preview\.\w+$', '.stl', preview)
                elif re.search(r'\.(png|jpg|jpeg|webp|gif)$', preview, re.I):
                    stl_path = re.sub(r'\.\w+$', '.stl', preview)
                else:
                    return None
                url = f"https://files.printables.com/{stl_path}"
            else:
                return None

        if not url.startswith("http"):
            url = f"https://files.printables.com/{url}"
        name = item.get("name", item.get("fileName", url.split("/")[-1]))
        if not name:
            name = "model.stl"
        return {"id": str(item.get("id", "")), "name": name, "url": url, "size": item.get("fileSize", 0)}

    @staticmethod
    def _derive_stl_from_preview(path: str) -> dict | None:
        """Derive an STL download URL from a media path.

        Handles old-format paths where preview is co-located with STL:
          media/prints/406501/stls/3380960_.../file_preview.png → .stl
        """
        if not path:
            return None

        # Only process paths in stls/ directory (old format co-location)
        if "/stls/" not in path:
            return None

        # Already a .stl file — use directly
        if path.lower().endswith('.stl'):
            url = f"https://files.printables.com/{path}"
            return {"id": "", "name": path.split("/")[-1], "url": url, "size": 0}

        # Strip _preview.{ext} → .stl
        stl_path = re.sub(r'_preview\.\w+$', '.stl', path)
        if stl_path != path:
            url = f"https://files.printables.com/{stl_path}"
            name = stl_path.split("/")[-1]
            return {"id": "", "name": name, "url": url, "size": 0}

        # Strip any image extension → .stl (for non-preview images in stls dir)
        if re.search(r'\.(png|jpg|jpeg|webp|gif)$', path, re.I):
            stl_path = re.sub(r'\.\w+$', '.stl', path)
            url = f"https://files.printables.com/{stl_path}"
            name = stl_path.split("/")[-1]
            return {"id": "", "name": name, "url": url, "size": 0}

        return None

    async def download_model(self, model_info: dict, job_id: str) -> CrawlResult:
        """Download the primary 3D file for a model."""
        model_id = model_info["source_id"]
        model_dir = RAW_DIR / job_id / f"printables_{model_id}"
        model_dir.mkdir(parents=True, exist_ok=True)

        # If STL info was included from GraphQL discovery
        stls = model_info.get("stls", [])
        files = []
        if stls:
            for stl in stls:
                file_path = stl.get("filePath", "")
                if file_path:
                    url = file_path if file_path.startswith("http") else f"https://files.printables.com/{file_path}"
                    files.append({
                        "name": stl.get("name", "model.stl"),
                        "url": url,
                    })

        if not files:
            files = await self.get_model_files(model_id)

        if not files:
            return CrawlResult(False, error="No downloadable files found")

        # Download the first valid 3D file
        valid_exts = ('.stl', '.obj', '.3mf', '.glb', '.gltf', '.ply')
        for f_info in files:
            url = f_info.get("url", "")
            if not url:
                continue
            fname = f_info.get("name", "model.stl")
            if not any(fname.lower().endswith(e) for e in valid_exts):
                fname += ".stl"
            dest = model_dir / fname
            result = await self.download_file(
                url, dest,
                headers={"Referer": f"{BASE_URL}/model/{model_id}"}
            )
            if result.success:
                # Verify downloaded content is a valid 3D file (not HTML error page)
                content = dest.read_bytes()
                if len(content) < 200 and b'<html' in content.lower():
                    logger.warning(f"Downloaded content for {model_id} is HTML, not 3D file")
                    dest.unlink(missing_ok=True)
                    continue
                return result

        return CrawlResult(False, error="All file downloads failed")

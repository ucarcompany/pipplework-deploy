"""
Printables (Prusa) crawler.

Strategy:
- Use curl_cffi with TLS impersonation
- Discover models via GraphQL at api.printables.com
- Get file IDs via GraphQL query
- Get signed download URLs via getDownloadLink mutation
- Download STL files from files.printables.com CDN
"""
from __future__ import annotations
import asyncio
import json
import logging
import random
import re
from pathlib import Path

from bs4 import BeautifulSoup

from backend.config import RAW_DIR, MAX_FILE_SIZE
from backend.crawler.engine import BaseCrawler, CrawlResult

logger = logging.getLogger("crawler.printables")

BASE_URL = "https://www.printables.com"
API_GQL = "https://api.printables.com/graphql/"


class PrintablesCrawler(BaseCrawler):

    # ---------- helpers ----------

    def _gql_headers(self, model_id: str = "") -> dict:
        ref = f"{BASE_URL}/model/{model_id}" if model_id else f"{BASE_URL}/"
        return {
            "Origin": BASE_URL,
            "Referer": ref,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _gql_post(self, payload: dict, model_id: str = "") -> dict | None:
        """Send a GraphQL request to api.printables.com and return parsed data."""
        session = self._get_session()
        try:
            resp = session.post(API_GQL, json=payload,
                                headers=self._gql_headers(model_id), timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data"):
                    return data["data"]
                if data.get("errors"):
                    logger.debug(f"GraphQL errors: {data['errors']}")
        except Exception as e:
            logger.debug(f"GraphQL request failed: {e}")
        return None

    # ---------- Discovery ----------

    async def discover_models(self, query: str = "", limit: int = 10) -> list[dict]:
        """Discover models from Printables via GraphQL."""
        models: list[dict] = []

        # Primary: GraphQL search at api.printables.com
        gql = {
            "operationName": "PrintList",
            "variables": {
                "limit": limit,
                "offset": 0,
                "categoryId": None,
                "ordering": "-download_count" if not query else "-relevance",
                "q": query or "",
            },
            "query": """
                query PrintList($limit: Int!, $offset: Int!, $q: String,
                                $categoryId: ID, $ordering: String) {
                    prints(limit: $limit, offset: $offset, query: $q,
                           categoryId: $categoryId, ordering: $ordering) {
                        items {
                            id name slug
                            user { publicUsername }
                            image { filePath }
                            stls { id name fileSize }
                        }
                    }
                }
            """,
        }
        data = self._gql_post(gql)
        if data:
            items = data.get("prints", {}).get("items", [])
            for item in items:
                models.append({
                    "source_id": str(item["id"]),
                    "name": item.get("name", "Unknown"),
                    "author": (item.get("user") or {}).get("publicUsername", ""),
                    "url": f"{BASE_URL}/model/{item['id']}-{item.get('slug', '')}",
                    "thumbnail": (item.get("image") or {}).get("filePath", ""),
                    "stls": item.get("stls") or [],
                })

        # Fallback: scrape listing page for model links
        if not models:
            url = f"{BASE_URL}/search/models?q={query}&ctx=models" if query else f"{BASE_URL}/model"
            result = await self.fetch(url)
            if result.success:
                soup = BeautifulSoup(result.data.text, "lxml")
                seen: set[str] = set()
                for link in soup.find_all("a", href=re.compile(r"/model/\d+")):
                    m = re.search(r"/model/(\d+)(?:-([^/\"]+))?", link.get("href", ""))
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
        return models[:limit]

    # ---------- File listing ----------

    async def get_model_files(self, model_id: str) -> list[dict]:
        """Get downloadable files for a model.

        1. GraphQL query to get STL file IDs
        2. GetDownloadLink mutation to obtain signed CDN URLs
        """
        # Step 1: get file IDs
        stl_items = self._query_stl_ids(model_id)
        if not stl_items:
            logger.warning(f"No STL IDs found for model {model_id}")
            return []

        # Step 2: get signed download URLs via mutation
        files = self._get_download_links(model_id, stl_items)
        if files:
            return files

        logger.warning(f"GetDownloadLink failed for model {model_id}")
        return []

    def _query_stl_ids(self, model_id: str) -> list[dict]:
        """Query GraphQL for STL file metadata (id, name, fileSize)."""
        gql = {
            "operationName": "PrintFiles",
            "variables": {"id": model_id},
            "query": """
                query PrintFiles($id: ID!) {
                    print(id: $id) {
                        id
                        stls { id name fileSize }
                        slas { id name fileSize }
                    }
                }
            """,
        }
        data = self._gql_post(gql, model_id)
        if not data:
            return []
        print_data = data.get("print") or {}
        items: list[dict] = []
        for key in ("stls", "slas"):
            for item in (print_data.get(key) or []):
                if isinstance(item, dict) and item.get("id"):
                    items.append(item)
        if items:
            logger.info(f"Model {model_id}: found {len(items)} file(s) via GraphQL")
        return items

    def _get_download_links(self, model_id: str, stl_items: list[dict]) -> list[dict]:
        """Call getDownloadLink mutation to get signed CDN URLs.

        Requests one file at a time to maximise success rate.
        """
        files: list[dict] = []
        for idx, item in enumerate(stl_items):
            name = item.get("name", "model.stl")

            # Determine file type for the mutation
            lower = name.lower()
            if lower.endswith(".sla"):
                file_type = "sla"
            elif lower.endswith(".gcode"):
                file_type = "gcode"
            else:
                file_type = "stl"  # default to stl for .stl, .3mf, .obj, etc.

            gql = {
                "operationName": "GetDownloadLink",
                "variables": {
                    "printId": model_id,
                    "source": "model_detail",
                    "files": [{"fileType": file_type, "ids": [str(item["id"])]}],
                },
                "query": """
                    mutation GetDownloadLink($printId: ID!, $source: DownloadSourceEnum!,
                                            $files: [DownloadFileInput!]) {
                        getDownloadLink(printId: $printId, source: $source, files: $files) {
                            ok
                            output {
                                link ttl count
                                files { id link ttl fileType }
                            }
                        }
                    }
                """,
            }
            data = self._gql_post(gql, model_id)
            if not data:
                logger.warning(f"getDownloadLink: no response for file {item['id']} ({name})")
                continue
            dl = data.get("getDownloadLink") or {}
            if not dl.get("ok"):
                logger.warning(f"getDownloadLink ok=false for file {item['id']} ({name})")
                continue
            output = dl.get("output") or {}
            for f in (output.get("files") or []):
                link = f.get("link", "")
                if link:
                    files.append({
                        "id": str(f.get("id", item["id"])),
                        "name": name,
                        "url": link,
                        "size": item.get("fileSize", 0),
                    })
                    logger.info(f"Got download link for {name} (file {item['id']})")

            # Small delay between mutation calls to avoid rate-limiting
            if idx < len(stl_items) - 1:
                import time
                time.sleep(random.uniform(0.5, 1.5))

        if files:
            logger.info(f"Model {model_id}: obtained {len(files)} signed download URL(s)")
        return files

    async def download_model(self, model_info: dict, job_id: str) -> CrawlResult:
        """Download the primary 3D file for a model."""
        model_id = model_info["source_id"]
        model_dir = RAW_DIR / job_id / f"printables_{model_id}"
        model_dir.mkdir(parents=True, exist_ok=True)

        # If STL info was included from GraphQL discovery, use it for IDs
        stls = model_info.get("stls") or []
        files: list[dict] = []
        if stls:
            files = self._get_download_links(model_id, stls)

        if not files:
            files = await self.get_model_files(model_id)

        if not files:
            # Last resort: try pack download
            pack_result = await self._try_pack_download(model_id, model_dir)
            if pack_result:
                return pack_result
            return CrawlResult(False, error="No downloadable files found")

        # Download the first valid 3D file (prefer smaller files)
        valid_exts = ('.stl', '.obj', '.3mf', '.glb', '.gltf', '.ply')
        # Sort by file size ascending so we try smaller files first
        files_sorted = sorted(files, key=lambda f: f.get("size", 0) or 0)
        for idx, f_info in enumerate(files_sorted):
            url = f_info.get("url", "")
            if not url:
                continue
            fsize = f_info.get("size", 0) or 0
            if fsize > MAX_FILE_SIZE:
                logger.info(f"Model {model_id}: skipping '{f_info.get('name')}' ({fsize/1024/1024:.1f} MB > limit)")
                continue
            url = f_info.get("url", "")
            if not url:
                continue
            fname = f_info.get("name", "model.stl")
            if not any(fname.lower().endswith(e) for e in valid_exts):
                fname += ".stl"
            dest = model_dir / fname
            logger.info(f"Model {model_id}: downloading file {idx+1}/{len(files)} '{fname}' ...")
            result = await self.download_file(url, dest)
            if result.success:
                content = dest.read_bytes()
                if len(content) < 200 and b'<html' in content.lower():
                    logger.warning(f"Downloaded HTML instead of 3D file for {model_id}")
                    dest.unlink(missing_ok=True)
                    continue
                logger.info(f"Model {model_id}: downloaded '{fname}' ({len(content)} bytes)")
                return result
            else:
                logger.warning(f"Model {model_id}: download failed for '{fname}': {result.error}")

        # All individual downloads failed — try pack download
        pack_result = await self._try_pack_download(model_id, model_dir)
        if pack_result:
            return pack_result

        return CrawlResult(False, error="All file downloads failed")

    async def _try_pack_download(self, model_id: str, model_dir: Path) -> CrawlResult | None:
        """Try downloading the model as a ZIP pack."""
        logger.info(f"Model {model_id}: trying pack (ZIP) download as fallback ...")
        gql = {
            "operationName": "GetDownloadLink",
            "variables": {
                "printId": model_id,
                "source": "model_detail",
                "fileType": "pack",
            },
            "query": """
                mutation GetDownloadLink($printId: ID!, $source: DownloadSourceEnum!,
                                        $fileType: DownloadFileTypeEnum) {
                    getDownloadLink(printId: $printId, source: $source, fileType: $fileType) {
                        ok
                        output {
                            link ttl
                        }
                    }
                }
            """,
        }
        data = self._gql_post(gql, model_id)
        if not data:
            return None
        dl = data.get("getDownloadLink") or {}
        if not dl.get("ok"):
            logger.warning(f"Model {model_id}: pack download link failed (ok=false)")
            return None
        link = (dl.get("output") or {}).get("link", "")
        if not link:
            return None

        dest = model_dir / f"printables_{model_id}_pack.zip"
        logger.info(f"Model {model_id}: downloading pack from CDN ...")
        result = await self.download_file(link, dest)
        if result.success:
            # Extract first STL from the ZIP
            try:
                import zipfile
                with zipfile.ZipFile(dest) as zf:
                    stl_names = [n for n in zf.namelist()
                                 if n.lower().endswith(('.stl', '.obj', '.3mf'))]
                    if stl_names:
                        extracted = model_dir / Path(stl_names[0]).name
                        with zf.open(stl_names[0]) as src, open(extracted, 'wb') as dst:
                            dst.write(src.read())
                        size = extracted.stat().st_size
                        dest.unlink(missing_ok=True)  # remove zip
                        logger.info(f"Model {model_id}: extracted '{extracted.name}' from pack ({size} bytes)")
                        return CrawlResult(True, {
                            "path": str(extracted),
                            "size": size,
                            "content_type": "model/stl",
                        })
            except Exception as e:
                logger.warning(f"Model {model_id}: pack extraction failed: {e}")
        return None

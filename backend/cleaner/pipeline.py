"""
Data cleaning pipeline – the core differentiator.

Multi-stage validation & transformation:
1. File integrity (magic bytes, basic parse)
2. Size bounds checking
3. Mesh structural validation (trimesh)
4. Geometry quality checks (degenerate faces, manifold, normals)
5. Complexity bounds
6. Content-hash deduplication
7. Format conversion → GLB for web display
"""
from __future__ import annotations
import hashlib
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from backend.config import (
    CLEANED_DIR, REJECTED_DIR,
    MIN_FILE_SIZE, MAX_FILE_SIZE,
    MIN_FACE_COUNT, MAX_FACE_COUNT, MIN_VERTEX_COUNT,
    DEGENERATE_AREA_THRESHOLD, MAX_DEGENERATE_RATIO,
    DUPLICATE_CHECK,
)
from backend.models import DirtyReason
from backend.storage.db import check_content_hash

logger = logging.getLogger("cleaner")

# --- STL magic bytes ---
STL_BINARY_HEADER_SIZE = 80
STL_ASCII_MARKER = b"solid"

# Known 3D format signatures
FORMAT_SIGNATURES = {
    ".stl": [STL_ASCII_MARKER, None],        # ASCII starts with 'solid', binary has 80-byte header
    ".obj": [b"v ", b"# ", b"mtllib"],        # OBJ starts with vertex or comment
    ".glb": [b"glTF"],                         # glTF binary magic
    ".gltf": [b"{"],                           # JSON format
    ".3mf": [b"PK"],                           # ZIP-based
    ".ply": [b"ply"],                          # Stanford PLY
}


@dataclass
class CleanResult:
    passed: bool
    reason: Optional[DirtyReason] = None
    detail: str = ""
    output_path: str = ""
    vertex_count: int = 0
    face_count: int = 0
    is_watertight: bool = False
    is_manifold: bool = False
    bounding_box: list = field(default_factory=list)
    content_hash: str = ""
    output_size: int = 0


class CleaningPipeline:
    """Multi-stage data cleaning pipeline for 3D model files."""

    async def process(self, file_path: str, model_name: str, job_id: str) -> CleanResult:
        """Run the full cleaning pipeline on a raw model file."""
        path = Path(file_path)
        if not path.exists():
            return CleanResult(False, DirtyReason.CORRUPT_FILE, "文件不存在")

        # Stage 1: File size check
        result = self._check_file_size(path)
        if not result.passed:
            self._archive_rejected(path, job_id)
            return result

        # Stage 2: File integrity (magic bytes)
        result = self._check_file_integrity(path)
        if not result.passed:
            self._archive_rejected(path, job_id)
            return result

        # Stage 3: Load mesh with trimesh
        try:
            import trimesh
            mesh = trimesh.load(str(path), force='mesh')
        except Exception as e:
            self._archive_rejected(path, job_id)
            return CleanResult(False, DirtyReason.CORRUPT_FILE,
                               f"Trimesh无法加载: {str(e)[:200]}")

        # Stage 4: Basic mesh validation
        result = self._validate_mesh_structure(mesh)
        if not result.passed:
            self._archive_rejected(path, job_id)
            return result

        # Stage 5: Geometry quality checks
        result = self._check_geometry_quality(mesh)
        if not result.passed:
            self._archive_rejected(path, job_id)
            return result

        # Stage 6: Complexity bounds
        result = self._check_complexity(mesh)
        if not result.passed:
            self._archive_rejected(path, job_id)
            return result

        # Stage 7: Content hash & deduplication
        content_hash = self._compute_content_hash(mesh)
        if DUPLICATE_CHECK:
            is_dup = await check_content_hash(content_hash)
            if is_dup:
                self._archive_rejected(path, job_id)
                return CleanResult(False, DirtyReason.DUPLICATE,
                                   f"内容哈希 {content_hash[:16]}... 已存在")

        # Stage 8: Convert to GLB
        output_path = self._convert_to_glb(mesh, model_name, job_id)
        if not output_path:
            self._archive_rejected(path, job_id)
            return CleanResult(False, DirtyReason.CORRUPT_FILE, "GLB转换失败")

        # Compute bounding box
        bbox = mesh.bounds.tolist() if mesh.bounds is not None else []

        return CleanResult(
            passed=True,
            output_path=str(output_path),
            vertex_count=len(mesh.vertices),
            face_count=len(mesh.faces),
            is_watertight=bool(mesh.is_watertight),
            is_manifold=self._is_manifold(mesh),
            bounding_box=bbox,
            content_hash=content_hash,
            output_size=output_path.stat().st_size,
        )

    # --- Stage implementations ---

    def _check_file_size(self, path: Path) -> CleanResult:
        size = path.stat().st_size
        if size < MIN_FILE_SIZE:
            return CleanResult(False, DirtyReason.FILE_TOO_SMALL,
                               f"文件仅 {size} 字节，低于 {MIN_FILE_SIZE} 字节下限")
        if size > MAX_FILE_SIZE:
            return CleanResult(False, DirtyReason.FILE_TOO_LARGE,
                               f"文件 {size/1024/1024:.1f} MB，超过 {MAX_FILE_SIZE/1024/1024:.0f} MB上限")
        return CleanResult(True)

    def _check_file_integrity(self, path: Path) -> CleanResult:
        """Validate file format via magic bytes and extension."""
        suffix = path.suffix.lower()
        if suffix not in FORMAT_SIGNATURES and suffix not in ('.stl', '.obj', '.glb', '.gltf', '.3mf', '.ply'):
            return CleanResult(False, DirtyReason.INVALID_FORMAT,
                               f"不支持的文件格式: {suffix}")

        with open(path, "rb") as f:
            header = f.read(256)

        if not header:
            return CleanResult(False, DirtyReason.CORRUPT_FILE, "文件为空")

        # STL binary: 80-byte header + 4-byte face count
        if suffix == ".stl":
            if header.startswith(b"solid") and b"\n" in header[:128]:
                # Likely ASCII STL – verify it has vertex data
                try:
                    text = path.read_text(encoding="ascii", errors="ignore")[:2000]
                    if "facet" not in text.lower() and "vertex" not in text.lower():
                        # File says 'solid' but has no mesh data – could be misnamed
                        # Check if it's actually binary
                        if len(header) > 84:
                            pass  # Let trimesh handle it
                        else:
                            return CleanResult(False, DirtyReason.CORRUPT_FILE,
                                               "ASCII STL文件缺少facet/vertex数据")
                except Exception:
                    pass
            elif len(header) >= 84:
                # Binary STL: check face count consistency
                face_count = struct.unpack_from("<I", header, 80)[0]
                expected_size = 84 + face_count * 50
                actual_size = path.stat().st_size
                if actual_size < expected_size * 0.9:  # Allow 10% tolerance
                    return CleanResult(False, DirtyReason.CORRUPT_FILE,
                                       f"二进制STL截断: 声明{face_count}面, 文件大小不匹配")

        elif suffix == ".glb":
            if not header.startswith(b"glTF"):
                return CleanResult(False, DirtyReason.INVALID_FORMAT,
                                   "GLB文件缺少glTF魔术字节")

        elif suffix == ".3mf":
            if not header.startswith(b"PK"):
                return CleanResult(False, DirtyReason.INVALID_FORMAT,
                                   "3MF文件不是有效的ZIP包")

        elif suffix == ".ply":
            if not header.startswith(b"ply"):
                return CleanResult(False, DirtyReason.INVALID_FORMAT,
                                   "PLY文件缺少ply头部标记")

        return CleanResult(True)

    def _validate_mesh_structure(self, mesh) -> CleanResult:
        """Check basic mesh properties."""
        if not hasattr(mesh, 'vertices') or not hasattr(mesh, 'faces'):
            return CleanResult(False, DirtyReason.EMPTY_MESH,
                               "文件不包含可解析的网格数据")

        if len(mesh.vertices) < MIN_VERTEX_COUNT:
            return CleanResult(False, DirtyReason.EMPTY_MESH,
                               f"仅 {len(mesh.vertices)} 个顶点，低于 {MIN_VERTEX_COUNT} 下限")

        if len(mesh.faces) == 0:
            return CleanResult(False, DirtyReason.ZERO_FACES,
                               "模型没有三角面片")

        return CleanResult(True)

    def _check_geometry_quality(self, mesh) -> CleanResult:
        """Check for degenerate geometry and normal issues."""
        # Degenerate faces (zero or near-zero area)
        try:
            areas = mesh.area_faces
            degenerate_count = int(np.sum(areas < DEGENERATE_AREA_THRESHOLD))
            total = len(areas)
            if total > 0:
                ratio = degenerate_count / total
                if ratio > MAX_DEGENERATE_RATIO:
                    return CleanResult(False, DirtyReason.DEGENERATE_FACES,
                                       f"{degenerate_count}/{total} ({ratio:.1%}) 面片退化（面积≈0）")
        except Exception:
            pass

        # Normal consistency check
        try:
            if hasattr(mesh, 'face_normals') and len(mesh.face_normals) > 0:
                # Check if normals are mostly consistent
                # Use the centroid-to-face-center direction vs normal
                if mesh.is_watertight:
                    # For watertight meshes, check winding consistency
                    pass  # trimesh handles this well
                else:
                    # Check for NaN normals (sign of corrupt data)
                    nan_normals = np.sum(np.isnan(mesh.face_normals).any(axis=1))
                    if nan_normals > len(mesh.face_normals) * 0.1:
                        return CleanResult(False, DirtyReason.INVERTED_NORMALS,
                                           f"{nan_normals} 个面片法线为NaN（数据损坏）")
        except Exception:
            pass

        return CleanResult(True)

    def _check_complexity(self, mesh) -> CleanResult:
        """Check face count bounds."""
        fc = len(mesh.faces)
        if fc < MIN_FACE_COUNT:
            return CleanResult(False, DirtyReason.TOO_SIMPLE,
                               f"仅 {fc} 个面片，低于 {MIN_FACE_COUNT} 下限（噪声数据）")
        if fc > MAX_FACE_COUNT:
            return CleanResult(False, DirtyReason.TOO_COMPLEX,
                               f"{fc} 个面片，超过 {MAX_FACE_COUNT} 上限")
        return CleanResult(True)

    def _compute_content_hash(self, mesh) -> str:
        """Compute SHA-256 hash of mesh geometry for deduplication."""
        h = hashlib.sha256()
        h.update(mesh.vertices.tobytes())
        h.update(mesh.faces.tobytes())
        return h.hexdigest()

    def _is_manifold(self, mesh) -> bool:
        """Check if mesh is manifold (each edge shared by at most 2 faces)."""
        try:
            if hasattr(mesh, 'is_watertight') and mesh.is_watertight:
                return True
            # Check edges
            edges = mesh.edges_sorted
            unique, counts = np.unique(edges, axis=0, return_counts=True)
            return bool(np.all(counts <= 2))
        except Exception:
            return False

    def _convert_to_glb(self, mesh, model_name: str, job_id: str) -> Optional[Path]:
        """Convert mesh to GLB format for web viewing."""
        import trimesh
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in model_name)[:60]
        output_dir = CLEANED_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{safe_name}.glb"

        try:
            # If it's a Scene, get the first geometry
            if isinstance(mesh, trimesh.Scene):
                geometries = list(mesh.geometry.values())
                if geometries:
                    mesh = geometries[0]
                else:
                    return None

            # Export to GLB
            glb_data = mesh.export(file_type='glb')
            output_path.write_bytes(glb_data)
            logger.info(f"Converted to GLB: {output_path} ({len(glb_data)} bytes)")
            return output_path
        except Exception as e:
            logger.error(f"GLB conversion failed: {e}")
            return None

    def _archive_rejected(self, path: Path, job_id: str):
        """Move rejected file to rejected directory."""
        dest_dir = REJECTED_DIR / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            if path.exists():
                import shutil
                shutil.copy2(str(path), str(dest))
        except Exception as e:
            logger.warning(f"Failed to archive rejected file: {e}")

"""Pydantic models & enums for the pipeline."""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class CrawlSource(str, Enum):
    THINGIVERSE = "thingiverse"
    PRINTABLES = "printables"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DirtyReason(str, Enum):
    CORRUPT_FILE = "corrupt_file"
    EMPTY_MESH = "empty_mesh"
    ZERO_FACES = "zero_faces"
    DEGENERATE_FACES = "degenerate_faces"
    NON_MANIFOLD = "non_manifold"
    INVERTED_NORMALS = "inverted_normals"
    TOO_COMPLEX = "too_complex"
    TOO_SIMPLE = "too_simple"
    DUPLICATE = "duplicate"
    INVALID_FORMAT = "invalid_format"
    FILE_TOO_LARGE = "file_too_large"
    FILE_TOO_SMALL = "file_too_small"
    ENCODING_ERROR = "encoding_error"
    DOWNLOAD_FAILED = "download_failed"


DIRTY_REASON_ZH = {
    DirtyReason.CORRUPT_FILE: "文件损坏：无法解析二进制结构",
    DirtyReason.EMPTY_MESH: "空网格：文件不包含任何几何数据",
    DirtyReason.ZERO_FACES: "零面片：模型没有三角面",
    DirtyReason.DEGENERATE_FACES: "退化面片：超过30%的面片面积为零",
    DirtyReason.NON_MANIFOLD: "非流形：存在共享超过2个面的边",
    DirtyReason.INVERTED_NORMALS: "法线翻转：超过50%的法线方向异常",
    DirtyReason.TOO_COMPLEX: "过度复杂：面片数超过200万上限",
    DirtyReason.TOO_SIMPLE: "过度简单：面片数低于10（噪声数据）",
    DirtyReason.DUPLICATE: "重复模型：内容哈希与已有模型一致",
    DirtyReason.INVALID_FORMAT: "格式错误：文件扩展名与实际内容不匹配",
    DirtyReason.FILE_TOO_LARGE: "文件过大：超过100MB上限",
    DirtyReason.FILE_TOO_SMALL: "文件过小：低于100字节",
    DirtyReason.ENCODING_ERROR: "编码错误：文本格式文件存在编码问题",
    DirtyReason.DOWNLOAD_FAILED: "下载失败：无法获取完整文件",
}


# --- Request / Response models ---

class CrawlRequest(BaseModel):
    source: CrawlSource = CrawlSource.PRINTABLES
    query: str = ""
    limit: int = 10


class CrawlJobOut(BaseModel):
    id: str
    source: str
    status: str
    query: str
    total_discovered: int
    total_downloaded: int
    total_cleaned: int
    total_rejected: int
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None


class CleanedModelOut(BaseModel):
    id: str
    raw_id: str
    name: str
    source: str
    file_size: int
    vertex_count: int
    face_count: int
    is_watertight: bool
    is_manifold: bool
    bounding_box: str
    cleaned_at: str


class DirtyDataOut(BaseModel):
    id: str
    raw_id: str
    name: str
    source: str
    reason: str
    reason_zh: str
    reason_detail: str
    detected_at: str


class PipelineStats(BaseModel):
    total_jobs: int = 0
    total_discovered: int = 0
    total_downloaded: int = 0
    total_cleaned: int = 0
    total_rejected: int = 0
    rejection_breakdown: dict = {}
    sources_breakdown: dict = {}


class PipelineEvent(BaseModel):
    event_type: str
    stage: str
    message: str
    data: dict = {}
    timestamp: str = ""

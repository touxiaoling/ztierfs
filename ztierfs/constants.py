"""ztierfs 全库共享的数值默认常量。

集中存放块布局、FUSE I/O、读缓存与预读、内联与压缩策略、热层容量与
水位、降级/清理相关时间阈值，以及「已知已压缩」文件后缀集合，便于
单处调整并保持各子模块行为一致。具体含义以使用处文档为准。
"""

# --- 块与 FUSE I/O ---
CHUNK_SIZE = 4 * 1024 * 1024
"""单块逻辑大小（字节）；文件分块与多数 I/O 对齐基准。"""

DEFAULT_FUSE_IOSIZE = CHUNK_SIZE
"""FUSE 建议的最大单次读写粒度，默认与块大小一致。"""

DEFAULT_FUSE_METADATA_CACHE_SECONDS = 5.0
"""FUSE 元数据缓存时长（秒）的默认值。"""

# --- 读路径：块读缓存与 FUSE 预读 ---
DEFAULT_READ_CACHE_BYTES = 128 * 1024 * 1024
"""BlockStore 进程内 LRU 解码块缓存总字节上限。"""

DEFAULT_READAHEAD_BLOCKS = 1
"""预读块数默认上限（以逻辑块为单位）。"""

DEFAULT_READAHEAD_WORKERS = 1
"""并行预读工作线程数默认值。"""

# --- 内联与压缩 ---
DEFAULT_INLINE_MAX_BYTES = 32 * 1024
"""小块可内联进元数据库存储的最大原始字节数。"""

DEFAULT_COMPRESSION_MIN_BYTES = 4 * 1024
"""低于此大小的数据默认不尝试 zstd 压缩（节省 CPU）。"""

# --- 热层容量与文件头保护 ---
DEFAULT_HOT_CACHE_MAX_BYTES = 10 * 1024 * 1024 * 1024
"""热层目标容量上限（高水位），超过后触发向冷层降级。"""

DEFAULT_HOT_CACHE_MIN_BYTES = 8 * 1024 * 1024 * 1024
"""热层目标容量下限（低水位），降级持续到落至此线附近或无可选块。"""

DEFAULT_PROTECTED_PREFIX_CHUNKS = 2
"""每个文件开头若干逻辑块默认保留在热层，减轻顺序读头延迟。"""

# --- 冷热与清理策略时间 ---
DEFAULT_MIN_HOT_AGE_SECONDS = 24 * 60 * 60
"""块在热层至少停留时长（秒），用于降级候选筛选，避免抖动。"""

DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS = 0
"""copy-up 后冗余冷层副本按龄清理的窗口（秒）；≤0 时关闭该清理路径。"""

DEFAULT_COLD_GC_AGE_SECONDS = 30 * 24 * 60 * 60
"""无引用冷层块进入 cold garbage 后的默认保留窗口（秒），默认 30 天。"""

# --- 已知已压缩/媒体类后缀：跳过 zstd 尝试 ---
COMPRESSED_SUFFIXES = frozenset(
    {
        ".7z",
        ".aac",
        ".avif",
        ".br",
        ".bz2",
        ".flac",
        ".gif",
        ".gz",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".pdf",
        ".png",
        ".rar",
        ".webm",
        ".webp",
        ".xz",
        ".zip",
        ".zst",
        ".zstd",
    }
)
"""小写扩展名集合；匹配时跳过压缩以省 CPU 且避免膨胀。"""

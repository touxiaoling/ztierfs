CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_FUSE_IOSIZE = CHUNK_SIZE
DEFAULT_FUSE_METADATA_CACHE_SECONDS = 5.0
DEFAULT_INLINE_MAX_BYTES = 32 * 1024
DEFAULT_READ_CACHE_BYTES = 128 * 1024 * 1024
DEFAULT_READAHEAD_BLOCKS = 1
DEFAULT_READAHEAD_WORKERS = 1
DEFAULT_COMPRESSION_MIN_BYTES = 4 * 1024
DEFAULT_HOT_CACHE_MAX_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_HOT_CACHE_MIN_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_PROTECTED_PREFIX_CHUNKS = 2
DEFAULT_MIN_HOT_AGE_SECONDS = 24 * 60 * 60
DEFAULT_COLD_COPY_CLEANUP_AGE_SECONDS = 0
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

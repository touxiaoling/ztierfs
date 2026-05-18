"""Shared physical unlink helper for committed pending block-file deletions."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from .tier_access import PathUnavailable, unlink_path


@dataclass(frozen=True)
class PendingDeletionOutcome:
    """Physical drain result; callers decide how to update SQLite."""

    removed_ids: list[int]
    deferred_ids: list[int]
    unavailable_ids: list[int]


class PendingDeletionRow(Protocol):
    """Row shape needed by the physical pending deletion helper."""

    def __getitem__(self, key: str, /) -> Any: ...


def drain_pending_block_files(
    rows: Iterable[PendingDeletionRow],
    block_path_for: Callable[[str, int], Path],
) -> PendingDeletionOutcome:
    """Unlink pending block files, treating already-missing files as complete."""
    removed_ids: list[int] = []
    deferred_ids: list[int] = []
    unavailable_ids: list[int] = []
    for row in rows:
        deletion_id = int(row["id"])
        digest = str(row["digest"])
        tier = int(row["tier"])
        try:
            unlink_path(block_path_for(digest, tier))
        except PathUnavailable:
            deferred_ids.append(deletion_id)
            unavailable_ids.append(deletion_id)
            continue
        except OSError:
            logger.exception("待 GC payload 删除失败：id={}", deletion_id)
            deferred_ids.append(deletion_id)
            continue
        removed_ids.append(deletion_id)
    return PendingDeletionOutcome(
        removed_ids=removed_ids,
        deferred_ids=deferred_ids,
        unavailable_ids=unavailable_ids,
    )

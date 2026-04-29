"""从库内 config 行解析 tier1/tier2 与可选载荷外置路径（CLI 与 checker 共用）。"""

import sqlite3

from dataclasses import dataclass
from pathlib import Path

from ztierfs.metadata import open_database

from .paths import default_database


@dataclass(frozen=True)
class MaintenancePaths:
    database: Path
    tier1: Path
    tier2: Path
    payload_store_path: Path | None = None


def resolve_maintenance_paths(
    path: str | Path,
    tier2: str | Path | None = None,
    database: str | Path | None = None,
    *,
    allow_config_mismatch: bool = False,
    update_config: bool = False,
) -> MaintenancePaths:
    if tier2 is None:
        if database is not None:
            raise ValueError(
                "--database is redundant when the positional path is already the database"
            )
        if allow_config_mismatch or update_config:
            raise ValueError(
                "--allow-config-mismatch and --update-config require explicit hot/cold tiers"
            )
        db_path = Path(path).resolve()
        config = _read_required_config(db_path)
        return MaintenancePaths(
            database=db_path,
            tier1=Path(config["hot_tier_path"]).resolve(),
            tier2=Path(config["cold_tier_path"]).resolve(),
            payload_store_path=_payload_store_path(config, None),
        )

    tier1_path = Path(path).resolve()
    tier2_path = Path(tier2).resolve()
    db_path = default_database(tier1_path, database)
    config = _read_optional_config(db_path)
    if config is None:
        if update_config:
            _write_config(db_path, tier1_path, tier2_path)
        return MaintenancePaths(
            database=db_path,
            tier1=tier1_path,
            tier2=tier2_path,
            payload_store_path=None,
        )

    if update_config:
        _write_config(
            db_path,
            tier1_path,
            tier2_path,
            payload_store=config["payload_store"],
            payload_store_path=_payload_store_path(config, tier1_path),
        )
        return MaintenancePaths(
            database=db_path,
            tier1=tier1_path,
            tier2=tier2_path,
            payload_store_path=_payload_store_path(config, tier1_path),
        )

    mismatches = _path_mismatches(config, tier1_path, tier2_path)
    if mismatches and not allow_config_mismatch:
        details = ", ".join(
            f"{key}: stored={stored!r}, requested={requested!r}"
            for key, stored, requested in mismatches
        )
        raise ValueError(
            "explicit storage paths do not match database config; "
            f"use --allow-config-mismatch or --update-config ({details})"
        )

    return MaintenancePaths(
        database=db_path,
        tier1=tier1_path,
        tier2=tier2_path,
        payload_store_path=_payload_store_path(config, tier1_path),
    )


def _read_required_config(db_path: Path) -> sqlite3.Row:
    config = _read_optional_config(db_path)
    if config is None:
        raise ValueError(
            "metadata database does not contain storage path config; "
            "rerun with explicit <hot-tier> <cold-tier> --database, optionally --update-config"
        )
    return config


def _read_optional_config(db_path: Path) -> sqlite3.Row | None:
    with open_database(db_path) as db:
        try:
            return db.execute(
                """
                SELECT hot_tier_path, cold_tier_path, payload_store, payload_store_path
                FROM filesystem_config
                WHERE id = 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: filesystem_config" in str(exc):
                return None
            raise


def _write_config(
    db_path: Path,
    tier1: Path,
    tier2: Path,
    *,
    payload_store: str | None = None,
    payload_store_path: Path | None = None,
) -> None:
    payload_store = payload_store or _detect_payload_store(db_path)
    if payload_store == "filekv" and payload_store_path is None:
        payload_store_path = tier1 / "payload-kv"
    with open_database(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS filesystem_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    config_version INTEGER NOT NULL,
                    hot_tier_path TEXT NOT NULL,
                    cold_tier_path TEXT NOT NULL,
                    payload_store TEXT NOT NULL CHECK (payload_store IN ('sqlite', 'filekv')),
                    payload_store_path TEXT,
                    CHECK (
                        (payload_store = 'sqlite' AND payload_store_path IS NULL)
                        OR (payload_store = 'filekv' AND payload_store_path IS NOT NULL)
                    )
                )
                """
            )
            db.execute(
                """
                INSERT INTO filesystem_config (
                    id, config_version, hot_tier_path, cold_tier_path, payload_store, payload_store_path
                )
                VALUES (1, 1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_version = excluded.config_version,
                    hot_tier_path = excluded.hot_tier_path,
                    cold_tier_path = excluded.cold_tier_path,
                    payload_store = excluded.payload_store,
                    payload_store_path = excluded.payload_store_path
                """,
                (
                    str(tier1),
                    str(tier2),
                    payload_store,
                    str(payload_store_path) if payload_store_path is not None else None,
                ),
            )
        except Exception:
            db.execute("ROLLBACK")
            raise
        else:
            db.execute("COMMIT")


def _detect_payload_store(db_path: Path) -> str:
    with open_database(db_path) as db:
        for table in ("inode_payloads", "block_payloads"):
            try:
                count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE payload_store != 'sqlite'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if count:
                return "filekv"
    return "sqlite"


def _payload_store_path(config: sqlite3.Row, tier1: Path | None) -> Path | None:
    if config["payload_store"] != "filekv":
        return None
    configured = config["payload_store_path"]
    if configured is not None:
        return Path(configured).resolve()
    if tier1 is not None:
        return tier1 / "payload-kv"
    return None


def _path_mismatches(
    config: sqlite3.Row, tier1: Path, tier2: Path
) -> list[tuple[str, str, str]]:
    expected = {
        "hot_tier_path": str(tier1),
        "cold_tier_path": str(tier2),
    }
    return [
        (key, config[key], value)
        for key, value in expected.items()
        if Path(config[key]).resolve() != Path(value)
    ]

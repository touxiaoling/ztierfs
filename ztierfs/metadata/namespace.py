import errno
import sqlite3

from typing import Any

from stat import S_IFDIR

from macfusepy import FuseOSError

from .base import MetadataMixinBase
from ztierfs.pathing import split_path

TRASH_ROOT_NAME = ".Trashes"
TRASH_ROOT_MODE = S_IFDIR | 0o1777
TRASH_USER_MODE = S_IFDIR | 0o700


class NamespaceMixin(MetadataMixinBase):
    NODE_SELECT = """
        SELECT
            inodes.*,
            COALESCE(inode_payloads.compressed, 0) AS inline_compressed,
            COALESCE(inode_payloads.stored_size, 0) AS inline_stored_size
        FROM inodes
        LEFT JOIN inode_payloads ON inode_payloads.inode_id = inodes.id
    """

    def get_node(self, path: str) -> sqlite3.Row:
        node = self.lookup_node(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        return node

    def lookup_node(self, path: str) -> sqlite3.Row | None:
        row = self.node_by_id(1)
        for part in split_path(path):
            if row is None or row["kind"] != "dir":
                return None
            row = self.child(row["id"], part)
        return row

    def parent_and_name(self, path: str) -> tuple[sqlite3.Row, str]:
        parts = split_path(path)
        if not parts:
            raise FuseOSError(errno.EINVAL)
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        parent = self.lookup_node(parent_path)
        if parent is None:
            raise FuseOSError(errno.ENOENT)
        if parent["kind"] != "dir":
            raise FuseOSError(errno.ENOTDIR)
        return parent, parts[-1]

    def child(self, parent_id: int, name: str) -> sqlite3.Row | None:
        return self._db.execute(
            f"""
            SELECT node.*, dir_entries.parent_id, dir_entries.name
            FROM dir_entries
            JOIN ({self.NODE_SELECT}) AS node ON node.id = dir_entries.inode_id
            WHERE dir_entries.parent_id = ? AND dir_entries.name = ?
            """,
            (parent_id, name),
        ).fetchone()

    def node_by_id(self, node_id: int) -> sqlite3.Row | None:
        return self._db.execute(
            f"""
            SELECT node.*, parent_entry.parent_id, parent_entry.name
            FROM ({self.NODE_SELECT}) AS node
            LEFT JOIN (
                SELECT parent_id, name, inode_id
                FROM dir_entries
                WHERE inode_id = ?
                ORDER BY parent_id, name
                LIMIT 1
            ) AS parent_entry ON parent_entry.inode_id = node.id
            WHERE id = ?
            """,
            (node_id, node_id),
        ).fetchone()

    def inline_payload(self, node_id: int) -> sqlite3.Row | dict[str, Any] | None:
        row = self._db.execute(
            """
            SELECT payload, payload_store, payload_key, compressed, raw_size, stored_size
            FROM inode_payloads
            WHERE inode_id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None or row["payload_store"] == "sqlite":
            return row
        payload = self.payload_store.get(row["payload_key"])
        return {
            "payload": payload,
            "payload_store": row["payload_store"],
            "payload_key": row["payload_key"],
            "compressed": row["compressed"],
            "raw_size": row["raw_size"],
            "stored_size": row["stored_size"],
        }

    def child_dir_count(self, parent_id: int) -> int:
        return self._db.execute(
            """
            SELECT COUNT(*)
            FROM dir_entries
            JOIN inodes ON inodes.id = dir_entries.inode_id
            WHERE dir_entries.parent_id = ? AND inodes.kind = 'dir'
            """,
            (parent_id,),
        ).fetchone()[0]

    def child_names(self, parent_id: int) -> list[str]:
        rows = self._db.execute(
            "SELECT name FROM dir_entries WHERE parent_id = ? ORDER BY name",
            (parent_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def children(self, parent_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            f"""
            SELECT node.*, dir_entries.parent_id, dir_entries.name
            FROM dir_entries
            JOIN ({self.NODE_SELECT}) AS node ON node.id = dir_entries.inode_id
            WHERE dir_entries.parent_id = ?
            ORDER BY dir_entries.name
            """,
            (parent_id,),
        ).fetchall()

    def children_page(
        self, parent_id: int, offset: int, limit: int
    ) -> list[sqlite3.Row]:
        return self._db.execute(
            f"""
            SELECT node.*, dir_entries.parent_id, dir_entries.name
            FROM dir_entries
            JOIN ({self.NODE_SELECT}) AS node ON node.id = dir_entries.inode_id
            WHERE dir_entries.parent_id = ?
            ORDER BY dir_entries.name
            LIMIT ? OFFSET ?
            """,
            (parent_id, limit, offset),
        ).fetchall()

    def children_after(
        self, parent_id: int, after_name: str | None, limit: int
    ) -> list[sqlite3.Row]:
        if after_name is None:
            return self.children_page(parent_id, 0, limit)
        return self._db.execute(
            f"""
            SELECT node.*, dir_entries.parent_id, dir_entries.name
            FROM dir_entries
            JOIN ({self.NODE_SELECT}) AS node ON node.id = dir_entries.inode_id
            WHERE dir_entries.parent_id = ? AND dir_entries.name > ?
            ORDER BY dir_entries.name
            LIMIT ?
            """,
            (parent_id, after_name, limit),
        ).fetchall()

    def has_children(self, parent_id: int) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM dir_entries WHERE parent_id = ? LIMIT 1", (parent_id,)
            ).fetchone()
            is not None
        )

    def ensure_trash_directories(self, uid: int, gid: int, now: int) -> None:
        trash_root = self.child(1, TRASH_ROOT_NAME)
        if trash_root is None:
            trash_root_id = self.insert_node(
                1,
                TRASH_ROOT_NAME,
                "dir",
                TRASH_ROOT_MODE,
                0,
                0,
                now,
            )
        else:
            if trash_root["kind"] != "dir":
                raise FuseOSError(errno.ENOTDIR)
            trash_root_id = trash_root["id"]
            if (
                trash_root["mode"] != TRASH_ROOT_MODE
                or trash_root["uid"] != 0
                or trash_root["gid"] != 0
            ):
                self.set_node_mode(trash_root_id, TRASH_ROOT_MODE, now)
                self.set_node_owner(trash_root_id, 0, 0, now)

        user_trash_name = str(uid)
        user_trash = self.child(trash_root_id, user_trash_name)
        if user_trash is None:
            self.insert_node(
                trash_root_id,
                user_trash_name,
                "dir",
                TRASH_USER_MODE,
                uid,
                gid,
                now,
            )
            return
        if user_trash["kind"] != "dir":
            raise FuseOSError(errno.ENOTDIR)
        if (
            user_trash["mode"] != TRASH_USER_MODE
            or user_trash["uid"] != uid
            or user_trash["gid"] != gid
        ):
            self.set_node_mode(user_trash["id"], TRASH_USER_MODE, now)
            self.set_node_owner(user_trash["id"], uid, gid, now)

    def insert_node(
        self,
        parent_id: int,
        name: str,
        kind: str,
        mode: int,
        uid: int,
        gid: int,
        now: int,
        *,
        symlink_target: str | None = None,
    ) -> int:
        size = len(symlink_target.encode()) if symlink_target is not None else 0
        cursor = self._db.execute(
            """
            INSERT INTO inodes
                (kind, mode, uid, gid, size, symlink_target, nlink, atime_ns, mtime_ns, ctime_ns)
            VALUES
                (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (kind, mode, uid, gid, size, symlink_target, now, now, now),
        )
        inode_id = cursor.lastrowid
        assert inode_id is not None
        self._db.execute(
            "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (?, ?, ?)",
            (parent_id, name, inode_id),
        )
        return inode_id

    def clone_file_node(
        self,
        source_id: int,
        parent_id: int,
        name: str,
        *,
        mode: int,
        uid: int,
        gid: int,
        size: int,
        now: int,
    ) -> int:
        cursor = self._db.execute(
            """
            INSERT INTO inodes
                (kind, mode, uid, gid, size, symlink_target, nlink, atime_ns, mtime_ns, ctime_ns)
            SELECT
                'file', ?, ?, ?, ?, NULL, 1, ?, ?, ?
            FROM inodes
            WHERE id = ?
            """,
            (mode, uid, gid, size, now, now, now, source_id),
        )
        inode_id = cursor.lastrowid
        assert inode_id is not None
        self._db.execute(
            "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (?, ?, ?)",
            (parent_id, name, inode_id),
        )
        self._db.execute(
            """
            INSERT INTO inode_payloads (
                inode_id, payload, payload_store, payload_key, compressed, raw_size, stored_size
            )
            SELECT ?, payload, payload_store, payload_key, compressed, raw_size, stored_size
            FROM inode_payloads
            WHERE inode_id = ?
            """,
            (inode_id, source_id),
        )
        payload_row = self._db.execute(
            """
            SELECT payload_store, payload_key
            FROM inode_payloads
            WHERE inode_id = ?
            """,
            (inode_id,),
        ).fetchone()
        if payload_row is not None and payload_row["payload_store"] != "sqlite":
            new_key = f"inode/{inode_id}"
            self.payload_store.put(new_key, self.payload_store.get(payload_row["payload_key"]))
            self._db.execute(
                """
                UPDATE inode_payloads
                SET payload_key = ?
                WHERE inode_id = ?
                """,
                (new_key, inode_id),
            )
        self._db.execute(
            """
            INSERT INTO file_chunks (file_id, chunk_index, hash, size)
            SELECT ?, chunk_index, hash, size
            FROM file_chunks
            WHERE file_id = ?
            """,
            (inode_id, source_id),
        )
        self._db.execute(
            """
            UPDATE blocks
            SET refcount = refcount + (
                SELECT COUNT(*)
                FROM file_chunks
                WHERE file_id = ? AND file_chunks.hash = blocks.hash
            )
            WHERE hash IN (
                SELECT hash
                FROM file_chunks
                WHERE file_id = ?
            )
            """,
            (source_id, source_id),
        )
        self._db.execute(
            """
            INSERT INTO inode_xattrs (inode_id, name, value)
            SELECT ?, name, value
            FROM inode_xattrs
            WHERE inode_id = ?
            """,
            (inode_id, source_id),
        )
        return inode_id

    def link_node(self, parent_id: int, name: str, inode_id: int, now: int) -> None:
        self._db.execute(
            "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (?, ?, ?)",
            (parent_id, name, inode_id),
        )
        self._db.execute(
            "UPDATE inodes SET nlink = nlink + 1, ctime_ns = ? WHERE id = ?",
            (now, inode_id),
        )

    def reset_file_node(self, node_id: int, mode: int, now: int) -> None:
        self._db.execute(
            """
            UPDATE inodes
            SET mode = ?, size = 0, mtime_ns = ?, ctime_ns = ?
            WHERE id = ?
            """,
            (mode, now, now, node_id),
        )
        self.clear_inline_file(node_id)

    def touch_node_atime(self, node_id: int, now: int) -> None:
        self._db.execute("UPDATE inodes SET atime_ns = ? WHERE id = ?", (now, node_id))


    def set_node_size(self, node_id: int, size: int, now: int) -> None:
        self._db.execute(
            """
            UPDATE inodes
            SET size = ?, mtime_ns = ?, ctime_ns = ?
            WHERE id = ?
            """,
            (size, now, now, node_id),
        )
        self.clear_inline_file(node_id)

    def set_inline_file(
        self,
        node_id: int,
        data: bytes,
        *,
        compressed: bool,
        raw_size: int,
        now: int,
    ) -> None:
        payload_store = self.payload_store.name
        payload_key = None
        inline_payload: bytes | None = data
        if payload_store != "sqlite":
            payload_key = f"inode/{node_id}"
            self.payload_store.put(payload_key, data)
            inline_payload = None
        self._db.execute(
            """
            UPDATE inodes
            SET size = ?, mtime_ns = ?, ctime_ns = ?
            WHERE id = ?
            """,
            (raw_size, now, now, node_id),
        )
        self._db.execute(
            """
            INSERT INTO inode_payloads (
                inode_id, payload, payload_store, payload_key, compressed, raw_size, stored_size
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(inode_id) DO UPDATE SET
                payload = excluded.payload,
                payload_store = excluded.payload_store,
                payload_key = excluded.payload_key,
                compressed = excluded.compressed,
                raw_size = excluded.raw_size,
                stored_size = excluded.stored_size
            """,
            (
                node_id,
                inline_payload,
                payload_store,
                payload_key,
                int(compressed),
                raw_size,
                len(data),
            ),
        )

    def clear_inline_file(self, node_id: int) -> None:
        row = self._db.execute(
            """
            SELECT payload_store, payload_key
            FROM inode_payloads
            WHERE inode_id = ?
            """,
            (node_id,),
        ).fetchone()
        self._db.execute(
            "DELETE FROM inode_payloads WHERE inode_id = ?",
            (node_id,),
        )
        if row is not None and row["payload_store"] != "sqlite":
            self.payload_store.delete(row["payload_key"])

    def delete_node(self, node_id: int) -> None:
        self._db.execute("DELETE FROM inodes WHERE id = ?", (node_id,))

    def remove_entry(self, parent_id: int, name: str, inode_id: int, now: int) -> int:
        self._db.execute(
            "DELETE FROM dir_entries WHERE parent_id = ? AND name = ?",
            (parent_id, name),
        )
        self._db.execute(
            "UPDATE inodes SET nlink = nlink - 1, ctime_ns = ? WHERE id = ?",
            (now, inode_id),
        )
        row = self._db.execute(
            "SELECT nlink FROM inodes WHERE id = ?", (inode_id,)
        ).fetchone()
        return int(row["nlink"]) if row else 0

    def move_entry(
        self,
        old_parent_id: int,
        old_name: str,
        new_parent_id: int,
        new_name: str,
        inode_id: int,
        now: int,
    ) -> None:
        self._db.execute(
            """
            UPDATE dir_entries
            SET parent_id = ?, name = ?
            WHERE parent_id = ? AND name = ? AND inode_id = ?
            """,
            (new_parent_id, new_name, old_parent_id, old_name, inode_id),
        )
        self._db.execute("UPDATE inodes SET ctime_ns = ? WHERE id = ?", (now, inode_id))

    def set_node_mode(self, node_id: int, mode: int, now: int) -> None:
        self._db.execute(
            "UPDATE inodes SET mode = ?, ctime_ns = ? WHERE id = ?",
            (mode, now, node_id),
        )

    def set_node_owner(self, node_id: int, uid: int, gid: int, now: int) -> None:
        self._db.execute(
            "UPDATE inodes SET uid = ?, gid = ?, ctime_ns = ? WHERE id = ?",
            (uid, gid, now, node_id),
        )

    def set_node_times(self, node_id: int, atime: int, mtime: int, now: int) -> None:
        self._db.execute(
            "UPDATE inodes SET atime_ns = ?, mtime_ns = ?, ctime_ns = ? WHERE id = ?",
            (atime, mtime, now, node_id),
        )

    def is_descendant(self, parent_id: int, node_id: int) -> bool:
        current = parent_id
        while current:
            if current == node_id:
                return True
            row = self._db.execute(
                """
                SELECT parent_id
                FROM dir_entries
                WHERE inode_id = ?
                LIMIT 1
                """,
                (current,),
            ).fetchone()
            current = row["parent_id"] if row else 0
        return False

    def xattr(self, inode_id: int, name: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT value FROM inode_xattrs WHERE inode_id = ? AND name = ?",
            (inode_id, name),
        ).fetchone()

    def xattr_names(self, inode_id: int) -> list[str]:
        rows = self._db.execute(
            "SELECT name FROM inode_xattrs WHERE inode_id = ? ORDER BY name",
            (inode_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def set_xattr(self, inode_id: int, name: str, value: bytes, now: int) -> None:
        self._db.execute(
            """
            INSERT INTO inode_xattrs (inode_id, name, value)
            VALUES (?, ?, ?)
            ON CONFLICT(inode_id, name) DO UPDATE SET value = excluded.value
            """,
            (inode_id, name, value),
        )
        self._db.execute("UPDATE inodes SET ctime_ns = ? WHERE id = ?", (now, inode_id))

    def remove_xattr(self, inode_id: int, name: str, now: int) -> bool:
        cursor = self._db.execute(
            "DELETE FROM inode_xattrs WHERE inode_id = ? AND name = ?",
            (inode_id, name),
        )
        if cursor.rowcount:
            self._db.execute(
                "UPDATE inodes SET ctime_ns = ? WHERE id = ?", (now, inode_id)
            )
            return True
        return False

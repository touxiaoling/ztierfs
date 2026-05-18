"""命名空间元数据的 SQL 辅助：路径解析、`dir_entries` 与 `NODE_SELECT` 上的 inode 行。

`NODE_SELECT` 统一返回 inode 行，便于上层 child/children 等接口共享列集。macOS 回收站布局：
根下按需创建 `.Trashes`（权限类 1777、属主 root）及
`.Trashes/<uid>/`（属主为当前用户）；详见 `ensure_trash_directories`。

文件内容统一由 `file_chunks -> blocks` 表达；`link_node` 与 `move_entry` 只改变目录项和链接计数。
"""

import errno
import sqlite3


from stat import S_IFDIR

from macfusepy import FuseOSError

from .base import MetadataMixinBase
from ztierfs.pathing import split_path

TRASH_ROOT_NAME = ".Trashes"
TRASH_ROOT_MODE = S_IFDIR | 0o1777
TRASH_USER_MODE = S_IFDIR | 0o700


class NamespaceMixin(MetadataMixinBase):
    """`inodes`、`dir_entries` 的查询与更新，以及 macOS `.Trashes` 目录树。

    查询侧统一套用在 `NODE_SELECT` 子查询上，以便同一 inode 的内联列与 `child`/`children`
    等接口返回结构一致。
    """

    NODE_SELECT = "SELECT inodes.* FROM inodes"

    def get_node(self, path: str) -> sqlite3.Row:
        """按路径解析 inode；不存在则抛出 ENOENT。返回行为 `NODE_SELECT` 结果集结构。"""
        node = self.lookup_node(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
        return node

    def lookup_node(self, path: str) -> sqlite3.Row | None:
        """从根 inode 起逐级 `child` 解析路径；任一段缺失或非目录则返回 None。"""
        row = self.node_by_id(1)
        for part in split_path(path):
            if row is None or row["kind"] != "dir":
                return None
            row = self.child(row["id"], part)
        return row

    def parent_and_name(self, path: str) -> tuple[sqlite3.Row, str]:
        """拆分路径为父目录 inode 与末段名字。空路径 EINVAL；父不存在 ENOENT；父非目录 ENOTDIR。"""
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
        """在指定父目录下按名字查询目录项；inode 侧经 `NODE_SELECT` 展开。"""
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
        """按 id 取 inode（`NODE_SELECT`），并左联一条任意父目录项以带上 parent_id/name（多硬链接 inode 仅展示其中一条链）。"""
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

    def inode_by_id(self, node_id: int) -> sqlite3.Row | None:
        """按 id 仅取 inode 行，不联查目录项。"""
        return self._db.execute(
            f"""
            {self.NODE_SELECT}
            WHERE id = ?
            """,
            (node_id,),
        ).fetchone()

    def child_dir_count(self, parent_id: int) -> int:
        """统计父目录下子项中 kind 为目录的个数。"""
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
        """列出父目录下所有目录项名字，按名字排序。"""
        rows = self._db.execute(
            "SELECT name FROM dir_entries WHERE parent_id = ? ORDER BY name",
            (parent_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def children(self, parent_id: int) -> list[sqlite3.Row]:
        """枚举父目录下全部子项（inode 经 `NODE_SELECT`），按名字排序。"""
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
        """分页枚举子项：`LIMIT`/`OFFSET` 作用于按名字排序后的结果。"""
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
        """键集分页：`after_name` 为 None 时等价于从开头分页；否则仅返回名字字典序大于 `after_name` 的前 `limit` 条。"""
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
        """若父目录下至少有一条目录项则返回 True。"""
        return (
            self._db.execute(
                "SELECT 1 FROM dir_entries WHERE parent_id = ? LIMIT 1", (parent_id,)
            ).fetchone()
            is not None
        )

    def ensure_trash_directories(self, uid: int, gid: int, now: int) -> None:
        """按需创建 macOS 风格回收站：`/.Trashes`（模式类 1777、属主 root）与 `/.Trashes/<uid>/`（属主 uid/gid、模式 700）；已存在但类型或属主/权限不符时修正。"""
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
        """插入新 inode 并在 `parent_id` 下建立目录项；符号链接时 `size` 为目标字符串字节长度。"""
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
        """克隆普通文件 inode：复制 `file_chunks`（并递增块引用计数）和 `inode_xattrs`，并在 `parent_id` 下新建目录项。"""
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
        """为已有 inode 增加一条硬链接目录项，并将该 inode 的 `nlink` 加一。"""
        self._db.execute(
            "INSERT INTO dir_entries (parent_id, name, inode_id) VALUES (?, ?, ?)",
            (parent_id, name, inode_id),
        )
        self._db.execute(
            "UPDATE inodes SET nlink = nlink + 1, ctime_ns = ? WHERE id = ?",
            (now, inode_id),
        )

    def reset_file_node(self, node_id: int, mode: int, now: int) -> None:
        """将文件 inode 截断语义落到元数据：`size` 置 0、更新时间戳。"""
        self._db.execute(
            """
            UPDATE inodes
            SET mode = ?, size = 0, mtime_ns = ?, ctime_ns = ?
            WHERE id = ?
            """,
            (mode, now, now, node_id),
        )

    def touch_node_atime(self, node_id: int, now: int) -> None:
        """仅更新 inode 的访问时间 `atime_ns`。"""
        self._db.execute("UPDATE inodes SET atime_ns = ? WHERE id = ?", (now, node_id))

    def set_node_size(self, node_id: int, size: int, now: int) -> None:
        """设置逻辑文件长度并更新 mtime/ctime。"""
        self._db.execute(
            """
            UPDATE inodes
            SET size = ?, mtime_ns = ?, ctime_ns = ?
            WHERE id = ?
            """,
            (size, now, now, node_id),
        )

    def delete_node(self, node_id: int) -> None:
        """从 `inodes` 表删除一行（调用方需已处理目录项与孤儿约束）。"""
        self._db.execute("DELETE FROM inodes WHERE id = ?", (node_id,))

    def remove_entry(self, parent_id: int, name: str, inode_id: int, now: int) -> int:
        """删除一条目录项并将目标 inode 的 `nlink` 减一；返回更新后的 `nlink`（用于判断是否可删 inode）。"""
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
        """重命名/移动目录项：仅更新 `dir_entries` 的父目录与名字，并刷新 inode `ctime_ns`（不改变 payload 与块绑定）。"""
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
        """更新 inode 权限位并刷新 `ctime_ns`。"""
        self._db.execute(
            "UPDATE inodes SET mode = ?, ctime_ns = ? WHERE id = ?",
            (mode, now, node_id),
        )

    def set_node_owner(self, node_id: int, uid: int, gid: int, now: int) -> None:
        """更新 inode 属主并刷新 `ctime_ns`。"""
        self._db.execute(
            "UPDATE inodes SET uid = ?, gid = ?, ctime_ns = ? WHERE id = ?",
            (uid, gid, now, node_id),
        )

    def set_node_times(self, node_id: int, atime: int, mtime: int, now: int) -> None:
        """设置 `atime_ns`/`mtime_ns`，并以 `now` 更新 `ctime_ns`。"""
        self._db.execute(
            "UPDATE inodes SET atime_ns = ?, mtime_ns = ?, ctime_ns = ? WHERE id = ?",
            (atime, mtime, now, node_id),
        )

    def is_descendant(self, parent_id: int, node_id: int) -> bool:
        """沿目录树向上（每次任取一条指向当前 inode 的父链接）判断 `parent_id` 是否为 `node_id` 的祖先；用于防止将目录移动到其子孙之下。"""
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
        """按键读取 inode 的一条扩展属性值。"""
        return self._db.execute(
            "SELECT value FROM inode_xattrs WHERE inode_id = ? AND name = ?",
            (inode_id, name),
        ).fetchone()

    def xattr_names(self, inode_id: int) -> list[str]:
        """列出 inode 的全部扩展属性名，排序返回。"""
        rows = self._db.execute(
            "SELECT name FROM inode_xattrs WHERE inode_id = ? ORDER BY name",
            (inode_id,),
        ).fetchall()
        return [row["name"] for row in rows]

    def set_xattr(self, inode_id: int, name: str, value: bytes, now: int) -> None:
        """插入或更新扩展属性，并刷新 inode `ctime_ns`。"""
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
        """删除一条扩展属性；若确有删除则更新 `ctime_ns` 并返回 True，否则 False。"""
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

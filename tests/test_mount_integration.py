import os
import sqlite3
import sys

import pytest

from .helpers import mounted_ztierfs


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="ztierfs only supports macOS")
def test_ztierfs_real_mount_round_trips_files_and_directories(tmp_path):
    with mounted_ztierfs(tmp_path) as (mount, tier1, tier2, database):
        docs = mount / "docs"
        docs.mkdir()

        note = docs / "note.txt"
        data = b"a" * 3000
        note.write_bytes(data)
        assert note.read_bytes() == data

        renamed = docs / "renamed.txt"
        note.rename(renamed)
        with renamed.open("r+b") as file:
            file.seek(512)
            file.write(b"middle")
        assert renamed.read_bytes()[512:518] == b"middle"

        os.truncate(renamed, 1024)
        assert renamed.stat().st_size == 1024
        assert renamed.read_bytes()[512:518] == b"middle"

        renamed.unlink()
        docs.rmdir()
        assert not docs.exists()

        with sqlite3.connect(database) as db:
            assert (
                db.execute(
                    """
                    SELECT COUNT(*)
                    FROM dir_entries
                    WHERE name IN ('docs', 'note.txt', 'renamed.txt')
                    """
                ).fetchone()[0]
                == 0
            )
        assert (tier1 / "blocks").exists()
        assert (tier2 / "blocks").exists()

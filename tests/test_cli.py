import json

import pytest
import macfusepy

from ztierfs.cli import main

from .helpers import adapted, make_fs


def test_cli_requires_explicit_subcommand(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "hot"), str(tmp_path / "cold"), str(tmp_path / "mount")])
    assert excinfo.value.code == 2


def test_cli_stats_outputs_json(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    main(["stats", str(fs_impl.database), "--json"])
    output = json.loads(capsys.readouterr().out)
    assert output["inodes"]["files"] == 1
    assert output["blocks"]["total"] == 0
    assert output["blocks"]["inode_inline"] == 1
    assert output["blocks"]["hot"] == 0


def test_cli_stats_can_write_log_file_without_polluting_json(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)
    log_file = tmp_path / "ztierfs.log"
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    main(
        [
            "stats",
            str(fs_impl.tier1),
            str(fs_impl.tier2),
            "--database",
            str(fs_impl.database),
            "--json",
            "--log-level",
            "DEBUG",
            "--log-file",
            str(log_file),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["blocks"]["inode_inline"] == 1
    log_text = log_file.read_text(encoding="utf-8")
    assert "收集统计信息" in log_text


def test_cli_fsck_returns_nonzero_for_unrepaired_issue(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)
    digest = "a" * 64
    orphan = fs_impl.tier1 / "blocks" / digest[:2] / digest[2:4] / digest
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "fsck",
                str(fs_impl.tier1),
                str(fs_impl.tier2),
                "--database",
                str(fs_impl.database),
            ]
        )
    assert excinfo.value.code == 1
    assert "orphan_block_file" in capsys.readouterr().out


def test_cli_fsck_repair_exits_successfully(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)
    digest = "a" * 64
    orphan = fs_impl.tier1 / "blocks" / digest[:2] / digest[2:4] / digest
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    main(
        [
            "fsck",
            str(fs_impl.tier1),
            str(fs_impl.tier2),
            "--database",
            str(fs_impl.database),
            "--repair",
        ]
    )
    assert "orphan_block_file" in capsys.readouterr().out
    assert not orphan.exists()


def test_cli_fsck_accepts_database_as_maintenance_entrypoint(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)
    with adapted(fs_impl) as fs:
        fh = fs("create", "/note.txt", 0o644)
        fs("write", "/note.txt", b"hello", 0, fh)

    main(["fsck", str(fs_impl.database)])
    assert "fsck: ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra_args", "expected_foreground"),
    [
        ([], True),
        (["--background"], False),
    ],
)
def test_cli_mount_runs_in_foreground_by_default(
    tmp_path, monkeypatch, extra_args, expected_foreground
):
    calls = []

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            *extra_args,
        ]
    )

    assert calls
    _, mountpoint, kwargs = calls[0]
    assert mountpoint == str(tmp_path / "mount")
    assert kwargs["foreground"] is expected_foreground
    assert kwargs["local"] is True
    assert kwargs["iosize"] == 4 * 1024 * 1024
    assert kwargs["kernel_permissions"] is True
    assert kwargs["attr_timeout"] == 5.0
    assert kwargs["entry_timeout"] == 5.0
    assert kwargs["loop_clone_fd"] is False
    assert kwargs["loop_max_idle_threads"] == 10
    assert kwargs["volname"] == "mount"


def test_cli_mount_accepts_explicit_iosize(tmp_path, monkeypatch):
    calls = []

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            "--iosize",
            "32m",
        ]
    )

    assert calls
    _, _, kwargs = calls[0]
    assert kwargs["iosize"] == 32 * 1024 * 1024


def test_cli_mount_accepts_metadata_cache_and_deferred_permissions(
    tmp_path, monkeypatch
):
    calls = []

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            "--metadata-cache",
            "0",
            "--defer-permissions",
        ]
    )

    assert calls
    _, _, kwargs = calls[0]
    assert kwargs["kernel_permissions"] is False
    assert kwargs["attr_timeout"] == 0
    assert kwargs["entry_timeout"] == 0


def test_cli_mount_accepts_fuse_loop_options(tmp_path, monkeypatch):
    calls = []

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            "--fuse-loop-clone-fd",
            "--fuse-loop-max-idle-threads",
            "32",
        ]
    )

    assert calls
    _, _, kwargs = calls[0]
    assert kwargs["loop_clone_fd"] is True
    assert kwargs["loop_max_idle_threads"] == 32


def test_cli_mount_accepts_explicit_volume_name(tmp_path, monkeypatch):
    calls = []

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            "--volname",
            "ztierfs",
        ]
    )

    assert calls
    _, _, kwargs = calls[0]
    assert kwargs["volname"] == "ztierfs"


def test_cli_mount_debug_sets_debug_log_level(tmp_path, monkeypatch):
    calls = []
    log_file = tmp_path / "mount.log"

    def fake_fuse(fs, mountpoint, **kwargs):
        calls.append((fs, mountpoint, kwargs))
        fs.close()

    monkeypatch.setattr(macfusepy, "FUSE", fake_fuse)

    main(
        [
            "mount",
            str(tmp_path / "hot"),
            str(tmp_path / "cold"),
            str(tmp_path / "mount"),
            "--debug",
            "--log-file",
            str(log_file),
        ]
    )

    assert calls
    log_text = log_file.read_text(encoding="utf-8")
    assert "DEBUG" in log_text
    assert "初始化文件系统" in log_text


def test_cli_cleanup_outputs_json(tmp_path, capsys):
    fs_impl = make_fs(tmp_path)

    main(
        [
            "cleanup",
            str(fs_impl.tier1),
            str(fs_impl.tier2),
            "--database",
            str(fs_impl.database),
            "--age",
            "0",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "removed_cold_copies": 0,
        "removed_pending_deletions": 0,
        "skipped_cold_copies": 0,
        "skipped_pending_deletions": 0,
    }

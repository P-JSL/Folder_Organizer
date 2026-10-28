"""A bin query is never treated as permission to permanently delete bytes."""
import ctypes
import sys
from types import SimpleNamespace

import pytest

from folder_organizer import deletion


def windows(monkeypatch, drive_type=3, query=True):
    calls = []
    monkeypatch.setattr(deletion, "_is_windows", lambda: True)
    monkeypatch.setattr(deletion, "_drive_type", lambda root: calls.append(("drive", root)) or drive_type)
    monkeypatch.setattr(deletion, "_query_recycle_bin", lambda root: calls.append(("query", root)) or query)
    return calls


@pytest.mark.parametrize("path", [r"\\nas\documents\report.txt", r"\\?\UNC\nas\documents\report.txt",
                                 "//nas/documents/report.txt"])
def test_unc_never_attempts_a_local_bin_or_modifies_a_share(monkeypatch, path):
    calls = windows(monkeypatch)
    result = deletion.deletion_destination(path)
    assert result["kind"] == "delete_files" and result["volume_kind"] == "network"
    assert not result["recycle_only_verified"] and not calls


def test_mapped_nas_drive_never_queries_local_recycle_bin(monkeypatch):
    calls = windows(monkeypatch, drive_type=4)
    result = deletion.deletion_destination(r"Z:\Shared\report.txt")
    assert result["kind"] == "delete_files" and result["volume_kind"] == "network"
    assert calls == [("drive", "Z:\\")]


@pytest.mark.parametrize("query", [True, False])
def test_local_query_cannot_authorize_unverified_deletion(monkeypatch, query):
    calls = windows(monkeypatch, query=query)
    result = deletion.deletion_destination(r"C:\Documents\report.txt")
    assert result["kind"] == "delete_files" and result["volume_kind"] == "fixed"
    assert result["recycle_bin_checked"] and result["recycle_bin_query_succeeded"] is query
    assert result["recycle_only_verified"] is False
    assert calls == [("drive", "C:\\"), ("query", "C:\\")]


@pytest.mark.parametrize("drive_type", [0, 1, 2, 5, 6])
def test_unavailable_removable_and_nonfixed_volumes_use_safe_folder(monkeypatch, drive_type):
    calls = windows(monkeypatch, drive_type=drive_type)
    result = deletion.deletion_destination(r"D:\report.txt")
    assert result["kind"] == "delete_files"
    assert not result["recycle_bin_checked"] and calls == [("drive", "D:\\")]


def test_recycle_query_failure_does_not_fall_back_to_shell_deletion(monkeypatch):
    calls = windows(monkeypatch)
    def denied(_root):
        raise OSError("access denied")
    monkeypatch.setattr(deletion, "_query_recycle_bin", denied)
    result = deletion.deletion_destination(r"C:\file.txt")
    assert result["kind"] == "delete_files" and result["recycle_bin_query_succeeded"] is False
    assert result["reason_code"] == "recycle_bin_query_failed"


def test_volume_query_failure_preserves_safe_destination(monkeypatch):
    windows(monkeypatch)
    monkeypatch.setattr(deletion, "_drive_type", lambda _root: (_ for _ in ()).throw(OSError("unavailable")))
    assert deletion.deletion_destination(r"C:\file.txt")["reason_code"] == "volume_query_failed"


def test_nonwindows_lookup_and_recycle_refusal_leave_every_file_unchanged(monkeypatch, tmp_path):
    path = tmp_path / "report.txt"
    path.write_bytes(b"these bytes must survive")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(deletion, "_is_windows", lambda: False)
    monkeypatch.setattr(deletion, "_drive_type", lambda _root: pytest.fail("No Windows call is allowed"))
    result = deletion.deletion_destination(path)
    assert result["kind"] == "delete_files" and result["directory_name"] == "delete_files"
    with pytest.raises(deletion.RecycleUnavailableError):
        deletion.recycle_file(path, {"sha256": "0" * 64})
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "delete_files").exists()


@pytest.mark.parametrize("path", ["", "bad\0path", b"bytes-not-text", None])
def test_invalid_inputs_do_not_invoke_native_apis(monkeypatch, path):
    calls = windows(monkeypatch)
    with pytest.raises(ValueError):
        deletion.deletion_destination(path)
    assert not calls


@pytest.mark.parametrize("path", ["report.txt", "C:report.txt", r"\\.\PhysicalDrive0"])
def test_relative_and_device_paths_are_not_recycled(monkeypatch, path):
    calls = windows(monkeypatch)
    result = deletion.deletion_destination(path)
    assert result["kind"] == "delete_files" and not result["recycle_bin_checked"]
    assert not calls


def test_extended_local_drive_path_queries_its_volume_without_resolving_source(monkeypatch):
    calls = windows(monkeypatch)
    result = deletion.deletion_destination(r"\\?\C:\Documents\report.txt")
    assert result["kind"] == "delete_files"
    assert calls == [("drive", "\\\\?\\C:\\"), ("query", "\\\\?\\C:\\")]


def test_native_probe_uses_only_readonly_win32_functions(monkeypatch):
    observed = []
    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result
        def __call__(self, *args):
            observed.append(self.name)
            return self.result
    def dll(name, **_kwargs):
        if name == "kernel32":
            return SimpleNamespace(GetDriveTypeW=Function("GetDriveTypeW", 3))
        assert name == "shell32"
        return SimpleNamespace(SHQueryRecycleBinW=Function("SHQueryRecycleBinW", 0))
    monkeypatch.setattr(ctypes, "WinDLL", dll, raising=False)
    monkeypatch.setattr(deletion, "_is_windows", lambda: True)
    result = deletion.deletion_destination(r"C:\Documents\report.txt")
    assert result["kind"] == "delete_files"
    assert observed == ["GetDriveTypeW", "SHQueryRecycleBinW"]


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows capability observation")
def test_real_windows_probe_leaves_disposable_source_intact(tmp_path):
    path = tmp_path / "actual-capability-check.txt"
    path.write_bytes(b"native Windows read-only probe")
    result = deletion.deletion_destination(path)
    assert result["kind"] == "delete_files" and not result["recycle_only_verified"]
    assert path.read_bytes() == b"native Windows read-only probe"

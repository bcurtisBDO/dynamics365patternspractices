import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import fs_browser
from app.fs_browser import is_network_path, list_directory, list_roots, unreadable_file_message

NETWORK_PATHS = [
    r"\\attacker\share",
    "//attacker/share",
    r"\\?\UNC\attacker\share",
    r"\\.\pipe\x",
    r"\/attacker/share",
    "  \\\\attacker\\share",
]


def test_list_roots_includes_home():
    roots = list_roots()
    assert any(r["path"] == str(Path.home().resolve()) for r in roots)


def test_list_directory_home():
    listing = list_directory(str(Path.home()))
    assert listing["path"] == str(Path.home().resolve())
    assert isinstance(listing["entries"], list)


def test_list_directory_rejects_relative():
    with pytest.raises(ValueError, match="(?i)absolute"):
        list_directory("BPC Files")


def test_list_directory_hides_excel_lock_files(tmp_path):
    for name in ("Template.xlsx", "~$Template.xlsx", "notes.txt"):
        (tmp_path / name).write_text("")
    names = [entry["name"] for entry in list_directory(str(tmp_path))["entries"]]
    assert names == ["Template.xlsx"]


def test_windows_drives_use_listdrives(monkeypatch):
    monkeypatch.setattr(os, "listdrives", lambda: ["A:\\", "C:\\", "Z:\\", "d:\\"], raising=False)
    assert fs_browser._windows_drives() == ["C:\\", "D:\\", "Z:\\"]


def test_windows_drives_fall_back_to_logical_drive_mask(monkeypatch):
    import ctypes

    monkeypatch.delattr(os, "listdrives", raising=False)
    kernel32 = SimpleNamespace(GetLogicalDrives=lambda: 0b1101)  # A:, C:, D:
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    assert fs_browser._windows_drives() == ["C:\\", "D:\\"]


def test_list_roots_lists_windows_drives_without_probing_them(monkeypatch):
    monkeypatch.setattr(fs_browser, "_windows_drives", lambda: ["C:\\", "Z:\\"])
    monkeypatch.setattr(fs_browser, "os", SimpleNamespace(name="nt"))
    roots = list_roots()
    assert {"label": "C:", "path": "C:\\"} in roots
    assert {"label": "Z:", "path": "Z:\\"} in roots
    assert not any(root["path"] == "/" for root in roots)


def test_unreadable_file_message_is_plain():
    locked = PermissionError(13, "Permission denied", "/x/Book.xlsx")
    assert unreadable_file_message(locked).startswith("Can't open /x/Book.xlsx: permission denied.")
    assert unreadable_file_message(OSError(5, "Input/output error", "/x/Book.xlsx")) == (
        "Can't read /x/Book.xlsx: Input/output error."
    )
    assert unreadable_file_message(FileNotFoundError("Excel template not found: /x")) == (
        "Excel template not found: /x"
    )


@pytest.mark.parametrize("path", NETWORK_PATHS)
def test_network_paths_are_detected(path):
    assert is_network_path(path)


@pytest.mark.parametrize("path", ["/Users/me", r"C:\Users\me", "C:/Users/me", "~/BPC", "/", ""])
def test_local_paths_are_not_network_paths(path):
    assert not is_network_path(path)


@pytest.mark.parametrize("path", NETWORK_PATHS)
def test_list_directory_rejects_network_paths(path, monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: pytest.fail("network path was opened"))
    with pytest.raises(ValueError, match="Network paths"):
        list_directory(path)


def test_fs_list_endpoint_answers_400_for_network_paths():
    from app.main import fs_list

    with pytest.raises(HTTPException) as caught:
        fs_list(path=r"\\attacker\share", directories_only=False)
    assert caught.value.status_code == 400
    assert "Network paths" in caught.value.detail


@pytest.mark.parametrize("root", NETWORK_PATHS)
def test_outputs_endpoint_rejects_network_root(root, monkeypatch):
    from app.main import output_file

    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: pytest.fail("network path was opened"))
    with pytest.raises(HTTPException) as caught:
        output_file("org_Proj", "import-failures.csv", root=root)
    assert caught.value.status_code == 400
    assert "Network paths" in caught.value.detail

"""Local filesystem listing for the path picker (absolute paths only)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .paths import bpc_files_root, resolve_release_folder


NETWORK_PATH_ERROR = (
    "Network paths (starting with \\\\ or //) aren't supported. "
    "Choose a folder on this computer, or map the share to a drive letter."
)


def is_network_path(path_str: str) -> bool:
    """True for UNC / device paths (\\\\host\\share, //host/share, \\\\?\\UNC\\..., \\\\.\\...).

    Opening one makes Windows connect to the host over SMB and send the user's
    credentials, so a web page could trigger it with a plain GET to the runner.
    """
    head = path_str.strip()[:2]
    return len(head) == 2 and all(char in "\\/" for char in head)


def unreadable_file_message(exc: OSError) -> str:
    """Plain message for a file the runner couldn't open, without the "[Errno 13]" prefix."""
    if exc.filename is None:
        return str(exc)
    name = os.fsdecode(exc.filename)
    if isinstance(exc, PermissionError):
        return (
            f"Can't open {name}: permission denied. "
            "If it's open in another program, close it and try again."
        )
    if isinstance(exc, FileNotFoundError):
        return f"File not found: {name}"
    return f"Can't read {name}: {exc.strerror or type(exc).__name__}."


def _home() -> Path:
    return Path.home().expanduser().resolve()


def _windows_drives() -> list[str]:
    """Drive roots C:\\ to Z:\\ without touching them (a disconnected mapped drive stalls)."""
    listdrives = getattr(os, "listdrives", None)
    if listdrives is not None:
        letters = {drive[0].upper() for drive in listdrives() if drive[1:2] == ":"}
    else:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = {chr(ord("A") + bit) for bit in range(26) if mask & (1 << bit)}
    return [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ" if letter in letters]


def list_roots() -> list[dict[str, str]]:
    home = _home()
    roots: list[dict[str, str]] = [
        {"label": "Home", "path": str(home)},
    ]
    catalog = bpc_files_root()
    if catalog.is_dir():
        roots.append({"label": "BPC Files", "path": str(catalog.resolve())})
        release = resolve_release_folder(catalog)
        if release and release != catalog:
            roots.append({"label": f"Latest release ({release.name})", "path": str(release.resolve())})
    if os.name == "nt":
        # Not resolve(): a mapped network drive resolves to its \\server\share path.
        roots.extend({"label": drive[:2], "path": drive} for drive in _windows_drives())
    else:
        roots.append({"label": "/", "path": "/"})
    # de-dupe by path
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in roots:
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        unique.append(item)
    return unique


def list_directory(path_str: str | None, *, directories_only: bool = False) -> dict[str, Any]:
    if path_str and path_str.strip():
        if is_network_path(path_str):
            raise ValueError(NETWORK_PATH_ERROR)
        current = Path(path_str).expanduser()
        if not current.is_absolute():
            raise ValueError(
                f'Path must be absolute (got "{path_str}"). '
                "Paste a full path or pick a folder from the list."
            )
        current = current.resolve()
    else:
        current = _home()

    if current.is_file():
        current = current.parent

    if not current.exists():
        raise FileNotFoundError(f"Path not found: {current}")
    if not current.is_dir():
        raise ValueError(f"Not a directory: {current}")

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError as exc:
        raise PermissionError(f"Permission denied: {current}") from exc

    for child in children:
        name = child.name
        if name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir:
            # ~$Book.xlsx is the lock file Excel keeps beside an open workbook.
            if directories_only or child.suffix.lower() != ".xlsx" or name.startswith("~$"):
                continue
        entries.append(
            {
                "name": name,
                "path": str(child.resolve()),
                "kind": "dir" if is_dir else "file",
            }
        )

    parent = current.parent if current != current.parent else None
    return {
        "path": str(current),
        "parent": str(parent) if parent and parent != current else None,
        "entries": entries,
    }

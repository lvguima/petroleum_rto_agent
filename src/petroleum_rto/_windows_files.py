"""Windows file boundaries shared by sessions, credentials, and writer locks.

Files are opened without following reparse points or permitting their deletion.
Private ACLs allow only the current user and Windows' privileged system accounts.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class UnsafeWindowsFileError(ValueError):
    """A path, owner, or access-control list cannot be trusted."""


def _security_attributes() -> Any:
    import win32api  # type: ignore[import-untyped]
    import win32security  # type: ignore[import-untyped]

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    sid = win32security.ConvertSidToStringSid(owner)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = (
        win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1
        )
    )
    return attributes


def _check_security(handle: Any, *, tighten: bool = False) -> None:
    import win32security

    wanted = _security_attributes().SECURITY_DESCRIPTOR
    owner = wanted.GetSecurityDescriptorOwner()
    security = win32security.GetSecurityInfo(
        handle,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    if security.GetSecurityDescriptorOwner() != owner:
        raise UnsafeWindowsFileError("file must be owned by the current Windows user")
    if tighten:
        win32security.SetSecurityInfo(
            handle,
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            wanted.GetSecurityDescriptorDacl(),
            None,
        )
        return
    dacl = security.GetSecurityDescriptorDacl()
    if dacl is None:
        raise UnsafeWindowsFileError("file permissions are too broad")
    allowed = {
        win32security.ConvertSidToStringSid(owner),
        "S-1-5-18",  # LocalSystem
        "S-1-5-32-544",  # Builtin administrators can already take ownership.
        "S-1-3-4",  # OWNER RIGHTS
        "S-1-3-0",  # CREATOR OWNER in inheritable directory ACLs
    }
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type = ace[0][0]
        if ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if (
            ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE
            or win32security.ConvertSidToStringSid(ace[2]) not in allowed
        ):
            raise UnsafeWindowsFileError("file permissions are too broad or unsupported")


@contextmanager
def guard_directory(path: Path, *, create: bool = False, private: bool = False) -> Iterator[None]:
    """Pin every ancestor against replacement; never change an existing directory ACL."""
    import pywintypes  # type: ignore[import-untyped]
    import win32con  # type: ignore[import-untyped]
    import win32file  # type: ignore[import-untyped]

    path = path.absolute()
    if (
        not path.drive
        or path.drive.startswith("\\\\")
        or any(
            part in (".", "..") or ":" in part or part.endswith((".", " "))
            for part in path.parts[1:]
        )
    ):
        raise UnsafeWindowsFileError("only local paths without alternate streams are supported")
    handles = []
    try:
        for directory in reversed((path, *path.parents)):
            if create and not directory.exists():
                try:
                    win32file.CreateDirectory(str(directory), _security_attributes())
                except pywintypes.error as exc:
                    if exc.winerror != 183:  # Another creator may have won the race.
                        raise
            handle = win32file.CreateFile(
                str(directory),
                win32con.READ_CONTROL if private and directory == path else 0,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            handles.append(handle)
            attributes = win32file.GetFileInformationByHandle(handle)[0]
            if (
                attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & win32con.FILE_ATTRIBUTE_DIRECTORY
            ):
                raise UnsafeWindowsFileError("directory path cannot contain reparse points")
        if private:
            _check_security(handles[-1])
        yield
    except pywintypes.error as exc:
        if exc.winerror in (2, 3):
            raise FileNotFoundError("Windows directory does not exist") from exc
        raise OSError("Windows directory cannot be opened safely") from exc
    finally:
        for handle in reversed(handles):
            handle.Close()


def open_private_file(path: Path, *, writable: bool = False) -> int:
    """Open a current-user regular file; tighten writable files, validate readers only."""
    if sys.platform != "win32":
        raise OSError("Windows file protection requires Windows")
    import msvcrt

    import pywintypes
    import win32con
    import win32file

    path = path.absolute()
    if ":" in path.name or path.is_reserved() or path.name.endswith((".", " ")):
        raise UnsafeWindowsFileError("reserved names and alternate streams are unsupported")
    try:
        with guard_directory(path.parent):
            handle = win32file.CreateFile(
                str(path),
                win32con.GENERIC_READ
                | (win32con.GENERIC_WRITE | win32con.WRITE_DAC if writable else 0),
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                _security_attributes() if writable else None,
                win32con.OPEN_ALWAYS if writable else win32con.OPEN_EXISTING,
                win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            try:
                info = win32file.GetFileInformationByHandle(handle)
                if (
                    info[0]
                    & (win32con.FILE_ATTRIBUTE_REPARSE_POINT | win32con.FILE_ATTRIBUTE_DIRECTORY)
                    or info[7] != 1
                    or win32file.GetFileType(handle) != win32con.FILE_TYPE_DISK
                ):
                    raise UnsafeWindowsFileError("file must be a regular file with one link")
                _check_security(handle, tighten=writable)
                descriptor = msvcrt.open_osfhandle(
                    int(handle), (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY
                )
                handle.Detach()
                return descriptor
            finally:
                handle.Close()
    except pywintypes.error as exc:
        if exc.winerror in (2, 3):
            raise FileNotFoundError("Windows file does not exist") from exc
        raise OSError("Windows file cannot be opened safely") from exc

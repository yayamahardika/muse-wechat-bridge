"""Private runtime files and durable atomic replacement (POSIX and Windows)."""
import csv
import ctypes
import functools
import io
import json
import os
import re
import subprocess
import tempfile

_private_dirs = set()


@functools.lru_cache(maxsize=1)
def _windows_sid():
    output = subprocess.check_output(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=10,
    ).decode("utf-8", errors="replace")
    sid = next(csv.reader(io.StringIO(output)))[-1]
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("cannot determine runtime file owner")
    return sid


def _windows_private(path, directory):
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    inherit = "OICI" if directory else ""
    sddl = "D:P(A;%s;FA;;;%s)(A;%s;FA;;;SY)" % (
        inherit, _windows_sid(), inherit)
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(os.path.abspath(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def private_directory(path):
    path = os.path.abspath(path)
    existed = os.path.isdir(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "nt":
        if not existed or path not in _private_dirs:
            _windows_private(path, True)
            _private_dirs.add(path)
    else:
        os.chmod(path, 0o700)


def private_file(path):
    if os.name == "nt":
        _windows_private(path, False)
    else:
        os.chmod(path, 0o600)


def atomic_write_text(path, text):
    """Publish only a complete, fsynced, owner-private file."""
    parent = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            private_file(tmp)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

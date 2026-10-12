"""Encrypt sessions before they reach disk; never persist an encryption key."""
import base64
import binascii
import ctypes
import json
import os

from Crypto.Cipher import AES
from runtime_io import atomic_write_json, private_file

FORMAT = "wechat-bridge-session"
VERSION = 1
KEY_ENV = "WECHAT_BRIDGE_SESSION_KEY"
AAD = b"wechat-bridge-session:v1:aes-256-gcm"


class SessionEncryptionError(RuntimeError):
    """A safe, credential-free diagnostic for the command line."""


def _default_scheme():
    return "windows-dpapi" if os.name == "nt" else "aes-256-gcm"


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value):
    if not isinstance(value, str):
        raise ValueError("invalid encoded field")
    return base64.b64decode(value, validate=True)


def _environment_key():
    try:
        key = _decode(os.environ.get(KEY_ENV))
        if len(key) != 32:
            raise ValueError("invalid key size")
        return key
    except (ValueError, binascii.Error):
        raise SessionEncryptionError(
            "%s must contain a base64-encoded random 32-byte key; "
            "provide the same key to every bridge process" % KEY_ENV) from None


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise SessionEncryptionError(
            "This session requires its original Windows user profile; "
            "log in again on this machine")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD),
                    ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    result = Blob()
    # UI forbidden; intentionally omit CRYPTPROTECT_LOCAL_MACHINE.
    if not fn(ctypes.byref(source), None, None, None, None, 1,
              ctypes.byref(result)):
        raise SessionEncryptionError(
            "Windows DPAPI %s failed (code %d); run under the Windows "
            "user that owns the session" % (
                "decryption" if decrypt else "encryption", ctypes.get_last_error()))
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def protect(session):
    raw = json.dumps(session, ensure_ascii=False).encode("utf-8")
    scheme = _default_scheme()
    envelope = {"format": FORMAT, "version": VERSION, "scheme": scheme}
    if scheme == "windows-dpapi":
        envelope["ciphertext"] = _encode(_dpapi(raw))
    else:
        cipher = AES.new(_environment_key(), AES.MODE_GCM, nonce=os.urandom(12))
        cipher.update(AAD)
        ciphertext, tag = cipher.encrypt_and_digest(raw)
        envelope.update(nonce=_encode(cipher.nonce), tag=_encode(tag),
                        ciphertext=_encode(ciphertext))
    return envelope


def unprotect(envelope):
    try:
        if envelope["format"] != FORMAT or envelope["version"] != VERSION:
            raise ValueError("unknown format")
        ciphertext = _decode(envelope["ciphertext"])
        if envelope["scheme"] == "windows-dpapi":
            raw = _dpapi(ciphertext, decrypt=True)
        elif envelope["scheme"] == "aes-256-gcm":
            nonce, tag = _decode(envelope["nonce"]), _decode(envelope["tag"])
            if len(nonce) != 12 or len(tag) != 16:
                raise ValueError("invalid GCM parameters")
            cipher = AES.new(_environment_key(), AES.MODE_GCM, nonce=nonce)
            cipher.update(AAD)
            raw = cipher.decrypt_and_verify(ciphertext, tag)
        else:
            raise ValueError("unknown scheme")
        session = json.loads(raw.decode("utf-8"))
        if not isinstance(session, dict):
            raise ValueError("invalid session")
        return session
    except SessionEncryptionError:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise SessionEncryptionError(
            "Cannot decrypt session: invalid data or wrong key; "
            "restore the original file/key or log in again") from None


def save(path, session):
    # Encrypt first: even the atomic-write temporary file contains only ciphertext.
    atomic_write_json(path, protect(session))


def load(path):
    """Caller holds the session lock, including during legacy migration."""
    if not os.path.exists(path):
        return None
    private_file(path)
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (ValueError, UnicodeError):
        raise SessionEncryptionError("Cannot read session: invalid JSON") from None
    if not isinstance(value, dict):
        raise SessionEncryptionError("Cannot read session: expected an object")
    if "format" in value or "ciphertext" in value:
        return unprotect(value)
    # Accept only the known legacy shape; damaged envelopes must not be migrated.
    if not all(isinstance(value.get(k), str) and value[k].strip()
               for k in ("bot_token", "ilink_user_id")):
        raise SessionEncryptionError("Cannot migrate session: invalid legacy credentials")
    save(path, value)
    return value

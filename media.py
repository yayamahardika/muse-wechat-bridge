# -*- coding: utf-8 -*-
"""
Media handling for the WeChat iLink bridge (mirrors the field mapping used by
DONGH4O/wechat-ilink-bridge, itself derived from tencent/openclaw-weixin).

Inbound:  download from CDN, AES-128-ECB decrypt (PKCS#7).
Outbound: AES-128-ECB encrypt, getUploadUrl, POST ciphertext, sendMessage.
Typing:   getConfig -> typing_ticket -> sendTyping(1/2).
"""
import base64
import hashlib
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

from Crypto.Cipher import AES
from runtime_io import private_directory, private_file

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_MEDIA_BYTES = 25 * 1024 * 1024

ITEM_KIND = {2: "image", 3: "voice", 4: "file", 5: "video"}
UPLOAD_MEDIA_TYPE = {"image": 1, "video": 2, "file": 3, "voice": 4}
OUTGOING_ITEM_TYPE = {"image": 2, "voice": 3, "file": 4, "video": 5}
SILK_HEADERS = (b"#!SILK_V3", b"\x02#!SILK_V3")
METADATA_FIELDS = {
    "image": ("mid_size", "hd_size", "thumb_size", "thumb_width", "thumb_height"),
    "voice": ("encode_type", "bits_per_sample", "sample_rate", "playtime"),
    "file": ("file_name", "len", "md5"),
    "video": ("video_size", "play_length", "video_md5", "thumb_size", "thumb_width", "thumb_height"),
}

MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
    ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
    ".csv": "text/csv", ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".amr": "audio/amr",
    ".silk": "audio/silk", ".sil": "audio/silk", ".pcm": "audio/pcm",
    ".adpcm": "audio/adpcm", ".spx": "audio/ogg", ".speex": "audio/ogg",
    ".ogg": "audio/ogg", ".wav": "audio/wav",
}


def validate_weixin_url(url):
    """Only HTTPS endpoints within the official Weixin domain are trusted."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))):
        raise ValueError("untrusted Weixin URL")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_weixin_url(newurl)
        if (req.has_header("Authorization")
                and urllib.parse.urlsplit(req.full_url).hostname
                != urllib.parse.urlsplit(newurl).hostname):
            raise ValueError("cross-host authenticated redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_weixin_url(req, timeout):
    validate_weixin_url(req.full_url)
    return urllib.request.build_opener(_SafeRedirect()).open(req, timeout=timeout)


def read_bounded(response, limit):
    length = response.headers.get("Content-Length")
    if length is not None and int(length) > limit:
        raise ValueError("download too large")
    data = bytearray()
    while True:
        chunk = response.read(min(64 * 1024, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("download too large")


def _pad(data):
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def _unpad(data):
    if not data:
        raise ValueError("empty pkcs7 data")
    n = data[-1]
    if n < 1 or n > 16 or len(data) < n or data[-n:] != bytes([n]) * n:
        raise ValueError("bad pkcs7 padding")
    return data[:-n]


def aes_ecb_encrypt(data, key):
    return AES.new(key, AES.MODE_ECB).encrypt(_pad(data))


def aes_ecb_decrypt(data, key):
    return _unpad(AES.new(key, AES.MODE_ECB).decrypt(data))


def decode_aes_key(value):
    """Accept 32-hex string, base64(16 raw bytes), or base64(32-hex ascii)."""
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", text):
        return bytes.fromhex(text)
    try:
        raw = base64.b64decode(text)
    except Exception:
        return None
    if len(raw) == 16:
        return raw
    try:
        t = raw.decode("utf-8").strip()
    except Exception:
        return None
    if re.fullmatch(r"[a-fA-F0-9]{32}", t):
        return bytes.fromhex(t)
    return None


def sanitize_filename(name, fallback="attachment.dat"):
    raw = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', "_", str(name or fallback))
    raw = re.sub(r"\.{2,}", ".", raw).strip("._ ")
    return raw[:120] or fallback


def sniff_mime(data):
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return "audio/silk"
    if data.startswith(b"#!AMR"):
        return "audio/amr"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    return None


def extract_media_items(msg):
    """Extract media references and an allowlist of useful scalar metadata."""
    out = []
    for idx, item in enumerate(msg.get("item_list") or []):
        if not isinstance(item, dict) or type(item.get("type")) is not int:
            continue
        kind = ITEM_KIND.get(item.get("type"))
        if not kind:
            continue
        sub = item.get(kind + "_item") or {}
        if not isinstance(sub, dict):
            continue
        out.append({
            "kind": kind,
            "index": idx,
            "media": sub.get("media") or {},
            "aeskey": sub.get("aeskey"),
            "file_name": sub.get("file_name"),
            "thumb_media": sub.get("thumb_media") if kind in ("image", "video") else None,
            "metadata": {k: sub[k] for k in METADATA_FIELDS[kind]
                         if k in sub and type(sub[k]) in (str, int, float)},
        })
    return out


def download_message_attachments(session, msg, directory, prefix):
    """Original and thumbnail failures are isolated from each other and other items."""
    def download(ref, aeskey, path):
        try:
            ok, info = download_attachment(session, ref, aeskey, path)
            return {"path": path if ok else None, "mime": info if ok else None,
                    "ok": ok, "error": None if ok else info}
        except Exception as error:
            return {"path": None, "mime": None, "ok": False,
                    "error": "download failed: %s" % type(error).__name__}

    attachments = []
    for item in extract_media_items(msg):
        name = sanitize_filename(item.get("file_name") or "%s-%d.dat" % (item["kind"], item["index"]))
        path = os.path.join(directory, "%s_%d_%s" % (prefix, item["index"], name))
        attachment = download(item["media"], item.get("aeskey"), path)
        attachment.update(kind=item["kind"], index=item["index"], metadata=item["metadata"])
        thumb = item.get("thumb_media")
        if isinstance(thumb, dict) and thumb:
            # A thumbnail may carry a key different from the original image.
            aeskey = None if thumb.get("aes_key") else item.get("aeskey")
            attachment["thumbnail"] = download(thumb, aeskey, path + ".thumb.dat")
        attachments.append(attachment)
    return attachments


def _validate_silk_header(header, size):
    prefix = next((p for p in SILK_HEADERS if header.startswith(p)), None)
    if prefix is None or size <= len(prefix) + (1 if header[len(prefix):len(prefix) + 1] == b"\n" else 0):
        raise ValueError("Invalid SILK V3 audio; pass the original .mp3 to send-voice for automatic conversion")


def validate_silk_file(path):
    """Check the actual signature, not just the extension; no transcoding is performed."""
    if not os.path.isfile(path):
        raise ValueError("voice file does not exist")
    size = os.path.getsize(path)
    if size > MAX_MEDIA_BYTES:
        raise ValueError("file too large (>25MB)")
    with open(path, "rb") as stream:
        header = stream.read(32)
    _validate_silk_header(header, size)


def validate_media_spec(spec, check_file=False):
    if not isinstance(spec, dict) or spec.get("kind") not in OUTGOING_ITEM_TYPE:
        raise ValueError("kind must be image, file, video or voice")
    if not isinstance(spec.get("path"), str) or not spec["path"]:
        raise ValueError("media path is required")
    if spec["kind"] == "voice":
        extension = os.path.splitext(spec["path"])[1].lower()
        if extension not in (".mp3", ".wav", ".silk", ".sil"):
            raise ValueError("audio replies require MP3, WAV or SILK input")
        if check_file:
            if not os.path.isfile(spec["path"]) or not 0 < os.path.getsize(spec["path"]) <= MAX_MEDIA_BYTES:
                raise ValueError("audio source must be nonempty and no larger than 25 MiB")
            if extension in (".silk", ".sil"):
                validate_silk_file(spec["path"])
    if spec["kind"] == "video" and os.path.splitext(spec["path"])[1].lower() != ".mp4":
        raise ValueError("video currently requires an MP4 file")


def download_attachment(session, media_ref, aeskey_value, dest_path,
                        base_cdn=CDN_BASE, timeout=60):
    """Download + decrypt one media item. Returns (ok, mime_or_error)."""
    param = media_ref.get("encrypt_query_param")
    url = media_ref.get("full_url")
    if not url:
        if not param:
            return False, "no download param"
        url = base_cdn + "/download?encrypted_query_param=" + urllib.parse.quote(param, safe="")
    try:
        validate_weixin_url(url)
        key = decode_aes_key(aeskey_value) or decode_aes_key(media_ref.get("aes_key"))
        if (aeskey_value or media_ref.get("aes_key")) and key is None:
            return False, "invalid AES key"
        req = urllib.request.Request(url, method="GET")
        with open_weixin_url(req, timeout=timeout) as r:
            data = read_bounded(r, MAX_MEDIA_BYTES + (16 if key is not None else 0))
    except Exception as e:
        return False, "download failed: %s" % type(e).__name__
    if key is not None:
        try:
            data = aes_ecb_decrypt(data, key)
        except Exception as e:
            return False, "decrypt failed: %s" % type(e).__name__
    if len(data) > MAX_MEDIA_BYTES:
        return False, "download too large"
    private_directory(os.path.dirname(os.path.abspath(dest_path)))
    with open(dest_path, "wb") as f:
        private_file(dest_path)
        f.write(data)
    return True, sniff_mime(data)


def upload_media(api_post, session, to_user_id, file_path, kind, options=None):
    """
    Encrypt + upload a local file, return the sendMessage item dict.
    api_post: callable(base, path, body, bot_token, timeout) -> (resp, err)
    kind: image/file/video. The voice convenience entry point prepares FILE media first.
    """
    if kind == "voice":
        raise ValueError("Native VOICE bubbles are disabled; send-voice sends audio as a FILE")
    spec = dict(options or {}, kind=kind, path=file_path)
    validate_media_spec(spec)
    if not os.path.isfile(file_path):
        raise ValueError("not a file: %s" % file_path)
    size = os.path.getsize(file_path)
    if size > MAX_MEDIA_BYTES:
        raise ValueError("file too large (>25MB)")
    ext = os.path.splitext(file_path)[1].lower()
    mime = MIME_BY_EXT.get(ext)
    if not mime or (kind == "image" and not mime.startswith("image/")):
        raise ValueError("unsupported type for %s: %s" % (kind, ext or "?"))
    with open(file_path, "rb") as f:
        raw = f.read(MAX_MEDIA_BYTES + 1)
    if len(raw) > MAX_MEDIA_BYTES:
        raise ValueError("file too large (>25MB)")
    if kind == "video" and sniff_mime(raw) != "video/mp4":
        raise ValueError("video file does not have an MP4 header")
    key = os.urandom(16)
    key_hex = key.hex()
    enc = aes_ecb_encrypt(raw, key)
    filekey = os.urandom(16).hex()
    base = session.get("baseurl") or "https://ilinkai.weixin.qq.com"
    token = session["bot_token"]
    up_req = {
        "filekey": filekey,
        "media_type": UPLOAD_MEDIA_TYPE[kind],
        "to_user_id": to_user_id,
        "rawsize": len(raw),
        "rawfilemd5": hashlib.md5(raw).hexdigest(),
        "filesize": len(enc),
        "thumb_rawsize": 0,
        "thumb_rawfilemd5": "",
        "thumb_filesize": 0,
        "no_need_thumb": True,
        "aeskey": key_hex,
        "base_info": {"channel_version": "1.0.0",
                      "bot_agent": "MuseWeChatBridge/1.0"},
    }
    resp, err = api_post(base, "/ilink/bot/getuploadurl", up_req, token, 30)
    if err:
        raise RuntimeError("getUploadUrl failed: %s" % err)
    upload_url = resp.get("upload_full_url")
    if not upload_url:
        up = resp.get("upload_param")
        if not up:
            raise RuntimeError("no upload url in getUploadUrl response")
        upload_url = (CDN_BASE + "/upload?encrypted_query_param="
                      + urllib.parse.quote(up, safe="") + "&filekey=" + filekey)
    req = urllib.request.Request(upload_url, data=enc, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with open_weixin_url(req, timeout=60) as r:
            status = r.status
            dl_param = r.headers.get("x-encrypted-param")
    except Exception as e:
        raise RuntimeError("cdn upload failed: %s" % type(e).__name__)
    if status != 200 or not dl_param:
        raise RuntimeError("CDN upload missing success status or download param")
    media = {
        "encrypt_query_param": dl_param,
        "aes_key": base64.b64encode(key_hex.encode("ascii")).decode("ascii"),
        "encrypt_type": 1,
    }
    if kind == "image":
        return {"type": OUTGOING_ITEM_TYPE["image"],
                "image_item": {"media": media, "mid_size": len(enc)}}
    if kind == "video":
        return {"type": OUTGOING_ITEM_TYPE["video"],
                "video_item": {"media": media, "video_size": len(enc)}}
    return {"type": OUTGOING_ITEM_TYPE["file"],
            "file_item": {"media": media,
                          "file_name": sanitize_filename(os.path.basename(file_path)),
                          "md5": hashlib.md5(raw).hexdigest(),
                          "len": str(len(raw))}}


class Typing:
    """Best-effort typing indicator around a send."""

    def __init__(self, api_post, session, to_user_id, context_token):
        self.api_post = api_post
        self.session = session
        self.to_user_id = to_user_id
        self.context_token = context_token
        self.ticket = None

    def __enter__(self):
        base = self.session.get("baseurl") or "https://ilinkai.weixin.qq.com"
        token = self.session["bot_token"]
        try:
            resp, err = self.api_post(
                base, "/ilink/bot/getconfig",
                {"ilink_user_id": self.to_user_id,
                 "context_token": self.context_token or "",
                 "base_info": {"channel_version": "1.0.0",
                               "bot_agent": "MuseWeChatBridge/1.0"}},
                token, 15)
            if not err:
                self.ticket = (resp or {}).get("typing_ticket")
            if self.ticket:
                self.api_post(
                    base, "/ilink/bot/sendtyping",
                    {"ilink_user_id": self.to_user_id,
                     "typing_ticket": self.ticket, "status": 1,
                     "base_info": {"channel_version": "1.0.0",
                                   "bot_agent": "MuseWeChatBridge/1.0"}},
                    token, 15)
        except Exception:
            self.ticket = None
        return self

    def __exit__(self, *a):
        if not self.ticket:
            return
        base = self.session.get("baseurl") or "https://ilinkai.weixin.qq.com"
        try:
            self.api_post(
                base, "/ilink/bot/sendtyping",
                {"ilink_user_id": self.to_user_id,
                 "typing_ticket": self.ticket, "status": 2,
                 "base_info": {"channel_version": "1.0.0",
                               "bot_agent": "MuseWeChatBridge/1.0"}},
                self.session["bot_token"], 15)
        except Exception:
            pass

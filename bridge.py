#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Muse <-> WeChat bridge (Tencent iLink ClawBot protocol).

Implements the protocol described in:
  tencent/openclaw-weixin docs/protocol_zh_CN.md
  https://developers.weixin.qq.com/doc/aispeech/knowledge/openapi/Clawbotrelated.html

Transport: HTTPS + JSON, stdlib only (urllib). QR rendering needs the
`qrcode` package, used by `login` only.

Subcommands:
  login       获取登录二维码（不轮询）
  wait-login  轮询二维码状态直到确认/过期
  poll        拉取新消息 -> inbox/，自动下载媒体附件（带锁，cron 调用）
  send        发送 outbox/ 中待发送回复，支持文字/图片/文件/视频（带锁）
  send-file   发送本地文件（需先有对方消息的 context）
  send-image  发送本地图片（需先有对方消息的 context）
  send-video  发送本地 MP4 视频（需先有对方消息的 context）
  send-voice  发送音频文件卡片，MP3/WAV 直传，SILK 转 WAV
  mark        标记 inbox 消息状态（供执行者用）
  cleanup     清理 N 天前的收发记录与媒体（默认 30 天）
  serve       前台轮询循环（调试用；生产由 cron tick 驱动）
  status      运行状态摘要（凭据脱敏）

Do NOT invent interface rules: auth headers, bodies, redirects and error
handling follow the documents above.
"""
import argparse
import contextlib
import base64
import datetime
import functools
import hashlib
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

import media as media_mod
import session_store
import voice_codec
from delivery_policy import DeliveryError, classify, retry_delay, transport_error
from runtime_io import atomic_write_json, atomic_write_text, private_directory, private_file

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
INBOX = os.path.join(ROOT, "inbox")
OUTBOX = os.path.join(ROOT, "outbox")

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CHANNEL_VERSION = "1.0.0"          # our own channel version, NOT copied
BOT_AGENT = "MuseWeChatBridge/1.0"  # observational label only

POLL_HTTP_TIMEOUT = 40   # seconds; server long-poll suggests ~35s
LOGIN_WAIT_TIMEOUT = 180
LOGIN_WAIT_INTERVAL = 3


def ensure_dirs():
    for d in (STATE, INBOX, OUTBOX):
        private_directory(d)


def client_version_str():
    mm, nn, pp = (int(x) for x in CHANNEL_VERSION.split("."))
    return str((mm << 16) | (nn << 8) | pp)


def rand_uin():
    # X-WECHAT-UIN: base64(decimal string of random uint32)
    return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode("ascii")).decode("ascii")


def app_headers():
    return {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": client_version_str(),
    }


def post_headers(bot_token=None, for_qrcode=False):
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": rand_uin(),
    }
    h.update(app_headers())
    if bot_token and not for_qrcode:
        h["Authorization"] = "Bearer " + bot_token
    return h


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def http_post(base, path, body, bot_token=None, timeout=POLL_HTTP_TIMEOUT,
              for_qrcode=False):
    if bot_token and time.time() < pause_until():
        return None, "PAUSED_1H"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data,
        headers=post_headers(bot_token, for_qrcode=for_qrcode), method="POST")
    try:
        with media_mod.open_weixin_url(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
            if not isinstance(resp, dict):
                return None, "invalid_response"
            if bot_token and (resp.get("ret") == -14 or resp.get("errcode") == -14):
                set_pause(3600)
            return resp, None
    except urllib.error.HTTPError as e:
        return None, "http_%d" % e.code
    except Exception as e:  # timeout / dns / reset ...
        return None, "net_%s" % type(e).__name__


def http_get(base, path_qs, timeout=POLL_HTTP_TIMEOUT):
    req = urllib.request.Request(base + path_qs, headers=app_headers(), method="GET")
    try:
        with media_mod.open_weixin_url(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
            return (resp, None) if isinstance(resp, dict) else (None, "invalid_response")
    except urllib.error.HTTPError as e:
        return None, "http_%d" % e.code
    except Exception as e:
        return None, "net_%s" % type(e).__name__


# ---------- state ----------

def session_path():
    return os.path.join(STATE, "session.json")


def load_session():
    with Locked("session.lock") as ok:
        if not ok:
            raise session_store.SessionEncryptionError("Session is busy; retry later")
        return session_store.load(session_path())


def save_session(sess):
    with Locked("session.lock") as ok:
        if not ok:
            raise session_store.SessionEncryptionError("Session is busy; retry later")
        session_store.save(session_path(), sess)


def valid_session(sess):
    return (isinstance(sess, dict)
            and all(isinstance(sess.get(k), str) and sess[k].strip()
                    for k in ("bot_token", "ilink_user_id")))


def account_id(sess):
    identity = [sess.get("ilink_bot_id") or sess["bot_token"], sess["ilink_user_id"]]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def belongs_to_session(rec, sess, inbound=False):
    if not isinstance(rec, dict):
        return False
    user_field = "from_user_id" if inbound else "to_user_id"
    if not valid_session(sess) or rec.get(user_field) != sess["ilink_user_id"]:
        return False
    if rec.get("account_id") and rec["account_id"] != account_id(sess):
        return False
    if inbound and sess.get("ilink_bot_id") and rec.get("to_user_id"):
        return rec["to_user_id"] == sess["ilink_bot_id"]
    return True


def _stamp_legacy_records(previous):
    """Keep legacy data attached to its previous login before switching accounts."""
    owner = account_id(previous) if valid_session(previous) else "legacy-unbound"
    for directory in (INBOX, OUTBOX):
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            if not record.get("account_id"):
                record["account_id"] = owner
                atomic_write_json(path, record)
    delayed = load_delayed()
    for queue in delayed.values():
        for record in queue:
            if not record.get("account_id"):
                record["account_id"] = owner
    if delayed:
        save_delayed(delayed)


def load_cursor():
    p = os.path.join(STATE, "cursor.txt")
    if not os.path.exists(p):
        return ""
    with open(p, "r", encoding="utf-8") as f:
        return f.read().strip()


def save_cursor(buf):
    atomic_write_text(os.path.join(STATE, "cursor.txt"), buf)


def pause_until():
    p = os.path.join(STATE, "pause_until.txt")
    if not os.path.exists(p):
        return 0
    try:
        with open(p) as f:
            return float(f.read().strip())
    except Exception:
        return 0


def set_pause(seconds):
    atomic_write_text(os.path.join(STATE, "pause_until.txt"), str(time.time() + seconds))


def touch_tick():
    atomic_write_text(os.path.join(STATE, "last_tick.txt"),
                      datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat())


def read_health(sess, operation):
    if not valid_session(sess):
        return {}
    path = os.path.join(STATE, "health-%s-%s.json" % (account_id(sess), operation))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {"diagnostic_error": "invalid_health_record"}
    except (OSError, ValueError):
        return {"diagnostic_error": "invalid_health_record"}


def record_health(sess, operation, attempt=False, success=False, error=None):
    """One writer per operation lock; timestamps distinguish attempts from durable success."""
    value = read_health(sess, operation)
    now = time.time()
    if attempt:
        value["last_attempt_at"] = now
    if success:
        value.update(last_success_at=now, last_error=None, consecutive_failures=0)
    if error:
        value.update(last_error=error, last_failure_at=now,
                     consecutive_failures=value.get("consecutive_failures", 0) + 1)
    atomic_write_json(os.path.join(STATE, "health-%s-%s.json" % (account_id(sess), operation)), value)


class Locked:
    """Non-blocking exclusive lock; skip quietly if another holder exists."""

    def __init__(self, name="bridge.lock"):
        self.path = os.path.join(STATE, name)
        self.fp = None

    def __enter__(self):
        ensure_dirs()
        self.fp = open(self.path, "a+b")
        try:
            if os.name == "nt":
                self.fp.seek(0, os.SEEK_END)
                if self.fp.tell() == 0:
                    self.fp.write(b"\0")
                    self.fp.flush()
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fp.close()
            self.fp = None
            return False

    def __exit__(self, *a):
        if self.fp:
            try:
                if os.name == "nt":
                    self.fp.seek(0)
                    msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            finally:
                self.fp.close()


def serialized(func):
    @functools.wraps(func)
    def run(*args, **kwargs):
        with Locked() as ok:
            if not ok:
                print(json.dumps({"ok": True, "skipped": "locked"}))
                return 0
            return func(*args, **kwargs)
    return run


def transport_exclusive(func):
    """Account switching/cleanup must not race any staged network operation."""
    @functools.wraps(func)
    def run(*args, **kwargs):
        with contextlib.ExitStack() as stack:
            for name in ("poll.lock", "send.lock", "media-retry.lock"):
                if not stack.enter_context(Locked(name)):
                    print(json.dumps({"ok": True, "skipped": "locked"}))
                    return 0
            return func(*args, **kwargs)
    return run


def log(msg):
    ensure_dirs()
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg)
    with open(os.path.join(STATE, "bridge.log"), "a", encoding="utf-8") as f:
        f.write(line)


def _local_time(ms):
    """Format a UTC epoch-ms timestamp as Asia/Shanghai local time."""
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(
            ms / 1000, datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def log_history(direction, text, user_id=None, attachments=None):
    """Append one line to the human-readable conversation log."""
    ensure_dirs()
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dir": direction,  # "in" = from WeChat, "out" = reply to WeChat
        "user": ("..." + user_id[-8:]) if user_id else "",
        "text": text or "",
    }
    if attachments:
        entry["attachments"] = attachments
    path = os.path.join(ROOT, "history.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        private_file(path)
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- login ----------

@transport_exclusive
@serialized
def cmd_login(args):
    ensure_dirs()
    sess = load_session()
    local_tokens = [sess["bot_token"]] if sess and sess.get("bot_token") else []
    resp, err = http_post(
        DEFAULT_BASE, "/ilink/bot/get_bot_qrcode?bot_type=3",
        {"local_token_list": local_tokens}, for_qrcode=True, timeout=30)
    if err:
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1
    qrcode = resp.get("qrcode")
    img = resp.get("qrcode_img_content")
    base = resp.get("baseurl") or DEFAULT_BASE
    if not qrcode:
        print(json.dumps({"ok": False, "error": "no_qrcode_in_response"},
                         ensure_ascii=False))
        return 1
    atomic_write_json(os.path.join(STATE, "qr.json"),
                      {"qrcode": qrcode, "base": base, "at": time.time()})
    png_path = os.path.join(STATE, "qr.png")
    rendered = render_qr(qrcode, img, png_path)
    print(json.dumps({
        "ok": True,
        "qr_png": png_path if rendered else None,
        "qr_img_content": img,
        "note": "用微信扫码；之后运行 wait-login 等待确认",
    }, ensure_ascii=False))
    return 0


def render_qr(qrcode_value, img_content, png_path):
    """Render a scannable QR PNG. Returns True on success."""
    payload = None
    if img_content and isinstance(img_content, str):
        if img_content.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(img_content, method="GET")
                with media_mod.open_weixin_url(req, timeout=20) as r:
                    data = media_mod.read_bounded(r, 2 * 1024 * 1024)
                with open(png_path, "wb") as f:
                    f.write(data)
                return True
            except Exception as e:
                log("qr img download failed: %s" % e)
        else:
            payload = img_content
    if payload is None:
        payload = qrcode_value
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(png_path)
        return True
    except Exception as e:
        log("qr render failed: %s" % e)
        return False


@transport_exclusive
@serialized
def cmd_wait_login(args):
    ensure_dirs()
    qp = os.path.join(STATE, "qr.json")
    if not os.path.exists(qp):
        print(json.dumps({"ok": False, "error": "no_qr_run_login_first"},
                         ensure_ascii=False))
        return 1
    with open(qp, encoding="utf-8") as f:
        qr = json.load(f)
    base = qr.get("base") or DEFAULT_BASE
    qrcode = qr["qrcode"]
    deadline = time.time() + (args.timeout or LOGIN_WAIT_TIMEOUT)
    verify_code = args.verify_code
    while time.time() < deadline:
        qs = "qrcode=" + urllib.parse.quote(qrcode, safe="")
        if verify_code:
            qs += "&verify_code=" + urllib.parse.quote(verify_code, safe="")
        resp, err = http_get(base, "/ilink/bot/get_qrcode_status?" + qs, timeout=20)
        if err:
            log("qr status error: %s" % err)
            time.sleep(LOGIN_WAIT_INTERVAL)
            continue
        status = resp.get("status")
        if status == "confirmed":
            sess = {
                "bot_token": resp.get("bot_token"),
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "ilink_user_id": resp.get("ilink_user_id"),
                "baseurl": resp.get("baseurl") or base,
                "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            if not valid_session(sess):
                print(json.dumps({"ok": False, "error": "confirmed_but_incomplete_identity"},
                                 ensure_ascii=False))
                return 1
            media_mod.validate_weixin_url(sess["baseurl"])
            previous = load_session()
            if not valid_session(previous) or account_id(previous) != account_id(sess):
                _stamp_legacy_records(previous)
                save_cursor("")
                set_pause(0)
            save_session(sess)
            for operation in ("poll", "send"):
                health = read_health(sess, operation)
                if health.get("last_error") in ("http_401", "http_403"):
                    health["last_error"] = None
                    health["consecutive_failures"] = 0
                    atomic_write_json(os.path.join(STATE, "health-%s-%s.json" %
                                                   (account_id(sess), operation)), health)
            try:
                os.remove(qp)
            except OSError:
                pass
            print(json.dumps({"ok": True, "ilink_user_id": sess["ilink_user_id"]},
                             ensure_ascii=False))
            log("login confirmed")
            return 0
        if status == "expired":
            print(json.dumps({"ok": False, "error": "qr_expired"},
                             ensure_ascii=False))
            return 2
        if status == "need_verifycode":
            print(json.dumps(
                {"ok": False, "error": "need_verifycode",
                 "hint": "微信页面显示了验证码，请告知后用 --verify-code 重试"},
                ensure_ascii=False))
            return 3
        if status == "verify_code_blocked":
            print(json.dumps({"ok": False, "error": "verify_code_blocked"},
                             ensure_ascii=False))
            return 3
        if status == "scaned_but_redirect" and resp.get("redirect_host"):
            base = "https://" + resp["redirect_host"]
            log("qr redirect to %s" % base)
        if status == "binded_redirect":
            print(json.dumps(
                {"ok": False, "error": "binded_redirect",
                 "hint": "该 Bot 已绑定到当前实例"}, ensure_ascii=False))
            return 4
        # wait / scaned: keep polling
        time.sleep(LOGIN_WAIT_INTERVAL)
    print(json.dumps({"ok": False, "error": "wait_timeout"}, ensure_ascii=False))
    return 2


# ---------- poll / inbox ----------

def extract_text_and_media(msg):
    texts = []
    has_media = False
    for item in msg.get("item_list", []) or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == 1:
            ti = item.get("text_item") or {}
            if isinstance(ti, dict) and isinstance(ti.get("text"), str) and ti["text"]:
                texts.append(ti["text"])
        elif t in (2, 3, 4, 5):
            has_media = True
            if t == 3:
                voice = item.get("voice_item") or {}
                voice_text = voice.get("text") if isinstance(voice, dict) else None
                if isinstance(voice_text, str) and voice_text.strip():
                    texts.append(voice_text)
    return "\n".join(texts).strip(), has_media


def server_message_id(value):
    """Keep uint64 IDs lossless; never infer an ID from timestamps or sequence numbers."""
    if isinstance(value, str):
        return value.strip() or None
    if type(value) is int and value >= 0:
        return str(value)
    return None


def cached_attachments(attachments):
    """Recheck cached files: cleanup may have removed media belonging to an old message."""
    result = []
    for original in attachments or []:
        item = dict(original)
        for part in (item, item.get("thumbnail")):
            if not isinstance(part, dict):
                continue
            if part is not item:
                item["thumbnail"] = part = dict(part)
            part.pop("retry", None)  # retry only the original inbox record
            if part.get("path") and not os.path.isfile(part["path"]):
                part.update(path=None, ok=False, error="cached attachment no longer available")
        result.append(item)
    return result


def find_cached_reference(message_id, session):
    """Resolve only exact server/item IDs in this account and peer's persisted records."""
    candidates = []
    for directory, inbound in ((INBOX, True), (OUTBOX, False)):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as stream:
                    record = json.load(stream)
                if not isinstance(record, dict) or not belongs_to_session(record, session, inbound=inbound):
                    continue
                if inbound:
                    if server_message_id(record.get("message_id")) == message_id:
                        candidates.append(record)
                    else:
                        candidates.extend(item for item in record.get("item_messages", [])
                                          if server_message_id(item.get("msg_id")) == message_id)
                else:
                    for part in record.get("delivery") or []:
                        if not part.get("sent") or server_message_id(part.get("message_id")) != message_id:
                            continue
                        text, has_media = extract_text_and_media({"item_list": part.get("item_list") or []})
                        attachments = []
                        spec = record.get("media")
                        if has_media and spec:
                            path = spec["path"]
                            attachments.append({"kind": spec["kind"], "path": path, "ok": True,
                                                "mime": media_mod.MIME_BY_EXT.get(os.path.splitext(path)[1].lower()),
                                                "error": None, "metadata": {
                                                    k: spec[k] for k in media_mod.METADATA_FIELDS[spec["kind"]]
                                                    if k in spec}})
                        candidates.append({"text": text, "has_media": has_media, "attachments": attachments})
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    # Conflicting local records are not a basis for guessing which message was quoted.
    matches = {}
    for candidate in candidates:
        try:
            value = {"text": candidate.get("text") or "", "has_media": bool(candidate.get("has_media")),
                     "attachments": cached_attachments(candidate.get("attachments"))}
            matches[json.dumps(value, sort_keys=True, ensure_ascii=False)] = value
        except (OSError, TypeError, ValueError):
            continue
    return next(iter(matches.values())) if len(matches) == 1 else None


def extract_references(msg, session, directory, prefix, depth=0):
    """Keep quoted context separate from the user's current instruction (max 3 levels)."""
    if depth >= 3:
        return []
    references = []
    for index, item in enumerate(msg.get("item_list") or []):
        ref = item.get("ref_msg") if isinstance(item, dict) else None
        if not isinstance(ref, dict):
            continue
        quoted = ref.get("message_item")
        content = {"item_list": [quoted]} if isinstance(quoted, dict) else {"item_list": []}
        text, has_media = extract_text_and_media(content)
        quoted_id = server_message_id(quoted.get("msg_id")) if isinstance(quoted, dict) else None
        reference_id = server_message_id(ref.get("svr_id")) or quoted_id
        key = "%s_ref-%d" % (prefix, index)
        record = {
            "item_index": index, "text": text, "has_media": has_media,
            "content_available": bool(text or has_media),
            "resolution": "inline" if text or has_media else "missing",
            "attachments": media_mod.download_message_attachments(session, content, directory, key),
            "references": extract_references(content, session, directory, key, depth + 1),
        }
        if quoted_id:
            record["msg_id"] = quoted_id
        if reference_id:
            record["reference_id"] = reference_id
            if not record["content_available"]:
                cached = find_cached_reference(reference_id, session)
                if cached is not None and (cached["text"] or cached["has_media"]):
                    record.update(cached, content_available=True, resolution="local_history")
        for field in ("title", "svr_id"):
            if isinstance(ref.get(field), str):
                record[field] = ref[field]
        partial = ref.get("partial_text")
        if isinstance(partial, dict):
            record["partial_text"] = {
                k: partial[k] for k in ("start", "end", "startindex", "endindex", "quotemd5")
                if k in partial and type(partial[k]) in (str, int)}
        if depth == 2 and isinstance(quoted, dict) and quoted.get("ref_msg"):
            record["references_truncated"] = True
        references.append(record)
    return references


def inbox_media_paths(record, depth=0):
    """Include quoted attachments and thumbnails when retaining pending-task inputs."""
    paths = set()
    for attachment in record.get("attachments") or []:
        for part in (attachment, attachment.get("thumbnail") or {}):
            if part.get("path"):
                paths.add(os.path.abspath(part["path"]))
    if depth < 3:
        for ref in record.get("references") or []:
            paths.update(inbox_media_paths(ref, depth + 1))
    return paths


def attachment_parts(record, depth=0):
    for attachment in record.get("attachments") or []:
        yield attachment
        if attachment.get("thumbnail"):
            yield attachment["thumbnail"]
    if depth < 3:
        for reference in record.get("references") or []:
            yield from attachment_parts(reference, depth + 1)


def media_retry_pending(record):
    return any(not part.get("ok") and part.get("retry")
               and not part["retry"].get("stopped")
               and part["retry"].get("attempts", 0) < 5 for part in attachment_parts(record))


def _load_inbox_record(path):
    """Validate structures traversed by recovery scans before doing any work."""
    with open(path, encoding="utf-8") as stream:
        record = json.load(stream)

    def validate(value, depth=0):
        if not isinstance(value, dict) or depth > 3:
            raise ValueError("invalid inbox record")
        for field in ("attachments", "references", "item_messages"):
            items = value.get(field) or []
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("invalid inbox collection")
        for part in attachment_parts(dict(value, references=[])):
            if not isinstance(part, dict):
                raise ValueError("invalid attachment")
            retry = part.get("retry")
            if retry is not None and (not isinstance(retry, dict)
                    or type(retry.get("attempts", 0)) is not int
                    or type(retry.get("next_attempt_at", 0)) not in (int, float)):
                raise ValueError("invalid attachment retry")
        for reference in value.get("references") or []:
            validate(reference, depth + 1)
        for item in value.get("item_messages") or []:
            validate(item, depth + 1)

    validate(record)
    return record


def _refresh_reference_snapshots(sess):
    """Also run with no due downloads, so a crash after recovery is repairable."""
    def refresh(references):
        changed = False
        for reference in references or []:
            if reference.get("resolution") == "local_history" and reference.get("reference_id"):
                cached = find_cached_reference(reference["reference_id"], sess)
                if cached is not None and any(reference.get(k) != v for k, v in cached.items()):
                    reference.update(cached)
                    changed = True
            changed = refresh(reference.get("references")) or changed
        return changed

    for name in sorted(os.listdir(INBOX)):
        if not name.endswith(".json"):
            continue
        with Locked() as ok:
            if not ok:
                return
            path = os.path.join(INBOX, name)
            try:
                record = _load_inbox_record(path)
            except (OSError, ValueError):
                continue
            if belongs_to_session(record, sess, inbound=True) and refresh(record.get("references")):
                atomic_write_json(path, record)


def cmd_retry_media(args):
    with Locked("media-retry.lock") as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0
        return _retry_media_locked(args)


def _retry_media_locked(args):
    limit = getattr(args, "limit", 20)
    if limit < 1:
        raise ValueError("limit must be positive")
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0
        sess = load_session()
    if not valid_session(sess):
        print(json.dumps({"ok": False, "error": "NO_SESSION"}))
        return 1
    attempted = recovered = 0
    for name in sorted(os.listdir(INBOX)):
        if not name.endswith(".json") or attempted >= limit:
            continue
        path = os.path.join(INBOX, name)
        try:
            record = _load_inbox_record(path)
        except (OSError, ValueError):
            log("invalid inbox record retained: %s" % name)
            continue
        if not belongs_to_session(record, sess, inbound=True):
            continue
        changed = False
        for part in attachment_parts(record):
            if attempted >= limit:
                break
            if media_mod.retry_attachment(sess, part):
                record_health(sess, "media", attempt=True)
                attempted += 1
                recovered += int(bool(part.get("ok")))
                changed = True
        if changed:
            with Locked() as ok:
                if not ok:
                    # No progress commit: next retry is safe and downloads are idempotent.
                    print(json.dumps({"ok": True, "skipped": "locked", "attempted": attempted}))
                    return 0
                current = _read_task(name[:-5], sess)
                current["attachments"] = record["attachments"]
                current["references"] = record.get("references", [])
                for item in current.get("item_messages") or []:
                    indexes = {a.get("index") for a in item.get("attachments") or []}
                    item["attachments"] = [a for a in current.get("attachments") or []
                                           if a.get("index") in indexes]
                atomic_write_json(path, current)
                record_health(sess, "media", success=all(p.get("ok") for p in attachment_parts(current)),
                              error="attachments_unavailable" if any(not p.get("ok") for p in attachment_parts(current)) else None)
    _refresh_reference_snapshots(sess)
    print(json.dumps({"ok": True, "attempted": attempted, "recovered": recovered}))
    return 0


def cmd_poll(args):
    ensure_dirs()
    with Locked("poll.lock") as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}, ensure_ascii=False))
            return 0
        return _poll_locked(args)


def _poll_locked(args):
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0
        sess = load_session()
        cursor = load_cursor()
    if not valid_session(sess):
        print(json.dumps({"ok": False, "error": "NO_SESSION"}, ensure_ascii=False))
        return 0
    if time.time() < pause_until():
        print(json.dumps({"ok": True, "skipped": "paused"}, ensure_ascii=False))
        return 0
    base = sess.get("baseurl") or DEFAULT_BASE
    token = sess["bot_token"]
    batch = time.strftime("%Y%m%dT%H%M%S")
    record_health(sess, "poll", attempt=True)
    resp, err = http_post(
        base, "/ilink/bot/getupdates",
        {"get_updates_buf": cursor, "base_info": base_info()},
        bot_token=token, timeout=POLL_HTTP_TIMEOUT)
    touch_tick()
    if err:
        record_health(sess, "poll", error=err)
        if err in ("http_401", "http_403"):
            print(json.dumps({"ok": False, "error": "NEED_LOGIN"},
                             ensure_ascii=False))
            return 0
        log("getupdates network error: %s" % err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 0
    ret = resp.get("ret")
    if ret == -14 or resp.get("errcode") == -14:
        record_health(sess, "poll", error="PAUSED_1H")
        # documented: pause the account session for one hour
        set_pause(3600)
        log("getupdates ret -14: pausing 1h per protocol doc")
        print(json.dumps({"ok": False, "error": "PAUSED_1H"}, ensure_ascii=False))
        return 0
    errcode = resp.get("errcode")
    if ret not in (0, None) or errcode not in (0, None):
        code = ret if ret not in (0, None) else errcode
        record_health(sess, "poll", error="business_code_%s" % code)
        log("getupdates business error: code=%s" % code)
        print(json.dumps({"ok": False, "error": "business_code_%s" % code},
                         ensure_ascii=False))
        return 0
    prepared = {}
    for message in resp.get("msgs", []) or []:
        if (message.get("message_type") != 1 or message.get("group_id")
                or not belongs_to_session(message, sess, inbound=True)):
            continue
        key = _inbound_key(message, sess)
        if os.path.exists(os.path.join(INBOX, key + ".json")):
            continue
        directory = os.path.join(ROOT, "media", time.strftime("%Y-%m-%d"))
        prepared[key] = (
            media_mod.download_message_attachments(sess, message, directory, key),
            extract_references(message, sess, directory, key))
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0  # cursor stays unchanged; staged downloads are safe to repeat
        current = load_session()
        if current != sess or load_cursor() != cursor:
            print(json.dumps({"ok": False, "error": "poll_snapshot_changed"}))
            return 1
        try:
            result = _persist_poll(sess, resp, batch, prepared)
        except Exception as error:
            record_health(sess, "poll", error=type(error).__name__)
            raise
        record_health(sess, "poll", success=True)
        return result


def _inbound_key(message, sess):
    key = "%s_%s" % (message.get("seq"), message.get("message_id"))
    outbox_path(key)
    path = os.path.join(INBOX, key + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            previous = json.load(stream)
        if not belongs_to_session(previous, sess, inbound=True):
            key = account_id(sess)[:16] + "_" + key
    return key


def _persist_poll(sess, resp, batch, prepared):
    buf = resp.get("get_updates_buf")
    added = 0
    for m in resp.get("msgs", []) or []:
        if m.get("message_type") != 1:  # only inbound user messages
            continue
        if m.get("group_id"):  # ignore group chats
            log("skip group message")
            continue
        if not belongs_to_session(m, sess, inbound=True):
            log("skip message from non-bound user")
            continue
        key = "%s_%s" % (m.get("seq"), m.get("message_id"))
        outbox_path(key)
        ipath = os.path.join(INBOX, key + ".json")
        if os.path.exists(ipath):
            with open(ipath, encoding="utf-8") as f:
                previous = json.load(f)
            if belongs_to_session(previous, sess, inbound=True):
                if not previous.get("delayed_flushed", True):
                    previous["delayed_flushed"] = _flush_delayed(m.get("from_user_id"), m.get("context_token"))
                    atomic_write_json(ipath, previous)
                continue  # dedup
            # Different accounts can reuse sequence/message IDs; retain the old record.
            key = account_id(sess)[:16] + "_" + key
            ipath = os.path.join(INBOX, key + ".json")
            if os.path.exists(ipath):
                with open(ipath, encoding="utf-8") as f:
                    previous = json.load(f)
                if not previous.get("delayed_flushed", True):
                    previous["delayed_flushed"] = _flush_delayed(m.get("from_user_id"), m.get("context_token"))
                    atomic_write_json(ipath, previous)
                continue
        text, has_media = extract_text_and_media(m)
        rec = {
            "key": key,
            "account_id": account_id(sess),
            "message_id": m.get("message_id"),
            "seq": m.get("seq"),
            "from_user_id": m.get("from_user_id"),
            "to_user_id": m.get("to_user_id"),
            "text": text,
            "has_media": has_media,
            "context_token": m.get("context_token"),
            "create_time_ms": m.get("create_time_ms"),
            # 本地时间（Asia/Shanghai）：worker 判断"今天/昨天"时直接用这个字段，
            # 不要对 create_time_ms 自行换算（它是 UTC 毫秒，裸读小时数会差 8 小时）。
            "local_time": _local_time(m.get("create_time_ms")),
            "batch": batch,
            "status": "new",
            "attachments": [],
            "delayed_flushed": False,
        }
        rec["attachments"], rec["references"] = prepared[key]
        _resolve_staged_references(rec["references"], sess)
        rec["item_messages"] = []
        for index, item in enumerate(m.get("item_list") or []):
            item_id = server_message_id(item.get("msg_id")) if isinstance(item, dict) else None
            if item_id:
                item_text, item_has_media = extract_text_and_media({"item_list": [item]})
                rec["item_messages"].append({
                    "msg_id": item_id, "text": item_text, "has_media": item_has_media,
                    "attachments": [a for a in rec["attachments"] if a.get("index") == index]})
        atomic_write_json(ipath, rec)
        added += 1
        log_history(
            "in",
            text if text else "[无文字]",
            m.get("from_user_id"),
            attachments=[{"kind": a["kind"], "path": a["path"], "ok": a["ok"]}
                         for a in rec["attachments"]] or None,
        )
        log("inbox new %s media=%s text_len=%d attachments=%d"
            % (key, has_media, len(text), len(rec["attachments"])))
        # flush delayed queue: retry first queued reply with fresh context
        rec["delayed_flushed"] = _flush_delayed(m.get("from_user_id"), m.get("context_token"))
        atomic_write_json(ipath, rec)
    # A replay after a crash is safe because inbox writes are atomic and deduplicated.
    if buf:
        save_cursor(buf)
    print(json.dumps({"ok": True, "new": added}, ensure_ascii=False))
    return 0


def _resolve_staged_references(references, sess):
    """Earlier messages in this same page are now durable and can resolve ID-only quotes."""
    for reference in references:
        if reference.get("resolution") == "missing" and reference.get("reference_id"):
            cached = find_cached_reference(reference["reference_id"], sess)
            if cached and (cached["text"] or cached["has_media"]):
                reference.update(cached, content_available=True, resolution="local_history")
        _resolve_staged_references(reference.get("references") or [], sess)


def delayed_path():
    return os.path.join(STATE, "delayed.json")


def load_delayed():
    p = delayed_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_delayed(d):
    atomic_write_json(delayed_path(), d)


def _to_delayed(o):
    d = load_delayed()
    queue = d.setdefault(o["to_user_id"], [])
    if not any(entry["reply_to"] == o["reply_to"] for entry in queue):
        queue.append(o)
    save_delayed(d)
    log("moved to delayed queue for user ...%s" % o["to_user_id"][-8:])


def _flush_delayed(user_id, fresh_context_token):
    with Locked("send.lock") as ok:
        if not ok:
            return False
        _flush_delayed_locked(user_id, fresh_context_token)
        return True


def _flush_delayed_locked(user_id, fresh_context_token):
    if not user_id:
        return
    d = load_delayed()
    q = d.get(user_id)
    if not q:
        return
    sess = load_session()
    index = next((i for i, entry in enumerate(q) if belongs_to_session(entry, sess)), None)
    if index is None:
        return
    entry = q.pop(index)
    if not q:
        d.pop(user_id, None)
    entry["context_token"] = fresh_context_token or entry.get("context_token")
    entry["status"] = "pending"
    entry["attempts"] = 0
    entry["last_error"] = ""
    entry["error_kind"] = None
    entry["next_attempt_at"] = 0
    path = outbox_path(entry["reply_to"])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        # Recovery after a crash between writing outbox and removing delayed entry.
        interrupted_move = (existing.get("status") == "pending" and (
            existing.get("error_kind") == "context" or
            (existing.get("attempts", 0) >= 3 and existing.get("error_kind") in (None, "business_unknown"))))
        if existing.get("status") != "delayed" and not interrupted_move:
            save_delayed(d)
            return
        # The outbox copy can have newer progress than an interrupted queue move.
        entry = existing
        entry.update(context_token=fresh_context_token or entry.get("context_token"),
                     status="pending", attempts=0, last_error="", error_kind=None, next_attempt_at=0)
    _write_outbox(path, entry)
    save_delayed(d)
    log("flushed 1 delayed reply for user ...%s" % user_id[-8:])


# ---------- send / outbox ----------

def cmd_send(args):
    ensure_dirs()
    with Locked("send.lock") as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}, ensure_ascii=False))
            return 0
        return _send_locked(args)


def _send_locked(args):
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0
        sess = load_session()
        if valid_session(sess):
            _resume_delayed_flushes(sess)
    if not valid_session(sess):
        print(json.dumps({"ok": False, "error": "NO_SESSION"}, ensure_ascii=False))
        return 0
    if time.time() < pause_until():
        print(json.dumps({"ok": True, "skipped": "paused"}))
        return 0
    base = sess.get("baseurl") or DEFAULT_BASE
    token = sess["bot_token"]
    sent, failed = 0, 0
    use_typing = getattr(args, "typing", False)
    files = sorted(f for f in os.listdir(OUTBOX) if f.endswith(".json"))
    for fn in files:
        p = os.path.join(OUTBOX, fn)
        try:
            with open(p, encoding="utf-8") as f:
                o = json.load(f)
            if not isinstance(o, dict):
                raise ValueError("invalid outbox record")
        except (OSError, ValueError):
            log("invalid outbox record retained: %s" % fn)
            failed += 1
            continue
        if o.get("status") != "pending":
            continue
        if not belongs_to_session(o, sess):
            log("skip outbox from another account: %s" % fn)
            continue
        o.setdefault("created_at", os.path.getmtime(p))
        if o.get("attempts", 0) >= 3 and not o.get("error_kind"):
            # Keep both copies until the queue move is durable; retries deduplicate.
            _to_delayed(o)
            o["status"] = "delayed"
            _write_outbox(p, o)
            failed += 1
            continue
        if o.get("next_attempt_at", 0) > time.time():
            continue
        if "delivery" not in o:
            o["delivery"] = []
            if o.get("media"):
                o["delivery"].append({"kind": "media", "client_id": uuid.uuid4().hex,
                                      "item_list": None, "sent": False})
            if o.get("text"):
                o["delivery"].append({
                    "kind": "text", "client_id": uuid.uuid4().hex, "sent": False,
                    "item_list": [{"type": 1, "text_item": {"text": o["text"]}}],
                })
            o["account_id"] = account_id(sess)
            # IDs are on disk before any network operation, including the first attempt.
            _write_outbox(p, o)

        def _do_send():
            if not o.get("context_token"):
                raise DeliveryError("context", "NO_CONTEXT_TOKEN")
            cached_voice = any(item.get("type") == 3 for part in o["delivery"]
                               for item in part.get("item_list") or [])
            if (o.get("media") or {}).get("kind") == "voice" or cached_voice:
                if not o.get("media"):
                    raise ValueError("Native VOICE is disabled; an audio source is required for FILE delivery")
                spec = o["media"]
                o["media"] = voice_codec.prepare_audio_file(
                    spec["path"], os.path.join(ROOT, "media", "audio-cache"), spec.get("source_path"))
                for part in o["delivery"]:
                    if part["kind"] == "media":
                        # A FILE is a new payload: do not reuse a VOICE upload or its client ID.
                        part.update(item_list=None, sent=False, client_id=uuid.uuid4().hex)
                        part.pop("message_id", None)
                        part.pop("silk_validated", None)
                _write_outbox(p, o)
            for part in o["delivery"]:
                if part["sent"]:
                    continue
                if time.time() < pause_until():
                    raise RuntimeError("PAUSED_1H")
                if part["item_list"] is None:
                    spec = o["media"]
                    part["item_list"] = [media_mod.upload_media(
                        http_post, sess, o["to_user_id"], spec["path"], spec["kind"], options=spec)]
                    _write_outbox(p, o)
                if any(item.get("type") == 3 for item in part["item_list"]):
                    raise ValueError("Native VOICE bubbles are disabled; use audio FILE delivery")
                body = {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": o["to_user_id"],
                        "client_id": part["client_id"],
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": o.get("context_token") or "",
                        "item_list": part["item_list"],
                    },
                    "base_info": base_info(),
                }
                resp, err = http_post(base, "/ilink/bot/sendmessage", body,
                                      bot_token=token, timeout=30)
                if err:
                    raise transport_error(err)
                # Per protocol doc: only non-zero ret is a failure;
                # missing ret is NOT a failure (server returns message_id).
                ret = (resp or {}).get("ret")
                if ret == -14 or (resp or {}).get("errcode") == -14:
                    set_pause(3600)
                    raise RuntimeError("PAUSED_1H")
                if ret not in (None, 0):
                    raise DeliveryError("business_unknown", "send_ret_%s" % ret)
                response_id = server_message_id((resp or {}).get("message_id"))
                if response_id:
                    part["message_id"] = response_id
                part["sent"] = True
                _write_outbox(p, o)

        try:
            record_health(sess, "send", attempt=True)
            if use_typing:
                with media_mod.Typing(http_post, sess, o["to_user_id"],
                                      o.get("context_token")):
                    _do_send()
            else:
                _do_send()
        except Exception as e:
            paused = time.time() < pause_until()
            kind, code = classify(e)
            if paused:
                kind, code = "paused", "PAUSED_1H"
            if not paused:
                o["attempts"] = o.get("attempts", 0) + 1
            o["last_error"] = code
            o["error_kind"] = kind
            o["last_attempt_at"] = time.time()
            o["next_attempt_at"] = (pause_until() if paused else
                                     time.time() + retry_delay(o["attempts"]))
            if kind in ("permanent", "local_io") or (kind == "transient" and o["attempts"] >= 8):
                o["status"] = "failed"
            elif kind == "auth":
                o["status"] = "blocked"
            elif kind == "context" or (kind == "business_unknown" and o["attempts"] >= 3):
                # Keep the durable pending copy if the delayed queue write fails.
                _write_outbox(p, o)
                _to_delayed(o)
                o["status"] = "delayed"
            _write_outbox(p, o)
            record_health(sess, "send", error=code)
            log("send fail %s: %s %s" % (fn, kind, code))
            failed += 1
            if paused or kind == "auth":
                break
            continue
        o["status"] = "sent"
        o["last_error"] = ""
        o["error_kind"] = None
        o["next_attempt_at"] = 0
        o["sent_at"] = time.time()
        _write_outbox(p, o)
        record_health(sess, "send", success=True)
        sent += 1
        log("sent %s" % fn)
        log_history(
            "out",
            o.get("text") or "",
            o.get("to_user_id"),
            attachments=[{"kind": o["media"]["kind"], "path": o["media"]["path"]}]
            if o.get("media") else None,
        )
    touch_tick()
    print(json.dumps({"ok": True, "sent": sent, "failed": failed},
                     ensure_ascii=False))
    return 0


def _write_outbox(path, obj):
    atomic_write_json(path, obj)


def cmd_retry_send(args):
    with Locked("send.lock") as acquired:
        if not acquired:
            print(json.dumps({"ok": True, "skipped": "locked"}))
            return 0
        with Locked() as acquired:
            if not acquired:
                print(json.dumps({"ok": True, "skipped": "locked"}))
                return 0
            sess = load_session()
            path = outbox_path(args.key)
            with open(path, encoding="utf-8") as stream:
                rec = json.load(stream)
            if not belongs_to_session(rec, sess) or rec.get("status") == "sent":
                raise ValueError("reply is not retryable in this account")
            latest = _latest_inbound(sess)
            if latest and latest.get("context_token"):
                rec["context_token"] = latest["context_token"]
            rec.update(status="pending", attempts=0, next_attempt_at=0, error_kind=None, last_error="")
            _write_outbox(path, rec)
            # Durable pending copy first; _flush_delayed also recovers interrupted removal.
            delayed = load_delayed()
            for user in list(delayed):
                delayed[user] = [item for item in delayed[user]
                                 if not (item.get("reply_to") == args.key and belongs_to_session(item, sess))]
                if not delayed[user]:
                    del delayed[user]
            save_delayed(delayed)
    print(json.dumps({"ok": True, "queued": args.key}))
    return 0


def _resume_delayed_flushes(sess):
    """Recover arrivals deferred while send held its transport lock."""
    for name in sorted(os.listdir(INBOX)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(INBOX, name)
        try:
            rec = _load_inbox_record(path)
        except (OSError, ValueError):
            log("invalid inbox record retained: %s" % name)
            continue
        if belongs_to_session(rec, sess, inbound=True) and rec.get("delayed_flushed") is False:
            _flush_delayed_locked(rec["from_user_id"], rec.get("context_token"))
            rec["delayed_flushed"] = True
            atomic_write_json(path, rec)


def outbox_path(key):
    if not isinstance(key, str) or not key or any(c in key for c in '/\\\x00:') or key in (".", ".."):
        raise ValueError("invalid message key")
    return os.path.join(OUTBOX, key + ".json")


def queue_reply(reply_to, to_user_id, context_token, text, media=None,
                task_key=None, claim_token=None):
    """Queue image/file/video or audio. The voice alias always sends playable FILE media."""
    with Locked() as ok:
        if not ok:
            raise RuntimeError("bridge busy; retry queue_reply later")
        return _queue_reply_locked(reply_to, to_user_id, context_token, text, media,
                                   task_key, claim_token)


def _queue_reply_locked(reply_to, to_user_id, context_token, text, media=None,
                        task_key=None, claim_token=None):
    sess = load_session()
    if not valid_session(sess) or to_user_id != sess["ilink_user_id"]:
        raise ValueError("reply recipient is not the bound user")
    linked = task_key or reply_to
    outbox_path(linked)
    task_path = os.path.join(INBOX, linked + ".json")
    if task_key or os.path.exists(task_path):
        rec = _read_task(linked, sess)
        _check_claim(rec, claim_token)
    p = outbox_path(reply_to)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as stream:
            existing = json.load(stream)
        if not belongs_to_session(existing, sess):
            raise ValueError("reply key belongs to another account")
        return p  # already queued/sent; never duplicate
    if media is not None:
        media_mod.validate_media_spec(media, check_file=True)
    _write_outbox(p, {
        "reply_to": reply_to,
        "account_id": account_id(sess),
        "to_user_id": to_user_id,
        "context_token": context_token or "",
        "text": text,
        "media": media,
        "status": "pending",
        "attempts": 0,
        "last_error": "",
        "created_at": time.time(),
    })
    return p


# ---------- mark / status / serve ----------

def _read_task(key, sess):
    outbox_path(key)
    rec = _load_inbox_record(os.path.join(INBOX, key + ".json"))
    if not belongs_to_session(rec, sess, inbound=True):
        raise ValueError("task belongs to another account")
    return rec


def _check_claim(rec, token):
    lease = rec.get("lease") or {}
    if (rec.get("status") != "processing" or not token
            or lease.get("token") != token or lease.get("expires_at", 0) <= time.time()):
        raise ValueError("claim missing, expired or owned by another worker")


def claim_task(key, worker, lease_seconds=300):
    """Atomically claim a new task or recover an expired, fenced lease."""
    if not isinstance(worker, str) or not worker.strip() or not 1 <= lease_seconds <= 86400:
        raise ValueError("worker and lease duration (1..86400 seconds) required")
    with Locked() as ok:
        if not ok:
            raise RuntimeError("bridge busy; retry claim later")
        rec = _read_task(key, load_session())
        lease = rec.get("lease") or {}
        recovered = (rec.get("status") == "processing" and bool(lease)
                     and lease.get("expires_at", 0) <= time.time())
        if rec.get("status") != "new" and not recovered:
            raise ValueError("task is not available")
        now = time.time()
        rec.update(status="processing", lease={"worker": worker, "token": uuid.uuid4().hex,
                   "claimed_at": now, "expires_at": now + lease_seconds}, updated_at=now)
        atomic_write_json(os.path.join(INBOX, key + ".json"), rec)
        return {"ok": True, "key": key, "claim_token": rec["lease"]["token"],
                "expires_at": rec["lease"]["expires_at"], "recovered": recovered}


def renew_task(key, claim_token, lease_seconds=300):
    if not 1 <= lease_seconds <= 86400:
        raise ValueError("lease duration must be 1..86400 seconds")
    with Locked() as ok:
        if not ok:
            raise RuntimeError("bridge busy; retry renewal later")
        rec = _read_task(key, load_session())
        _check_claim(rec, claim_token)
        rec["lease"]["expires_at"] = time.time() + lease_seconds
        rec["updated_at"] = time.time()
        atomic_write_json(os.path.join(INBOX, key + ".json"), rec)
        return {"ok": True, "expires_at": rec["lease"]["expires_at"]}


def cmd_claim(args):
    print(json.dumps(claim_task(args.key, args.worker, args.lease_seconds)))
    return 0


def cmd_renew(args):
    print(json.dumps(renew_task(args.key, args.claim_token, args.lease_seconds)))
    return 0


@serialized
def cmd_reset_task(args):
    """Explicit operator recovery; never reopens processing or completed work."""
    if not args.reason.strip():
        raise ValueError("a recovery reason is required")
    rec = _read_task(args.key, load_session())
    if rec.get("status") != "failed":
        raise ValueError("only failed tasks can be reset")
    rec.update(status="new", updated_at=time.time(),
               reset={"at": time.time(), "reason": args.reason,
                      "count": (rec.get("reset") or {}).get("count", 0) + 1})
    rec.pop("lease", None)
    # Keep last_claim_token: old workers remain fenced until a fresh claim.
    atomic_write_json(os.path.join(INBOX, args.key + ".json"), rec)
    print(json.dumps({"ok": True, "key": args.key, "status": "new"}))
    return 0


@serialized
def cmd_mark(args):
    ensure_dirs()
    outbox_path(args.key)  # apply the same path validation to inbox keys
    p = os.path.join(INBOX, args.key + ".json")
    if not os.path.exists(p):
        print(json.dumps({"ok": False, "error": "not_found"}, ensure_ascii=False))
        return 1
    rec = _read_task(args.key, load_session())
    if args.status == "processing":
        raise ValueError("use claim --worker to enter processing")
    if rec.get("lease") or rec.get("last_claim_token"):
        _check_claim(rec, getattr(args, "claim_token", None))
    rec["status"] = args.status
    if rec.get("lease"):
        rec["last_claim_token"] = rec["lease"]["token"]
    rec.pop("lease", None)
    rec["updated_at"] = time.time()
    if args.note:
        rec["note"] = args.note
    atomic_write_json(p, rec)
    print(json.dumps({"ok": True}, ensure_ascii=False))
    return 0


def redact(sess):
    if not sess:
        return None
    out = {}
    for k in ("ilink_bot_id", "ilink_user_id", "baseurl", "bound_at"):
        out[k] = sess.get(k)
    tok = sess.get("bot_token") or ""
    out["bot_token"] = ("..." + tok[-4:]) if len(tok) > 4 else "?"
    return out


def cmd_status(args):
    ensure_dirs()
    sess = load_session()
    now = time.time()
    invalid = {"inbox": 0, "outbox": 0}

    def records(directory, inbound):
        found = []
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                path = os.path.join(directory, name)
                if inbound:
                    record = _load_inbox_record(path)
                else:
                    with open(path, encoding="utf-8") as stream:
                        record = json.load(stream)
                if not isinstance(record, dict):
                    raise ValueError("invalid record")
                if belongs_to_session(record, sess, inbound=inbound):
                    record.setdefault("created_at", os.path.getmtime(path))
                    found.append(record)
            except (OSError, ValueError, TypeError):
                invalid["inbox" if inbound else "outbox"] += 1
        return found

    inbox, outbox = records(INBOX, True), records(OUTBOX, False)
    by_status, out_status = {}, {}
    expired = legacy = media_pending = media_exhausted = 0
    for record in inbox:
        state = record.get("status", "?")
        by_status[state] = by_status.get(state, 0) + 1
        if state == "processing":
            lease = record.get("lease") or {}
            expired += int(bool(lease) and lease.get("expires_at", 0) <= now)
            legacy += int(not lease)
        for part in attachment_parts(record):
            retry = part.get("retry") or {}
            if not part.get("ok") and retry:
                media_pending += int(not retry.get("stopped") and retry.get("attempts", 0) < 5)
                media_exhausted += int(bool(retry.get("stopped")) or retry.get("attempts", 0) >= 5)
    delayed_keys = set()
    for record in outbox:
        state = record.get("status", "?")
        out_status[state] = out_status.get(state, 0) + 1
        if state == "delayed":
            delayed_keys.add(record["reply_to"])
    delayed_error = None
    try:
        for queue in load_delayed().values():
            for record in queue:
                if belongs_to_session(record, sess):
                    # Ignore stale queue copies after a successful durable transition.
                    current = next((o for o in outbox if o.get("reply_to") == record.get("reply_to")), None)
                    if current is None or current.get("status") == "delayed":
                        delayed_keys.add(record["reply_to"])
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        delayed_error = "invalid_delayed_queue"
    pending = [o for o in outbox if o.get("status") == "pending"]
    health = {operation: read_health(sess, operation) for operation in ("poll", "send", "media")}
    lt = os.path.join(STATE, "last_tick.txt")
    last_tick = None
    if os.path.exists(lt):
        with open(lt, encoding="utf-8") as stream:
            last_tick = stream.read().strip()
    print(json.dumps({
        "ok": True,
        "logged_in": valid_session(sess),
        "needs_login": not valid_session(sess) or any(h.get("last_error") in ("http_401", "http_403")
                                                       for h in health.values()),
        "session": redact(sess),
        "paused": now < pause_until(),
        "inbox": {"total": len(inbox), "by_status": by_status,
                  "expired_leases": expired, "legacy_processing": legacy},
        "outbox_pending": len(pending),
        "outbox": {"total": len(outbox), "by_status": out_status,
                   "ready": sum(o.get("next_attempt_at", 0) <= now for o in pending),
                   "waiting_retry": sum(o.get("next_attempt_at", 0) > now for o in pending),
                   "delayed": len(delayed_keys),
                   "oldest_pending_age_seconds": max([max(0, now - o["created_at"]) for o in pending], default=0)},
        "media": {"pending_retry": media_pending, "exhausted": media_exhausted},
        "health": health,
        "invalid_records": invalid,
        "delayed_queue_error": delayed_error,
        "last_tick": last_tick,
        "cursor_saved": bool(load_cursor()),
    }, ensure_ascii=False, indent=2))
    return 0


def _latest_inbound(sess=None):
    """Latest inbox message from the bound user (for manual sends)."""
    sess = sess or load_session()
    if not valid_session(sess):
        return None
    best = None
    if not os.path.isdir(INBOX):
        return None
    for fn in os.listdir(INBOX):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(INBOX, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if not belongs_to_session(rec, sess, inbound=True):
            continue
        if best is None or (rec.get("create_time_ms") or 0) > (best.get("create_time_ms") or 0):
            best = rec
    return best


@serialized
def cmd_send_media(args):
    ensure_dirs()
    sess = load_session()
    if not valid_session(sess):
        print(json.dumps({"ok": False, "error": "NO_SESSION"}, ensure_ascii=False))
        return 1
    if not os.path.isfile(args.path):
        print(json.dumps({"ok": False, "error": "file not found: %s" % args.path},
                         ensure_ascii=False))
        return 1
    rec = _latest_inbound(sess)
    if not rec or not rec.get("context_token"):
        print(json.dumps(
            {"ok": False, "error": "NO_CONTEXT_TOKEN",
             "hint": "需要先收到对方一条消息再发送"}, ensure_ascii=False))
        return 1
    key = "manual_%s" % uuid.uuid4().hex[:12]
    spec = {"kind": args.kind, "path": os.path.abspath(args.path)}
    if args.kind == "voice":
        for field in ("encode_type", "sample_rate", "playtime", "bits_per_sample"):
            value = getattr(args, field, None)
            if value is not None:
                spec[field] = value
    try:
        media_mod.validate_media_spec(spec, check_file=args.kind == "voice")
    except ValueError as error:
        print(json.dumps({"ok": False, "error": "INVALID_MEDIA", "hint": str(error)}, ensure_ascii=False))
        return 1
    _write_outbox(os.path.join(OUTBOX, key + ".json"), {
        "reply_to": key,
        "account_id": account_id(sess),
        "to_user_id": rec["from_user_id"],
        "context_token": rec["context_token"],
        "text": args.text or "",
        "media": spec,
        "status": "pending",
        "attempts": 0,
        "last_error": "",
        "created_at": time.time(),
    })
    print(json.dumps({"ok": True, "queued": key}, ensure_ascii=False))
    return 0


def proactive_path():
    return os.path.join(STATE, "proactive.json")


def load_proactive():
    p = proactive_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_proactive(d):
    atomic_write_json(proactive_path(), d)


@serialized
def cmd_send_proactive(args):
    """Queue a proactive message (not a reply). Cooldown per kind."""
    ensure_dirs()
    now = time.time()
    d = load_proactive()
    last = d.get(args.kind, 0)
    if now - last < args.cooldown:
        print(json.dumps({"ok": True, "skipped": "cooldown",
                          "kind": args.kind}, ensure_ascii=False))
        return 0
    rec = _latest_inbound()
    if not rec or not rec.get("context_token"):
        print(json.dumps({"ok": False, "error": "NO_CONTEXT_TOKEN"},
                         ensure_ascii=False))
        return 1
    key = "proactive_%s" % uuid.uuid4().hex
    _queue_reply_locked(key, rec["from_user_id"], rec["context_token"], args.text)
    d[args.kind] = now
    save_proactive(d)
    print(json.dumps({"ok": True, "queued": key}, ensure_ascii=False))
    return 0


@transport_exclusive
@serialized
def cmd_cleanup(args):
    ensure_dirs()
    if args.days < 1:
        raise ValueError("cleanup days must be positive")
    cutoff = time.time() - args.days * 86400
    retained_media = set()
    for name in os.listdir(OUTBOX):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(OUTBOX, name), encoding="utf-8") as f:
                entry = json.load(f)
            if entry.get("status") != "sent" and entry.get("media"):
                retained_media.add(os.path.abspath(entry["media"]["path"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Unknown references: retain media until the damaged record is repaired.
            print(json.dumps({"ok": False, "error": "invalid_outbox_record"}))
            return 1
    for queue in load_delayed().values():
        for entry in queue:
            if entry.get("media"):
                retained_media.add(os.path.abspath(entry["media"]["path"]))
    for name in os.listdir(INBOX):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(INBOX, name), encoding="utf-8") as stream:
                entry = json.load(stream)
            if entry.get("status") not in ("done", "failed") or media_retry_pending(entry):
                retained_media.update(inbox_media_paths(entry))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print(json.dumps({"ok": False, "error": "invalid_inbox_record"}))
            return 1
    removed = 0
    for d in (INBOX, OUTBOX, os.path.join(ROOT, "media")):
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    if os.path.abspath(p) in retained_media:
                        continue
                    if d == OUTBOX and fn.endswith(".json"):
                        with open(p, encoding="utf-8") as f:
                            if json.load(f).get("status") != "sent":
                                continue  # never delete an undelivered reply
                    if d == INBOX and fn.endswith(".json"):
                        with open(p, encoding="utf-8") as f:
                            record = json.load(f)
                            if record.get("status") not in ("done", "failed") or media_retry_pending(record):
                                continue
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except (OSError, ValueError):
                    pass
    # drop empty dirs under media/
    md = os.path.join(ROOT, "media")
    if os.path.isdir(md):
        for root, dirs, files in os.walk(md, topdown=False):
            if not dirs and not files and root != md:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    print(json.dumps({"ok": True, "removed": removed, "days": args.days},
                     ensure_ascii=False))
    return 0


def cmd_serve(args):
    # Foreground poll loop (debugging). Production uses the cron tick.
    ensure_dirs()
    print("serve: foreground loop every %ss, Ctrl-C to stop" % args.every)
    try:
        while True:
            cmd_poll(args)
            cmd_retry_media(args)
            cmd_send(args)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="bridge.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")

    w = sub.add_parser("wait-login")
    w.add_argument("--timeout", type=int, default=LOGIN_WAIT_TIMEOUT)
    w.add_argument("--verify-code", default=None)

    p = sub.add_parser("poll")
    retry_media = sub.add_parser("retry-media", help="retry failed attachments using protected sources")
    retry_media.add_argument("--limit", type=int, default=20)
    retry_send = sub.add_parser("retry-send", help="requeue a failed reply, preserving delivery IDs")
    retry_send.add_argument("key")

    s = sub.add_parser("send")
    s.add_argument("--typing", action="store_true",
                   help="show typing indicator while sending")

    sf = sub.add_parser("send-file")
    sf.add_argument("path")
    sf.add_argument("--text", default="")

    si = sub.add_parser("send-image")
    si.add_argument("path")
    si.add_argument("--text", default="")

    video = sub.add_parser("send-video", help="queue an MP4 video")
    video.add_argument("path")
    video.add_argument("--text", default="")

    voice = sub.add_parser("send-voice", help="queue audio FILE: MP3/WAV directly, SILK decoded to WAV")
    voice.add_argument("path")
    voice.add_argument("--text", default="点击播放")
    voice.add_argument("--encode-type", type=int, choices=[6], default=6, help="legacy option; ignored for FILE delivery")
    voice.add_argument("--sample-rate", type=int, default=None, help="legacy option; ignored for FILE delivery")
    voice.add_argument("--playtime", type=int, default=None, help="legacy option; ignored for FILE delivery")
    voice.add_argument("--bits-per-sample", type=int, default=None, help="legacy option; ignored for FILE delivery")

    cc = sub.add_parser("cleanup")
    cc.add_argument("--days", type=int, default=30)

    sp = sub.add_parser("send-proactive")
    sp.add_argument("text")
    sp.add_argument("--kind", default="general")
    sp.add_argument("--cooldown", type=int, default=3600,
                    help="min seconds between sends of the same kind")

    m = sub.add_parser("mark")
    m.add_argument("key")
    m.add_argument("status", choices=["new", "done", "failed"])
    m.add_argument("--note", default="")
    m.add_argument("--claim-token")

    claim = sub.add_parser("claim", help="atomically claim a new or expired task")
    claim.add_argument("key")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)
    reset = sub.add_parser("reset-task", help="explicitly reopen a failed task after checking external effects")
    reset.add_argument("key")
    reset.add_argument("--reason", required=True)
    renew = sub.add_parser("renew", help="renew a live task lease")
    renew.add_argument("key")
    renew.add_argument("--claim-token", required=True)
    renew.add_argument("--lease-seconds", type=int, default=300)

    sv = sub.add_parser("serve")
    sv.add_argument("--every", type=int, default=60)

    sub.add_parser("status")

    args = ap.parse_args()
    ensure_dirs()
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "wait-login":
        return cmd_wait_login(args)
    if args.cmd == "poll":
        return cmd_poll(args)
    if args.cmd == "retry-media":
        return cmd_retry_media(args)
    if args.cmd == "retry-send":
        return cmd_retry_send(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "send-file":
        args.kind = "file"
        return cmd_send_media(args)
    if args.cmd == "send-image":
        args.kind = "image"
        return cmd_send_media(args)
    if args.cmd in ("send-video", "send-voice"):
        args.kind = "video" if args.cmd == "send-video" else "voice"
        return cmd_send_media(args)
    if args.cmd == "cleanup":
        return cmd_cleanup(args)
    if args.cmd == "send-proactive":
        return cmd_send_proactive(args)
    if args.cmd == "mark":
        return cmd_mark(args)
    if args.cmd == "claim":
        return cmd_claim(args)
    if args.cmd == "reset-task":
        return cmd_reset_task(args)
    if args.cmd == "renew":
        return cmd_renew(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except session_store.SessionEncryptionError as error:
        print(json.dumps({"ok": False, "error": "SESSION_ENCRYPTION_ERROR",
                          "hint": str(error)}, ensure_ascii=False))
        sys.exit(1)
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        sys.exit(1)

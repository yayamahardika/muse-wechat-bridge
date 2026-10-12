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
  send        发送 outbox/ 中待发送回复，支持文字/图片/文件（带锁，cron 调用）
  send-file   发送本地文件（需先有对方消息的 context）
  send-image  发送本地图片（需先有对方消息的 context）
  mark        标记 inbox 消息状态（供执行者用）
  cleanup     清理 N 天前的收发记录与媒体（默认 30 天）
  serve       前台轮询循环（调试用；生产由 cron tick 驱动）
  status      运行状态摘要（凭据脱敏）

Do NOT invent interface rules: auth headers, bodies, redirects and error
handling follow the documents above.
"""
import argparse
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
    p = session_path()
    if not os.path.exists(p):
        return None
    private_file(p)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_session(sess):
    atomic_write_json(session_path(), sess)


def valid_session(sess):
    return (isinstance(sess, dict)
            and all(isinstance(sess.get(k), str) and sess[k].strip()
                    for k in ("bot_token", "ilink_user_id")))


def account_id(sess):
    identity = [sess.get("ilink_bot_id") or sess["bot_token"], sess["ilink_user_id"]]
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def belongs_to_session(rec, sess, inbound=False):
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
    with open(os.path.join(STATE, "last_tick.txt"), "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z"))


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
        t = item.get("type")
        if t == 1:
            ti = item.get("text_item") or {}
            if ti.get("text"):
                texts.append(ti["text"])
        elif t in (2, 3, 4, 5):
            has_media = True
    return "\n".join(texts).strip(), has_media


def cmd_poll(args):
    ensure_dirs()
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}, ensure_ascii=False))
            return 0
        return _poll_locked(args)


def _poll_locked(args):
    sess = load_session()
    if not valid_session(sess):
        print(json.dumps({"ok": False, "error": "NO_SESSION"}, ensure_ascii=False))
        return 0
    if time.time() < pause_until():
        print(json.dumps({"ok": True, "skipped": "paused"}, ensure_ascii=False))
        return 0
    base = sess.get("baseurl") or DEFAULT_BASE
    token = sess["bot_token"]
    batch = time.strftime("%Y%m%dT%H%M%S")
    resp, err = http_post(
        base, "/ilink/bot/getupdates",
        {"get_updates_buf": load_cursor(), "base_info": base_info()},
        bot_token=token, timeout=POLL_HTTP_TIMEOUT)
    touch_tick()
    if err:
        if err in ("http_401", "http_403"):
            print(json.dumps({"ok": False, "error": "NEED_LOGIN"},
                             ensure_ascii=False))
            return 0
        log("getupdates network error: %s" % err)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 0
    ret = resp.get("ret")
    if ret == -14 or resp.get("errcode") == -14:
        # documented: pause the account session for one hour
        set_pause(3600)
        log("getupdates ret -14: pausing 1h per protocol doc")
        print(json.dumps({"ok": False, "error": "PAUSED_1H"}, ensure_ascii=False))
        return 0
    errcode = resp.get("errcode")
    if ret not in (0, None) or errcode not in (0, None):
        code = ret if ret not in (0, None) else errcode
        log("getupdates business error: code=%s" % code)
        print(json.dumps({"ok": False, "error": "business_code_%s" % code},
                         ensure_ascii=False))
        return 0
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
                    _flush_delayed(m.get("from_user_id"), m.get("context_token"))
                    previous["delayed_flushed"] = True
                    atomic_write_json(ipath, previous)
                continue  # dedup
            # Different accounts can reuse sequence/message IDs; retain the old record.
            key = account_id(sess)[:16] + "_" + key
            ipath = os.path.join(INBOX, key + ".json")
            if os.path.exists(ipath):
                with open(ipath, encoding="utf-8") as f:
                    previous = json.load(f)
                if not previous.get("delayed_flushed", True):
                    _flush_delayed(m.get("from_user_id"), m.get("context_token"))
                    previous["delayed_flushed"] = True
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
        # download inbound media (image/file/voice/video) for the agent
        try:
            for mi in media_mod.extract_media_items(m):
                fname = media_mod.sanitize_filename(
                    mi.get("file_name") or "%s-%d.dat" % (mi["kind"], mi["index"]))
                dest = os.path.join(
                    ROOT, "media", time.strftime("%Y-%m-%d"),
                    "%s_%d_%s" % (key, mi["index"], fname))
                ok, info = media_mod.download_attachment(
                    sess, mi["media"], mi.get("aeskey"), dest)
                rec["attachments"].append({
                    "kind": mi["kind"],
                    "path": dest if ok else None,
                    "mime": info if ok else None,
                    "ok": ok,
                    "error": None if ok else info,
                })
        except Exception as e:
            log("media download error %s: %s" % (key, e))
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
        _flush_delayed(m.get("from_user_id"), m.get("context_token"))
        rec["delayed_flushed"] = True
        atomic_write_json(ipath, rec)
    # A replay after a crash is safe because inbox writes are atomic and deduplicated.
    if buf:
        save_cursor(buf)
    print(json.dumps({"ok": True, "new": added}, ensure_ascii=False))
    return 0


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
    path = outbox_path(entry["reply_to"])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        # Recovery after a crash between writing outbox and removing delayed entry.
        if (existing.get("status") != "delayed"
                and not (existing.get("status") == "pending" and existing.get("attempts", 0) >= 3)):
            save_delayed(d)
            return
        # The outbox copy can have newer progress than an interrupted queue move.
        entry = existing
        entry.update(context_token=fresh_context_token or entry.get("context_token"),
                     status="pending", attempts=0, last_error="")
    _write_outbox(path, entry)
    save_delayed(d)
    log("flushed 1 delayed reply for user ...%s" % user_id[-8:])


# ---------- send / outbox ----------

def cmd_send(args):
    ensure_dirs()
    with Locked() as ok:
        if not ok:
            print(json.dumps({"ok": True, "skipped": "locked"}, ensure_ascii=False))
            return 0
        return _send_locked(args)


def _send_locked(args):
    sess = load_session()
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
        if o.get("attempts", 0) >= 3:
            # Keep both copies until the queue move is durable; retries deduplicate.
            _to_delayed(o)
            o["status"] = "delayed"
            _write_outbox(p, o)
            failed += 1
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
            for part in o["delivery"]:
                if part["sent"]:
                    continue
                if time.time() < pause_until():
                    raise RuntimeError("PAUSED_1H")
                if part["item_list"] is None:
                    spec = o["media"]
                    part["item_list"] = [media_mod.upload_media(
                        http_post, sess, o["to_user_id"], spec["path"], spec["kind"])]
                    _write_outbox(p, o)
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
                    raise RuntimeError("net: " + err)
                # Per protocol doc: only non-zero ret is a failure;
                # missing ret is NOT a failure (server returns message_id).
                ret = (resp or {}).get("ret")
                if ret == -14 or (resp or {}).get("errcode") == -14:
                    set_pause(3600)
                    raise RuntimeError("PAUSED_1H")
                if ret not in (None, 0):
                    raise RuntimeError("business: ret=%s" % ret)
                part["sent"] = True
                _write_outbox(p, o)

        try:
            if use_typing:
                with media_mod.Typing(http_post, sess, o["to_user_id"],
                                      o.get("context_token")):
                    _do_send()
            else:
                _do_send()
        except Exception as e:
            paused = time.time() < pause_until()
            if not paused:
                o["attempts"] = o.get("attempts", 0) + 1
            o["last_error"] = str(e)[:200]
            _write_outbox(p, o)
            log("send fail %s: %s" % (fn, e))
            failed += 1
            if paused:
                break
            continue
        o["status"] = "sent"
        o["last_error"] = ""
        _write_outbox(p, o)
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


def outbox_path(key):
    if not isinstance(key, str) or not key or any(c in key for c in '/\\\x00:') or key in (".", ".."):
        raise ValueError("invalid message key")
    return os.path.join(OUTBOX, key + ".json")


def queue_reply(reply_to, to_user_id, context_token, text, media=None):
    """Queue a reply. media: {"kind": "image"|"file", "path": ...} or None."""
    with Locked() as ok:
        if not ok:
            raise RuntimeError("bridge busy; retry queue_reply later")
        return _queue_reply_locked(reply_to, to_user_id, context_token, text, media)


def _queue_reply_locked(reply_to, to_user_id, context_token, text, media=None):
    sess = load_session()
    if not valid_session(sess) or to_user_id != sess["ilink_user_id"]:
        raise ValueError("reply recipient is not the bound user")
    p = outbox_path(reply_to)
    if os.path.exists(p):
        return p  # already queued/sent; never duplicate
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
    })
    return p


# ---------- mark / status / serve ----------

@serialized
def cmd_mark(args):
    ensure_dirs()
    outbox_path(args.key)  # apply the same path validation to inbox keys
    p = os.path.join(INBOX, args.key + ".json")
    if not os.path.exists(p):
        print(json.dumps({"ok": False, "error": "not_found"}, ensure_ascii=False))
        return 1
    with open(p, encoding="utf-8") as f:
        rec = json.load(f)
    rec["status"] = args.status
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
    inbox = [f for f in os.listdir(INBOX) if f.endswith(".json")] if os.path.isdir(INBOX) else []
    outbox = [f for f in os.listdir(OUTBOX) if f.endswith(".json")] if os.path.isdir(OUTBOX) else []
    by_status = {}
    for fn in inbox:
        try:
            with open(os.path.join(INBOX, fn), encoding="utf-8") as f:
                st = json.load(f).get("status", "?")
            by_status[st] = by_status.get(st, 0) + 1
        except Exception:
            pass
    pending_out = 0
    for fn in outbox:
        try:
            with open(os.path.join(OUTBOX, fn), encoding="utf-8") as f:
                if json.load(f).get("status") == "pending":
                    pending_out += 1
        except Exception:
            pass
    lt = os.path.join(STATE, "last_tick.txt")
    last_tick = open(lt).read().strip() if os.path.exists(lt) else None
    print(json.dumps({
        "ok": True,
        "logged_in": bool(sess and sess.get("bot_token")),
        "session": redact(sess),
        "paused": time.time() < pause_until(),
        "inbox": {"total": len(inbox), "by_status": by_status},
        "outbox_pending": pending_out,
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
    _write_outbox(os.path.join(OUTBOX, key + ".json"), {
        "reply_to": key,
        "account_id": account_id(sess),
        "to_user_id": rec["from_user_id"],
        "context_token": rec["context_token"],
        "text": args.text or "",
        "media": {"kind": args.kind, "path": os.path.abspath(args.path)},
        "status": "pending",
        "attempts": 0,
        "last_error": "",
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
                            if json.load(f).get("status") not in ("done", "failed"):
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

    s = sub.add_parser("send")
    s.add_argument("--typing", action="store_true",
                   help="show typing indicator while sending")

    sf = sub.add_parser("send-file")
    sf.add_argument("path")
    sf.add_argument("--text", default="")

    si = sub.add_parser("send-image")
    si.add_argument("path")
    si.add_argument("--text", default="")

    cc = sub.add_parser("cleanup")
    cc.add_argument("--days", type=int, default=30)

    sp = sub.add_parser("send-proactive")
    sp.add_argument("text")
    sp.add_argument("--kind", default="general")
    sp.add_argument("--cooldown", type=int, default=3600,
                    help="min seconds between sends of the same kind")

    m = sub.add_parser("mark")
    m.add_argument("key")
    m.add_argument("status", choices=["new", "processing", "done", "failed"])
    m.add_argument("--note", default="")

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
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "send-file":
        args.kind = "file"
        return cmd_send_media(args)
    if args.cmd == "send-image":
        args.kind = "image"
        return cmd_send_media(args)
    if args.cmd == "cleanup":
        return cmd_cleanup(args)
    if args.cmd == "send-proactive":
        return cmd_send_proactive(args)
    if args.cmd == "mark":
        return cmd_mark(args)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

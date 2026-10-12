"""Offline regression checks. Network access is replaced with deterministic replies."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bridge
import media
import runtime_io


class Response(io.BytesIO):
    def __init__(self, data=b"", headers=None, status=200):
        super().__init__(data)
        self.headers = headers or {}
        self.status = status


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-regression-")
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        self.paths = patch.multiple(bridge, ROOT=root,
                                    STATE=os.path.join(root, "state"),
                                    INBOX=os.path.join(root, "inbox"),
                                    OUTBOX=os.path.join(root, "outbox"))
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.stdout = patch("sys.stdout", new_callable=io.StringIO)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)
        self.network = patch.object(media, "open_weixin_url",
                                    side_effect=AssertionError("unexpected network request"))
        self.network.start()
        self.addCleanup(self.network.stop)
        bridge.ensure_dirs()
        self.session = {"bot_token": "test-placeholder", "ilink_user_id": "owner",
                        "ilink_bot_id": "bot"}
        bridge.save_session(self.session)
        self.args = SimpleNamespace(typing=False)

    def message(self, user="owner", number=1):
        return {"seq": number, "message_id": number, "message_type": 1,
                "from_user_id": user, "to_user_id": "bot", "create_time_ms": number,
                "context_token": "test-context", "item_list": [
                    {"type": 1, "text_item": {"text": "task"}}]}

    def poll(self, messages, **fields):
        response = dict(ret=0, msgs=messages, get_updates_buf="next")
        response.update(fields)
        with patch.object(bridge, "http_post", return_value=(response, None)):
            return bridge.cmd_poll(self.args)

    def read_reply(self, key="reply"):
        return json.loads(Path(bridge.outbox_path(key)).read_text(encoding="utf-8"))

    def queue(self, key="reply", media_spec=None):
        return bridge.queue_reply(key, "owner", "test-context", "caption", media_spec)

    def test_cursor_survives_failed_inbox_write(self):
        bridge.save_cursor("old")
        write = bridge.atomic_write_json

        def fail_inbox(path, value):
            if os.path.dirname(path) == bridge.INBOX:
                raise OSError("disk failure")
            return write(path, value)

        with patch.object(bridge, "atomic_write_json", side_effect=fail_inbox):
            with self.assertRaises(OSError):
                self.poll([self.message()])
        self.assertEqual(bridge.load_cursor(), "old")
        self.assertEqual(os.listdir(bridge.INBOX), [])

    def test_replay_deduplicates_persisted_messages(self):
        self.poll([self.message()])
        self.poll([self.message()])
        self.assertEqual(len(os.listdir(bridge.INBOX)), 1)
        self.assertEqual(bridge.load_cursor(), "next")

    def test_replay_recovers_interrupted_delayed_flush(self):
        self.queue()
        entry = self.read_reply()
        entry.update(status="delayed", attempts=3)
        bridge._write_outbox(bridge.outbox_path("reply"), entry)
        bridge.save_delayed({"owner": [entry]})
        with patch.object(bridge, "_flush_delayed", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.poll([self.message()])
        self.assertEqual(bridge.load_cursor(), "")
        self.poll([self.message()])
        self.assertEqual(bridge.load_delayed(), {})
        self.assertEqual(self.read_reply()["status"], "pending")
        self.assertEqual(bridge.load_cursor(), "next")

    def test_error_code_does_not_advance_cursor(self):
        bridge.save_cursor("old")
        self.poll([], errcode=123)
        self.assertEqual(bridge.load_cursor(), "old")

    def test_missing_owner_rejects_poll_and_send(self):
        self.session.pop("ilink_user_id")
        bridge.save_session(self.session)
        with patch.object(bridge, "http_post") as post:
            bridge.cmd_poll(self.args)
            bridge.cmd_send(self.args)
        post.assert_not_called()

    def test_stranger_and_group_messages_are_filtered(self):
        group = self.message(number=2)
        group["group_id"] = "group"
        self.poll([self.message("stranger"), group])
        self.assertEqual(os.listdir(bridge.INBOX), [])

    def test_latest_message_uses_current_owner_and_bot(self):
        for name, record in [("stranger", self.message("stranger", 3)),
                             ("owner", self.message(number=1)),
                             ("other-bot", dict(self.message(number=4), to_user_id="old-bot"))]:
            runtime_io.atomic_write_json(os.path.join(bridge.INBOX, name + ".json"), record)
        self.assertEqual(bridge._latest_inbound()["seq"], 1)

    def test_switch_account_does_not_send_previous_queue(self):
        self.queue()
        bridge.save_session(dict(self.session, ilink_user_id="new-owner"))
        with patch.object(bridge, "http_post") as post:
            bridge.cmd_send(self.args)
        post.assert_not_called()
        self.assertEqual(self.read_reply()["status"], "pending")

    def test_inbox_id_collision_preserves_both_accounts(self):
        self.poll([self.message()])
        bridge.save_session(dict(self.session, ilink_user_id="new-owner"))
        self.poll([self.message("new-owner")])
        self.assertEqual(len(os.listdir(bridge.INBOX)), 2)

    def test_partial_send_resumes_text_without_resending_media(self):
        self.queue(media_spec={"kind": "image", "path": "unused.png"})
        calls = []

        def post(base, path, body, **kwargs):
            calls.append(copy.deepcopy(body["msg"]))
            return (None, "timeout") if len(calls) == 2 else ({"ret": 0}, None)

        with patch.object(media, "upload_media", return_value={"type": 2}) as upload, \
                patch.object(bridge, "http_post", side_effect=post):
            bridge.cmd_send(self.args)
            progress = self.read_reply()
            self.assertTrue(progress["delivery"][0]["sent"])
            self.assertFalse(progress["delivery"][1]["sent"])
            bridge.cmd_send(self.args)
        self.assertEqual([m["item_list"][0]["type"] for m in calls], [2, 1, 1])
        self.assertEqual(calls[1]["client_id"], calls[2]["client_id"])
        upload.assert_called_once()
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_timeout_retry_reuses_persisted_client_id(self):
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "timeout")):
            bridge.cmd_send(self.args)
        first = self.read_reply()["delivery"][0]["client_id"]
        with patch.object(bridge, "http_post", return_value=({"message_id": 10}, None)) as post:
            bridge.cmd_send(self.args)
        self.assertEqual(post.call_args.args[2]["msg"]["client_id"], first)
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_pause_stops_all_sends(self):
        self.queue()
        bridge.set_pause(3600)
        with patch.object(bridge, "http_post") as post:
            bridge.cmd_send(self.args)
        post.assert_not_called()
        self.assertEqual(self.read_reply()["attempts"], 0)

    def test_negative_14_stops_batch_without_spending_retry_budget(self):
        self.queue("a")
        self.queue("b")
        with patch.object(bridge, "http_post", return_value=({"ret": -14}, None)) as post:
            bridge.cmd_send(self.args)
        self.assertEqual(post.call_count, 1)
        self.assertGreater(bridge.pause_until(), time.time())
        self.assertEqual(self.read_reply("a")["attempts"], 0)

    def test_transport_pauses_on_negative_14_and_blocks_next_api(self):
        with patch.object(media, "open_weixin_url", return_value=Response(b'{"errcode": -14}')) as remote:
            bridge.http_post(bridge.DEFAULT_BASE, "/test", {}, "test-placeholder")
            _, error = bridge.http_post(bridge.DEFAULT_BASE, "/test", {}, "test-placeholder")
        self.assertEqual(error, "PAUSED_1H")
        self.assertEqual(remote.call_count, 1)

    def test_poll_and_send_share_a_cross_process_lock(self):
        script = ("import bridge; bridge.STATE = %r; "
                  "lock = bridge.Locked(); acquired = lock.__enter__(); "
                  "lock.__exit__(); print(acquired)") % bridge.STATE
        with bridge.Locked() as acquired:
            self.assertTrue(acquired)
            with patch.object(bridge, "http_post") as post:
                bridge.cmd_poll(self.args)
                bridge.cmd_send(self.args)
                with self.assertRaises(RuntimeError):
                    self.queue()
            post.assert_not_called()
            completed = subprocess.run([sys.executable, "-B", "-c", script],
                                       cwd=os.path.dirname(bridge.__file__),
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "False")
        with bridge.Locked() as acquired:
            self.assertTrue(acquired)

    def test_failed_delayed_write_keeps_outbox_copy(self):
        self.queue()
        entry = self.read_reply()
        entry["attempts"] = 3
        bridge._write_outbox(bridge.outbox_path("reply"), entry)
        with patch.object(bridge, "save_delayed", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                bridge.cmd_send(self.args)
        self.assertEqual(self.read_reply()["status"], "pending")

    def test_delayed_move_and_flush_recover_after_interrupted_mark(self):
        self.queue()
        entry = self.read_reply()
        entry["attempts"] = 3
        bridge._write_outbox(bridge.outbox_path("reply"), entry)
        with bridge.Locked() as acquired:
            self.assertTrue(acquired)
            bridge._to_delayed(entry)
            bridge._to_delayed(entry)
            self.assertEqual(len(bridge.load_delayed()["owner"]), 1)
            bridge._flush_delayed("owner", "fresh-context")
        self.assertEqual(bridge.load_delayed(), {})
        self.assertEqual(self.read_reply()["context_token"], "fresh-context")
        self.assertEqual(self.read_reply()["attempts"], 0)

    def test_delayed_flush_never_replays_already_sent_outbox(self):
        self.queue()
        entry = self.read_reply()
        bridge.save_delayed({"owner": [entry]})
        entry["status"] = "sent"
        bridge._write_outbox(bridge.outbox_path("reply"), entry)
        with bridge.Locked():
            bridge._flush_delayed("owner", "fresh-context")
        self.assertEqual(self.read_reply()["status"], "sent")
        self.assertEqual(bridge.load_delayed(), {})

    def test_corrupt_delayed_queue_is_not_silently_discarded(self):
        Path(bridge.delayed_path()).write_text("{broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            bridge.load_delayed()

    def test_login_without_owner_keeps_previous_session(self):
        runtime_io.atomic_write_json(os.path.join(bridge.STATE, "qr.json"), {"qrcode": "test-qr"})
        with patch.object(bridge, "http_get", return_value=({"status": "confirmed", "bot_token": "test-placeholder"}, None)):
            code = bridge.cmd_wait_login(SimpleNamespace(timeout=1, verify_code=None))
        self.assertEqual(code, 1)
        self.assertEqual(bridge.load_session(), self.session)

    def test_login_account_switch_resets_cursor_and_pause(self):
        runtime_io.atomic_write_json(os.path.join(bridge.STATE, "qr.json"), {"qrcode": "test-qr"})
        bridge.save_cursor("old-account-cursor")
        bridge.set_pause(3600)
        response = dict(self.session, status="confirmed", ilink_user_id="new-owner")
        with patch.object(bridge, "http_get", return_value=(response, None)):
            bridge.cmd_wait_login(SimpleNamespace(timeout=1, verify_code=None))
        self.assertEqual(bridge.load_cursor(), "")
        self.assertLessEqual(bridge.pause_until(), time.time())

    def test_login_stamps_legacy_data_before_changing_bot(self):
        legacy = {"reply_to": "legacy", "to_user_id": "owner", "status": "pending", "text": "old"}
        bridge._write_outbox(bridge.outbox_path("legacy"), legacy)
        bridge.save_delayed({"owner": [dict(legacy, reply_to="delayed")]})
        runtime_io.atomic_write_json(os.path.join(bridge.STATE, "qr.json"), {"qrcode": "test-qr"})
        response = dict(self.session, status="confirmed", ilink_bot_id="new-bot")
        with patch.object(bridge, "http_get", return_value=(response, None)):
            bridge.cmd_wait_login(SimpleNamespace(timeout=1, verify_code=None))
        self.assertEqual(self.read_reply("legacy")["account_id"], bridge.account_id(self.session))
        self.assertEqual(bridge.load_delayed()["owner"][0]["account_id"], bridge.account_id(self.session))
        with patch.object(bridge, "http_post") as post:
            bridge.cmd_send(self.args)
        post.assert_not_called()

    def test_invalid_outbox_does_not_block_valid_replies(self):
        Path(bridge.OUTBOX, "broken.json").write_text("{broken", encoding="utf-8")
        self.queue()
        with patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            bridge.cmd_send(self.args)
        self.assertEqual(self.read_reply()["status"], "sent")
        self.assertTrue(Path(bridge.OUTBOX, "broken.json").exists())

    def test_queue_path_cannot_escape_runtime_directory(self):
        with self.assertRaises(ValueError):
            self.queue("../session")

    def test_cleanup_retains_undelivered_outbox(self):
        path = self.queue()
        os.utime(path, (0, 0))
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(os.path.isfile(path))

    def test_cleanup_retains_media_referenced_by_delayed_reply(self):
        directory = Path(bridge.ROOT, "media", "old")
        runtime_io.private_directory(str(directory))
        attachment = directory / "test.txt"
        attachment.write_bytes(b"test")
        os.utime(str(attachment), (0, 0))
        legacy = {"reply_to": "legacy", "to_user_id": "owner", "text": "old",
                  "media": {"kind": "file", "path": str(attachment)}}
        bridge.save_delayed({"owner": [legacy]})
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(attachment.exists())

    def test_atomic_failure_preserves_existing_file(self):
        path = os.path.join(bridge.STATE, "atomic.json")
        runtime_io.atomic_write_json(path, {"value": "original"})
        with patch.object(runtime_io.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                runtime_io.atomic_write_json(path, {"value": "replacement"})
        self.assertEqual(json.loads(Path(path).read_text()), {"value": "original"})
        self.assertEqual(list(Path(bridge.STATE).glob(".*.tmp")), [])

    def test_runtime_permissions_are_private(self):
        if os.name == "nt":
            from ctypes import wintypes
            import ctypes
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            size = wintypes.DWORD()
            advapi.GetFileSecurityW(str(bridge.STATE), 4, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            self.assertTrue(advapi.GetFileSecurityW(str(bridge.STATE), 4, buf, size, ctypes.byref(size)))
            text = ctypes.c_wchar_p()
            self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                buf, 1, 4, ctypes.byref(text), None))
            try:
                self.assertIn("D:P", text.value)
                sid = runtime_io._windows_sid()
                # Windows canonicalizes the local Administrator SID to the LA alias.
                owner = "LA" if sid.endswith("-500") else sid
                self.assertIn(";;;" + owner + ")", text.value)
                self.assertEqual(text.value.count("(A;"), 2)
                self.assertNotIn(";;;BU)", text.value)
                self.assertNotIn(";;;WD)", text.value)
            finally:
                kernel = ctypes.WinDLL("kernel32")
                kernel.LocalFree.argtypes = [ctypes.c_void_p]
                kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        else:
            self.assertEqual(os.stat(bridge.STATE).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(bridge.session_path()).st_mode & 0o777, 0o600)

    def test_media_rejects_local_http_and_untrusted_hosts(self):
        for url in ["file:///private", "http://novac2c.cdn.weixin.qq.com/a",
                    "https://127.0.0.1/a", "https://weixin.qq.com.attacker.invalid/a"]:
            ok, _ = media.download_attachment({}, {"full_url": url}, None,
                                               os.path.join(self.temp.name, "attachment"))
            self.assertFalse(ok)

    def test_redirect_to_untrusted_host_is_refused(self):
        request = media.urllib.request.Request(media.CDN_BASE + "/download")
        with self.assertRaises(ValueError):
            media._SafeRedirect().redirect_request(request, None, 302, "", {}, "file:///private")

    def test_authenticated_cross_host_redirect_is_refused(self):
        request = media.urllib.request.Request(bridge.DEFAULT_BASE + "/test",
                                              headers={"Authorization": "Bearer test-placeholder"})
        with self.assertRaises(ValueError):
            media._SafeRedirect().redirect_request(request, None, 302, "", {}, media.CDN_BASE)

    def test_download_size_limit_without_content_length(self):
        with patch.object(media, "MAX_MEDIA_BYTES", 8), \
                patch.object(media, "open_weixin_url", return_value=Response(b"x" * 9)):
            dest = os.path.join(self.temp.name, "attachment")
            ok, _ = media.download_attachment({}, {"full_url": media.CDN_BASE + "/download"}, None, dest)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_download_size_limit_with_content_length(self):
        response = Response(b"", {"Content-Length": str(media.MAX_MEDIA_BYTES + 1)})
        with patch.object(media, "open_weixin_url", return_value=response):
            ok, _ = media.download_attachment({}, {"full_url": media.CDN_BASE + "/download"}, None,
                                               os.path.join(self.temp.name, "attachment"))
        self.assertFalse(ok)
        self.assertEqual(response.tell() if not response.closed else 0, 0)

    def test_actual_aes_roundtrip_and_full_padding_block_at_limit(self):
        key = b"0123456789abcdef"
        raw = b"a" * 16
        encrypted = media.aes_ecb_encrypt(raw, key)
        self.assertEqual(media.aes_ecb_decrypt(encrypted, key), raw)
        dest = os.path.join(self.temp.name, "attachment")
        with patch.object(media, "MAX_MEDIA_BYTES", 16), \
                patch.object(media, "open_weixin_url", return_value=Response(encrypted)):
            ok, _ = media.download_attachment({}, {"full_url": media.CDN_BASE + "/download"}, key.hex(), dest)
        self.assertTrue(ok)
        self.assertEqual(Path(dest).read_bytes(), raw)

    def test_invalid_padding_is_rejected(self):
        with self.assertRaises(ValueError):
            media._unpad(b"abc\x02")

    def test_upload_fallback_uses_cdn_and_requires_success_header(self):
        path = os.path.join(self.temp.name, "test.txt")
        Path(path).write_bytes(b"test")
        api = lambda *args: ({"upload_param": "test-upload", "encrypt_query_param": "ignored"}, None)
        with patch.object(media, "open_weixin_url", return_value=Response(headers={"x-encrypted-param": "test-download"})) as remote:
            media.upload_media(api, self.session, "owner", path, "file")
        self.assertTrue(remote.call_args.args[0].full_url.startswith(media.CDN_BASE + "/upload?"))
        with patch.object(media, "open_weixin_url", return_value=Response(status=201)):
            with self.assertRaises(RuntimeError):
                media.upload_media(api, self.session, "owner", path, "file")


if __name__ == "__main__":
    unittest.main()

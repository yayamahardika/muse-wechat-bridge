"""Offline regression checks. Network access is replaced with deterministic replies."""
import base64
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
import session_store
import voice_codec


class Response(io.BytesIO):
    def __init__(self, data=b"", headers=None, status=200):
        super().__init__(data)
        self.headers = headers or {}
        self.status = status


class BridgeTests(unittest.TestCase):
    def setUp(self):
        key = base64.b64encode(os.urandom(32)).decode("ascii")
        key_env = patch.dict(os.environ, {session_store.KEY_ENV: key})
        key_env.start()
        self.addCleanup(key_env.stop)
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

    def test_session_file_is_encrypted_and_readable(self):
        content = Path(bridge.session_path()).read_text(encoding="utf-8")
        self.assertNotIn(self.session["bot_token"], content)
        self.assertEqual(json.loads(content)["format"], session_store.FORMAT)
        self.assertEqual(bridge.load_session(), self.session)

    def test_legacy_plaintext_session_migrates_on_read(self):
        runtime_io.atomic_write_json(bridge.session_path(), self.session)
        self.assertEqual(bridge.load_session(), self.session)
        content = Path(bridge.session_path()).read_text(encoding="utf-8")
        self.assertNotIn(self.session["bot_token"], content)
        self.assertEqual(json.loads(content)["version"], 1)
        self.assertEqual(bridge.load_session(), self.session)

    def test_failed_session_migration_keeps_original_file(self):
        runtime_io.atomic_write_json(bridge.session_path(), self.session)
        original = Path(bridge.session_path()).read_bytes()
        with patch.object(session_store, "protect", side_effect=session_store.SessionEncryptionError("unavailable")):
            with self.assertRaises(session_store.SessionEncryptionError):
                bridge.load_session()
        self.assertEqual(Path(bridge.session_path()).read_bytes(), original)

    def test_session_migration_replacement_failure_keeps_original_file(self):
        runtime_io.atomic_write_json(bridge.session_path(), self.session)
        original = Path(bridge.session_path()).read_bytes()
        with patch.object(runtime_io.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                bridge.load_session()
        self.assertEqual(Path(bridge.session_path()).read_bytes(), original)
        self.assertEqual(list(Path(bridge.STATE).glob(".*.tmp")), [])

    def test_encryption_failure_never_replaces_existing_session(self):
        original = Path(bridge.session_path()).read_bytes()
        with patch.object(session_store, "protect", side_effect=session_store.SessionEncryptionError("unavailable")):
            with self.assertRaises(session_store.SessionEncryptionError):
                bridge.save_session(dict(self.session, bot_token="replacement-token"))
        self.assertEqual(Path(bridge.session_path()).read_bytes(), original)

    def test_corrupt_encrypted_session_prevents_network_access(self):
        runtime_io.atomic_write_json(bridge.session_path(), {
            "format": session_store.FORMAT, "version": 999, "scheme": "windows-dpapi"})
        with patch.object(bridge, "http_post") as post:
            with self.assertRaises(session_store.SessionEncryptionError):
                bridge.cmd_poll(self.args)
        post.assert_not_called()

    def test_session_lock_prevents_migration_and_overwrite(self):
        original = Path(bridge.session_path()).read_bytes()
        with bridge.Locked("session.lock") as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(session_store.SessionEncryptionError):
                bridge.load_session()
            with self.assertRaises(session_store.SessionEncryptionError):
                bridge.save_session(self.session)
        self.assertEqual(Path(bridge.session_path()).read_bytes(), original)

    def test_voice_transcript_reaches_inbox_and_history_when_download_fails(self):
        message = self.message()
        message["item_list"] = [{"type": 3, "voice_item": {"text": "帮我查一下天气"}}]
        with patch.object(media, "download_attachment", return_value=(False, "no download param")), \
                patch.object(bridge, "log_history") as history:
            self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["text"], "帮我查一下天气")
        self.assertTrue(record["has_media"])
        self.assertEqual(record["status"], "new")
        self.assertEqual(record["context_token"], "test-context")
        self.assertEqual(record["attachments"][0]["kind"], "voice")
        self.assertFalse(record["attachments"][0]["ok"])
        self.assertEqual(history.call_args.args[1], record["text"])

    def test_voice_transcript_keeps_downloaded_audio_attachment(self):
        message = self.message()
        message["item_list"] = [{"type": 3, "voice_item": {
            "text": "语音任务", "media": {"encrypt_query_param": "audio-ref"}}}]
        with patch.object(media, "download_attachment", return_value=(True, "audio/silk")) as download:
            self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["text"], "语音任务")
        self.assertTrue(record["attachments"][0]["ok"])
        self.assertEqual(record["attachments"][0]["path"], download.call_args.args[3])
        self.assertEqual(download.call_args.args[1], {"encrypt_query_param": "audio-ref"})

    def test_text_and_voice_transcripts_keep_item_order(self):
        message = {"item_list": [
            {"type": 1, "text_item": {"text": "先做这件事"}},
            {"type": 3, "voice_item": {"text": "再做那件事"}},
            {"type": 1, "text_item": {"text": "补充说明"}},
            {"type": 3, "voice_item": {"text": "最后一步"}},
        ]}
        self.assertEqual(bridge.extract_text_and_media(message),
                         ("先做这件事\n再做那件事\n补充说明\n最后一步", True))

    def test_voice_without_usable_transcript_remains_media_only(self):
        for voice in (None, {}, {"text": None}, {"text": ""}, {"text": "  \n "}, {"text": 42}):
            with self.subTest(voice=voice):
                message = {"item_list": [{"type": 3, "voice_item": voice}]}
                self.assertEqual(bridge.extract_text_and_media(message), ("", True))

    def test_quoted_text_is_separate_from_current_instruction(self):
        message = self.message()
        message["item_list"][0]["ref_msg"] = {
            "title": "旧消息", "svr_id": "9007199254740993",
            "partial_text": {"start": "旧", "end": "消息", "startindex": 0, "endindex": 3},
            "message_item": {"type": 1, "text_item": {"text": "引用内容"}},
        }
        self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["text"], "task")
        ref = record["references"][0]
        self.assertEqual(ref["text"], "引用内容")
        self.assertEqual(ref["svr_id"], "9007199254740993")
        self.assertEqual(ref["partial_text"]["endindex"], 3)
        self.assertTrue(ref["content_available"])

    def test_reference_with_only_id_does_not_invent_content(self):
        message = self.message()
        message["item_list"][0]["ref_msg"] = {"svr_id": "missing", "title": "摘要"}
        self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        ref = record["references"][0]
        self.assertFalse(ref["content_available"])
        self.assertEqual(ref["text"], "")
        self.assertEqual(ref["title"], "摘要")

    def test_real_device_reference_stub_preserves_id_without_inventing_content(self):
        message = self.message(number=10)
        message.pop("message_id")
        message.update(root_id=0, parent_id=0)
        message["item_list"] = [{"type": 1, "is_completed": True,
            "text_item": {"text": "测试引用"}, "ref_msg": {"message_item": {
                "type": 0, "is_completed": True, "msg_id": "7515306531200413704"}}}]
        self.poll([message])
        record = json.loads(Path(bridge.INBOX, "10_None.json").read_text(encoding="utf-8"))
        self.assertEqual(len(record["references"]), 1)
        ref = record["references"][0]
        self.assertEqual(ref["msg_id"], "7515306531200413704")
        self.assertEqual(ref["reference_id"], "7515306531200413704")
        self.assertFalse(ref["content_available"])
        self.assertEqual(ref["resolution"], "missing")
        self.assertEqual(ref["text"], "")

    def test_server_reply_id_is_persisted_and_resolves_real_device_reference_stub(self):
        self.queue()
        message_id = "7515306531200413704"
        with patch.object(bridge, "http_post", return_value=({"ret": 0, "message_id": message_id}, None)):
            bridge.cmd_send(self.args)
        self.assertEqual(self.read_reply()["delivery"][0]["message_id"], message_id)
        message = self.message(number=2)
        message["item_list"][0]["ref_msg"] = {"message_item": {
            "type": 0, "is_completed": True, "msg_id": message_id}}
        self.poll([message])
        record = json.loads(Path(bridge.INBOX, "2_2.json").read_text(encoding="utf-8"))
        ref = record["references"][0]
        self.assertTrue(ref["content_available"])
        self.assertEqual(ref["resolution"], "local_history")
        self.assertEqual(ref["text"], "caption")
        self.assertEqual(record["text"], "task")

    def test_id_only_quote_resolves_exact_inbound_item_without_neighboring_text(self):
        original = self.message()
        original["item_list"] = [
            {"type": 1, "msg_id": "7515306531200413704", "text_item": {"text": "被引用的文字"}},
            {"type": 1, "msg_id": "7515306531200413705", "text_item": {"text": "另一条内容"}}]
        self.poll([original])
        current = self.message(number=2)
        current["item_list"][0]["ref_msg"] = {"message_item": {
            "type": 0, "msg_id": "7515306531200413704"}}
        self.poll([current])
        record = json.loads(Path(bridge.INBOX, "2_2.json").read_text(encoding="utf-8"))
        self.assertEqual(record["references"][0]["text"], "被引用的文字")

    def test_cached_quote_never_uses_other_account_or_peer(self):
        message_id = "7515306531200413704"
        for name, fields in [
                ("other-account", {"account_id": "old-account", "from_user_id": "owner"}),
                ("other-peer", {"account_id": bridge.account_id(self.session), "from_user_id": "stranger"})]:
            record = dict(fields, to_user_id="bot", message_id=message_id,
                          text="private content", has_media=False, attachments=[])
            runtime_io.atomic_write_json(os.path.join(bridge.INBOX, name + ".json"), record)
        self.assertIsNone(bridge.find_cached_reference(message_id, self.session))

    def test_conflicting_cached_ids_are_not_resolved_by_timestamp(self):
        first = self.message()
        second = self.message(number=2)
        second["message_id"] = first["message_id"]
        second["create_time_ms"] = first["create_time_ms"]
        second["item_list"][0]["text_item"]["text"] = "conflicting content"
        self.poll([first, second])
        self.assertIsNone(bridge.find_cached_reference("1", self.session))
        self.assertIsNone(bridge.find_cached_reference("unknown", self.session))

    def test_media_reply_id_resolves_media_separately_from_caption_and_checks_expiry(self):
        path = Path(self.temp.name, "image.png")
        path.write_bytes(b"image")
        self.queue(media_spec={"kind": "image", "path": str(path)})
        with patch.object(media, "upload_media", return_value={"type": 2}), \
                patch.object(bridge, "http_post", side_effect=[
                    ({"ret": 0, "message_id": "image-id"}, None),
                    ({"ret": 0, "message_id": "caption-id"}, None)]):
            bridge.cmd_send(self.args)
        image = bridge.find_cached_reference("image-id", self.session)
        self.assertEqual(image["text"], "")
        self.assertTrue(image["attachments"][0]["ok"])
        self.assertEqual(bridge.find_cached_reference("caption-id", self.session)["text"], "caption")
        path.unlink()
        expired = bridge.find_cached_reference("image-id", self.session)
        self.assertFalse(expired["attachments"][0]["ok"])
        self.assertIsNone(expired["attachments"][0]["path"])

    def test_quoted_voice_transcript_and_media_are_available(self):
        message = self.message()
        message["item_list"][0]["ref_msg"] = {"message_item": {
            "type": 3, "voice_item": {"text": "引用语音", "playtime": 1500}}}
        with patch.object(media, "download_attachment", return_value=(False, "missing audio")):
            self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        ref = record["references"][0]
        self.assertEqual(ref["text"], "引用语音")
        self.assertTrue(ref["has_media"])
        self.assertEqual(ref["attachments"][0]["metadata"]["playtime"], 1500)

    def test_reference_nesting_is_bounded(self):
        item = {"type": 1, "text_item": {"text": "context"}}
        item["ref_msg"] = {"message_item": item}
        refs = bridge.extract_references({"item_list": [item]}, self.session, self.temp.name, "safe")
        self.assertEqual(refs[0]["references"][0]["references"][0]["references"], [])
        self.assertTrue(refs[0]["references"][0]["references"][0]["references_truncated"])

    def test_original_failure_does_not_skip_thumbnail_or_next_attachment(self):
        message = self.message()
        message["item_list"] = [
            {"type": 5, "video_item": {"video_size": 120, "play_length": 3,
                "media": {"encrypt_query_param": "video"},
                "thumb_media": {"encrypt_query_param": "thumb"}}},
            {"type": 4, "file_item": {"file_name": "report.txt", "len": "12"}},
        ]
        with patch.object(media, "download_attachment", side_effect=[
                OSError("original failed"), (True, "image/png"), (True, "text/plain")]):
            self.poll([message])
        record = json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))
        video, document = record["attachments"]
        self.assertFalse(video["ok"])
        self.assertTrue(video["thumbnail"]["ok"])
        self.assertTrue(document["ok"])
        self.assertEqual(document["metadata"]["file_name"], "report.txt")
        self.assertEqual(video["metadata"]["play_length"], 3)

    def test_thumbnail_uses_its_own_decryption_key(self):
        original_key, thumb_key = b"0123456789abcdef", b"fedcba9876543210"
        png = b"\x89PNG\r\n\x1a\npreview"
        message = {"item_list": [{"type": 2, "image_item": {
            "aeskey": original_key.hex(), "media": {"full_url": media.CDN_BASE + "/image"},
            "thumb_media": {"full_url": media.CDN_BASE + "/thumb",
                            "aes_key": base64.b64encode(thumb_key).decode("ascii")}}}]}
        with patch.object(media, "open_weixin_url", side_effect=[
                Response(media.aes_ecb_encrypt(png, original_key)),
                Response(media.aes_ecb_encrypt(png, thumb_key))]):
            attachments = media.download_message_attachments(self.session, message, self.temp.name, "photo")
        attachment = attachments[0]
        self.assertTrue(attachment["ok"])
        self.assertTrue(attachment["thumbnail"]["ok"])
        self.assertEqual(Path(attachment["thumbnail"]["path"]).read_bytes(), png)
        self.assertNotEqual(attachment["path"], attachment["thumbnail"]["path"])

    def test_pending_task_retains_quoted_media_and_thumbnail_during_cleanup(self):
        message = self.message()
        message["item_list"][0]["ref_msg"] = {"message_item": {
            "type": 2, "image_item": {"media": {"encrypt_query_param": "full"},
                                      "thumb_media": {"encrypt_query_param": "thumb"}}}}

        def download(session, ref, aeskey, path):
            runtime_io.private_directory(os.path.dirname(path))
            Path(path).write_bytes(b"test")
            os.utime(path, (0, 0))
            return True, "image/png"

        with patch.object(media, "download_attachment", side_effect=download):
            self.poll([message])
        inbox = Path(bridge.INBOX, "1_1.json")
        record = json.loads(inbox.read_text(encoding="utf-8"))
        paths = bridge.inbox_media_paths(record)
        self.assertEqual(len(paths), 2)
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(all(Path(path).exists() for path in paths))
        record["status"] = "done"
        runtime_io.atomic_write_json(str(inbox), record)
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_video_and_voice_upload_build_expected_protocol_items(self):
        for kind, ext, raw, options, upload_type, item_type in [
                ("video", ".mp4", b"\x00\x00\x00\x18ftypmp42test", {}, 2, 5),
                ("voice", ".silk", b"#!SILK_V3\n\x05\x00abcde", {"encode_type": 6, "sample_rate": 24000, "playtime": 1500}, 4, 3)]:
            with self.subTest(kind=kind):
                path = str(Path(self.temp.name, "sample" + ext))
                Path(path).write_bytes(raw)
                calls = []

                def api(base, route, body, token, timeout):
                    calls.append(body)
                    return {"upload_param": "upload"}, None

                with patch.object(media, "open_weixin_url", return_value=Response(
                        headers={"x-encrypted-param": "download"})) as remote:
                    item = media.upload_media(api, self.session, "owner", path, kind, options=options)
                self.assertEqual(calls[0]["media_type"], upload_type)
                self.assertEqual(item["type"], item_type)
                encrypted = remote.call_args.args[0].data
                key = bytes.fromhex(calls[0]["aeskey"])
                self.assertEqual(media.aes_ecb_decrypt(encrypted, key), raw)
                if kind == "video":
                    self.assertEqual(item["video_item"]["video_size"], len(encrypted))
                else:
                    self.assertEqual(item["voice_item"]["encode_type"], 6)
                    self.assertEqual(item["voice_item"]["playtime"], 1500)
                    self.assertEqual(item["voice_item"]["sample_rate"], 24000)

    def test_invalid_voice_spec_is_rejected_before_queue_or_network(self):
        base = {"kind": "voice", "path": "sample.silk", "encode_type": 6,
                "sample_rate": 24000, "playtime": 1000}
        for changes in ({"encode_type": 99}, {"encode_type": True}, {"playtime": 0},
                        {"sample_rate": None}, {"path": "sample.wav"}, {"bits_per_sample": -1},
                        {"encode_type": 1, "path": "sample.pcm"},
                        {"encode_type": 7, "path": "sample.mp3"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.queue(media_spec=dict(base, **changes))
        self.assertEqual(os.listdir(bridge.OUTBOX), [])

    def test_invalid_video_or_empty_voice_never_requests_upload(self):
        for kind, name, raw, options in [
                ("video", "fake.mp4", b"not a video", {}),
                ("voice", "empty.silk", b"", {"encode_type": 6, "sample_rate": 24000, "playtime": 1000})]:
            path = str(Path(self.temp.name, name))
            Path(path).write_bytes(raw)
            with self.subTest(kind=kind), patch.object(bridge, "http_post") as api:
                with self.assertRaises(ValueError):
                    media.upload_media(api, self.session, "owner", path, kind, options=options)
                api.assert_not_called()

    def test_mp3_renamed_to_silk_is_rejected_before_queue_and_upload(self):
        path = Path(self.temp.name, "disguised.silk")
        path.write_bytes(b"ID3test-audio")
        spec = {"kind": "voice", "path": str(path), "encode_type": 6,
                "sample_rate": 24000, "playtime": 1500}
        with self.assertRaisesRegex(ValueError, "SILK"):
            self.queue(media_spec=spec)
        with patch.object(bridge, "http_post") as api:
            with self.assertRaisesRegex(ValueError, "SILK"):
                media.upload_media(api, self.session, "owner", str(path), "voice", options=spec)
            api.assert_not_called()
        self.assertEqual(os.listdir(bridge.OUTBOX), [])

    def test_silk_header_with_and_without_tencent_prefix_is_accepted(self):
        for prefix in (b"", b"\x02"):
            path = Path(self.temp.name, "sample.silk")
            path.write_bytes(prefix + b"#!SILK_V3\n\x05\x00abcde")
            media.validate_silk_file(str(path))
        path.write_bytes(b"#!SILK_V3\n")
        with self.assertRaises(ValueError):
            media.validate_silk_file(str(path))

    def test_old_pending_cached_mp3_voice_is_not_sent(self):
        record = {"reply_to": "legacy-voice", "to_user_id": "owner", "text": "", "status": "pending",
                  "account_id": bridge.account_id(self.session), "context_token": "test-context",
                  "media": {"kind": "voice", "path": "old.mp3", "encode_type": 7,
                            "sample_rate": 24000, "playtime": 1500},
                  "delivery": [{"kind": "media", "sent": False, "client_id": "stable-id",
                                "item_list": [{"type": 3, "voice_item": {"encode_type": 7}}]}]}
        bridge._write_outbox(bridge.outbox_path("legacy-voice"), record)
        with patch.object(bridge, "http_post") as api:
            bridge.cmd_send(self.args)
            api.assert_not_called()
        self.assertIn("SILK", self.read_reply("legacy-voice")["last_error"])

    def test_mp3_voice_is_converted_once_before_upload_and_reused_after_timeout(self):
        source, encoded = Path(self.temp.name, "reply.mp3"), Path(self.temp.name, "reply.silk")
        source.write_bytes(b"ID3test")
        encoded.write_bytes(b"\x02#!SILK_V3\x03\x00abc")
        self.queue(media_spec={"kind": "voice", "path": str(source)})
        prepared = {"kind": "voice", "path": str(encoded), "source_path": str(source),
                    "encode_type": 6, "sample_rate": 24000, "playtime": 20, "bits_per_sample": 16}
        uploaded = {"type": 3, "voice_item": {"encode_type": 6, "sample_rate": 24000, "playtime": 20}}
        with patch.object(voice_codec, "prepare_mp3", return_value=prepared) as convert, \
                patch.object(media, "upload_media", return_value=uploaded) as upload, \
                patch.object(bridge, "http_post", side_effect=[
                    (None, "timeout"), ({"ret": 0}, None), ({"ret": 0}, None)]) as post:
            bridge.cmd_send(self.args)
            self.assertEqual(self.read_reply()["media"]["path"], str(encoded))
            source.unlink()
            encoded.unlink()  # Uploaded bytes and client ID are already durably cached.
            bridge.cmd_send(self.args)
            convert.assert_called_once()
            upload.assert_called_once()
            self.assertEqual(upload.call_args.args[3], str(encoded))
            self.assertEqual(upload.call_args.kwargs["options"]["playtime"], 20)
            self.assertEqual(post.call_args_list[0].args[2]["msg"]["client_id"],
                             post.call_args_list[1].args[2]["msg"]["client_id"])
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_failed_mp3_conversion_does_not_upload_or_send(self):
        source = Path(self.temp.name, "reply.mp3")
        source.write_bytes(b"ID3test")
        self.queue(media_spec={"kind": "voice", "path": str(source)})
        with patch.object(voice_codec, "prepare_mp3", side_effect=voice_codec.VoiceConversionError("decode failed")), \
                patch.object(media, "upload_media") as upload, patch.object(bridge, "http_post") as post:
            bridge.cmd_send(self.args)
            upload.assert_not_called()
            post.assert_not_called()
        self.assertEqual(self.read_reply()["status"], "pending")
        self.assertEqual(self.read_reply()["media"]["path"], str(source))
        self.assertIn("decode failed", self.read_reply()["last_error"])

    def test_transcoded_path_is_durable_even_when_upload_fails(self):
        source, encoded = Path(self.temp.name, "reply.mp3"), Path(self.temp.name, "reply.silk")
        source.write_bytes(b"ID3test")
        encoded.write_bytes(b"\x02#!SILK_V3\x03\x00abc")
        self.queue(media_spec={"kind": "voice", "path": str(source)})
        prepared = {"kind": "voice", "path": str(encoded), "encode_type": 6,
                    "sample_rate": 24000, "playtime": 20}
        with patch.object(voice_codec, "prepare_mp3", return_value=prepared) as convert, \
                patch.object(media, "upload_media", side_effect=[
                    RuntimeError("upload failed"), {"type": 3, "voice_item": {"encode_type": 6}}]) as upload, \
                patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            bridge.cmd_send(self.args)
            self.assertEqual(self.read_reply()["media"]["path"], str(encoded))
            source.unlink()
            bridge.cmd_send(self.args)
            convert.assert_called_once()
            self.assertEqual(upload.call_count, 2)
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_mp3_voice_cli_needs_no_sampling_or_duration_arguments(self):
        with patch.object(sys, "argv", ["bridge.py", "send-voice", "reply.mp3"]), \
                patch.object(bridge, "cmd_send_media", return_value=0) as send:
            self.assertEqual(bridge.main(), 0)
        args = send.call_args.args[0]
        self.assertEqual(args.kind, "voice")
        self.assertIsNone(args.sample_rate)
        self.assertIsNone(args.playtime)

    def test_video_and_voice_send_retries_reuse_upload_and_client_id(self):
        for kind, ext, item_type, options in [
                ("video", ".mp4", 5, {}),
                ("voice", ".silk", 3, {"encode_type": 6, "sample_rate": 24000, "playtime": 1000})]:
            path = str(Path(self.temp.name, "reply" + ext))
            if kind == "voice":
                Path(path).write_bytes(b"#!SILK_V3\n\x05\x00abcde")
            spec = dict(options, kind=kind, path=path)
            bridge.queue_reply(kind, "owner", "test-context", "", spec)
            uploaded = {"type": item_type}
            if kind == "voice":
                uploaded["voice_item"] = {"encode_type": 6}
            with self.subTest(kind=kind), \
                    patch.object(media, "upload_media", return_value=uploaded) as upload, \
                    patch.object(bridge, "http_post", side_effect=[(None, "timeout"), ({"ret": 0}, None)]) as post:
                bridge.cmd_send(self.args)
                bridge.cmd_send(self.args)
                upload.assert_called_once()
                self.assertEqual(upload.call_args.kwargs["options"], spec)
                first, second = [call.args[2]["msg"] for call in post.call_args_list]
                self.assertEqual(first["client_id"], second["client_id"])
                self.assertEqual(first["item_list"][0]["type"], item_type)
                self.assertEqual(self.read_reply(kind)["status"], "sent")

    def test_send_voice_command_preserves_options_in_queue(self):
        self.poll([self.message()])
        path = Path(self.temp.name, "reply.silk")
        path.write_bytes(b"#!SILK_V3\n\x05\x00abcde")
        args = SimpleNamespace(kind="voice", path=str(path), text="语音回复", encode_type=6,
                               sample_rate=24000, playtime=1000, bits_per_sample=None)
        self.assertEqual(bridge.cmd_send_media(args), 0)
        record = json.loads(next(Path(bridge.OUTBOX).glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["media"]["encode_type"], 6)
        self.assertEqual(record["text"], "语音回复")
        self.assertEqual(record["media"]["path"], str(path))

    def test_media_cli_dispatches_video_and_voice_with_explicit_parameters(self):
        for command, options, kind in [
                ("send-video", [], "video"),
                ("send-voice", ["--sample-rate", "24000",
                                "--playtime", "1500"], "voice")]:
            with self.subTest(command=command), patch.object(sys, "argv", [
                    "bridge.py", command, "sample.silk" if kind == "voice" else "sample.mp4"] + options), \
                    patch.object(bridge, "cmd_send_media", return_value=0) as send:
                self.assertEqual(bridge.main(), 0)
                args = send.call_args.args[0]
                self.assertEqual(args.kind, kind)
                if kind == "voice":
                    self.assertEqual((args.encode_type, args.sample_rate, args.playtime), (6, 24000, 1500))

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

"""Offline task ownership, attachment recovery, concurrency and health regressions."""
import json
import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge
import media
import test_bridge as baseline


class ReliabilityTests(unittest.TestCase):
    setUp = baseline.BridgeTests.setUp
    message = baseline.BridgeTests.message
    poll = baseline.BridgeTests.poll
    queue = baseline.BridgeTests.queue
    read_reply = baseline.BridgeTests.read_reply
    send_when_due = baseline.BridgeTests.send_when_due

    def read_task(self):
        return json.loads(Path(bridge.INBOX, "1_1.json").read_text(encoding="utf-8"))

    def test_bad_inbox_shapes_do_not_block_send_or_media_recovery(self):
        self.failed_image()
        self.queue()
        bad = [[], None, {"from_user_id": "owner", "attachments": [None]},
               {"from_user_id": "owner", "references": "invalid"},
               {"from_user_id": "owner", "attachments": [{"thumbnail": True}]},
               {"from_user_id": "owner", "attachments": [{"retry": {"attempts": "bad"}}]}]
        for index, value in enumerate(bad):
            Path(bridge.INBOX, "bad-%d.json" % index).write_text(json.dumps(value), encoding="utf-8")
        due = self.read_task()["attachments"][0]["retry"]["next_attempt_at"] + 1
        with patch.object(bridge.time, "time", return_value=due), \
                patch.object(media, "download_attachment", return_value=(True, "image/png")), \
                patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
            bridge.cmd_send(self.args)
        self.assertTrue(self.read_task()["attachments"][0]["ok"])
        self.assertEqual(self.read_reply()["status"], "sent")
        self.assertTrue(all(Path(bridge.INBOX, "bad-%d.json" % i).exists() for i in range(len(bad))))
        self.assertEqual(self.status()["invalid_records"]["inbox"], len(bad))

    def test_corrupt_retry_source_is_isolated_and_serve_still_sends(self):
        self.failed_image()
        self.queue()
        rec = self.read_task()
        rec["attachments"][0]["retry"]["source"]["ciphertext"] = "broken"
        # A second part should still recover in the same pass.
        second = json.loads(json.dumps(rec["attachments"][0]))
        second["retry"]["source"] = media.session_store.protect({
            "ref": {"encrypt_query_param": "dummy"}, "aeskey": None,
            "path": str(Path(bridge.ROOT, "media", "second.png"))})
        rec["attachments"].append(second)
        bridge.atomic_write_json(os.path.join(bridge.INBOX, "1_1.json"), rec)
        due = rec["attachments"][0]["retry"]["next_attempt_at"] + 1
        with patch.object(bridge.time, "time", return_value=due), \
                patch.object(bridge, "cmd_poll", return_value=0), \
                patch.object(media, "download_attachment", return_value=(True, "image/png")) as download, \
                patch.object(bridge, "http_post", return_value=({"ret": 0}, None)), \
                patch.object(bridge.time, "sleep", side_effect=KeyboardInterrupt):
            bridge.cmd_serve(SimpleNamespace(every=1))
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        download.assert_called_once()
        record = self.read_task()
        self.assertEqual(record["attachments"][0]["error"], "retry_source_unavailable")
        self.assertTrue(record["attachments"][0]["retry"]["stopped"])
        self.assertTrue(record["attachments"][1]["ok"])
        self.assertFalse(bridge.media_retry_pending(record))
        self.assertEqual(self.read_reply()["status"], "sent")
        self.assertEqual(self.status()["media"]["exhausted"], 1)

    def test_explicit_failed_task_reset_preserves_fencing_and_reply(self):
        self.poll([self.message()])
        first = bridge.claim_task("1_1", "worker")
        bridge.queue_reply("1_1", "owner", "context", "existing result", claim_token=first["claim_token"])
        bridge.cmd_mark(SimpleNamespace(key="1_1", status="failed", note="temporary failure",
                                        claim_token=first["claim_token"]))
        bridge.cmd_reset_task(SimpleNamespace(key="1_1", reason="checked external result"))
        with self.assertRaises(ValueError):
            bridge.cmd_mark(SimpleNamespace(key="1_1", status="done", note="",
                                            claim_token=first["claim_token"]))
        second = bridge.claim_task("1_1", "recovery")
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        with self.assertRaises(ValueError):
            bridge.renew_task("1_1", first["claim_token"])
        self.assertEqual(self.read_reply("1_1")["text"], "existing result")
        self.assertEqual(self.read_task()["reset"]["count"], 1)
        with self.assertRaises(ValueError):
            bridge.cmd_reset_task(SimpleNamespace(key="1_1", reason="cannot reset processing"))
        bridge.cmd_mark(SimpleNamespace(key="1_1", status="done", note="", claim_token=second["claim_token"]))
        with self.assertRaises(ValueError):
            bridge.cmd_reset_task(SimpleNamespace(key="1_1", reason="cannot reset done"))

    def test_reset_task_rejects_other_account_and_requires_reason(self):
        self.poll([self.message()])
        bridge.cmd_mark(SimpleNamespace(key="1_1", status="failed", note="legacy"))
        with self.assertRaises(ValueError):
            bridge.cmd_reset_task(SimpleNamespace(key="1_1", reason=" "))
        bridge.save_session(dict(self.session, ilink_bot_id="other"))
        with self.assertRaises(ValueError):
            bridge.cmd_reset_task(SimpleNamespace(key="1_1", reason="repair"))

    def test_quote_refresh_recovers_after_interruption_and_keeps_task_lease(self):
        first, second = self.message(), self.message(number=2)
        first["item_list"] = [{"type": 2, "msg_id": "image-id", "image_item": {
            "media": {"encrypt_query_param": "dummy"}}}]
        second["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": "image-id"}}
        with patch.object(media, "download_attachment", return_value=(False, "timeout")):
            self.poll([first, second])
        claim = bridge.claim_task("2_2", "worker")
        due = self.read_task()["attachments"][0]["retry"]["next_attempt_at"] + 1
        def download(session, ref, key, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"image")
            return True, "image/png"
        with patch.object(bridge.time, "time", return_value=due), \
                patch.object(media, "download_attachment", side_effect=download), \
                patch.object(bridge, "_refresh_reference_snapshots", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                bridge.cmd_retry_media(SimpleNamespace(limit=20))
        # No downloads are due now; the next pass must still refresh references.
        with patch.object(media, "download_attachment") as download:
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        download.assert_not_called()
        quote = json.loads(Path(bridge.INBOX, "2_2.json").read_text(encoding="utf-8"))
        self.assertTrue(quote["references"][0]["attachments"][0]["ok"])
        self.assertEqual(quote["lease"]["token"], claim["claim_token"])
        self.assertEqual(quote["status"], "processing")

    def test_claim_is_exclusive_and_expiry_fences_old_worker(self):
        self.poll([self.message()])
        first = bridge.claim_task("1_1", "worker-a", 10)
        with self.assertRaises(ValueError):
            bridge.claim_task("1_1", "worker-b")
        with patch.object(bridge.time, "time", return_value=first["expires_at"] + 1):
            second = bridge.claim_task("1_1", "worker-b")
            self.assertTrue(second["recovered"])
            with self.assertRaises(ValueError):
                bridge.queue_reply("1_1", "owner", "context", "stale",
                                   claim_token=first["claim_token"])
            with self.assertRaises(ValueError):
                bridge.cmd_mark(SimpleNamespace(key="1_1", status="done", note="",
                                                claim_token=first["claim_token"]))
            bridge.queue_reply("1_1", "owner", "context", "result",
                               claim_token=second["claim_token"])
            bridge.cmd_mark(SimpleNamespace(key="1_1", status="done", note="",
                                            claim_token=second["claim_token"]))
        self.assertEqual(self.read_task()["status"], "done")
        with self.assertRaises(ValueError):
            bridge.cmd_mark(SimpleNamespace(key="1_1", status="new", note="",
                                            claim_token=first["claim_token"]))

    def test_renewal_requires_live_token_and_extends_lease(self):
        self.poll([self.message()])
        claim = bridge.claim_task("1_1", "worker", 10)
        renewed = bridge.renew_task("1_1", claim["claim_token"], 300)
        self.assertGreater(renewed["expires_at"], claim["expires_at"])
        with self.assertRaises(ValueError):
            bridge.renew_task("1_1", "wrong-token")
        with patch.object(bridge.time, "time", return_value=renewed["expires_at"] + 1):
            with self.assertRaises(ValueError):
                bridge.renew_task("1_1", claim["claim_token"])

    def test_legacy_processing_is_not_silently_reexecuted(self):
        self.poll([self.message()])
        record = self.read_task()
        record["status"] = "processing"
        bridge.atomic_write_json(os.path.join(bridge.INBOX, "1_1.json"), record)
        with self.assertRaises(ValueError):
            bridge.claim_task("1_1", "worker")
        with self.assertRaises(ValueError):
            bridge.cmd_mark(SimpleNamespace(key="1_1", status="processing", note=""))

    def test_task_claim_and_existing_reply_are_account_scoped(self):
        self.poll([self.message()])
        self.queue()
        bridge.save_session(dict(self.session, ilink_bot_id="different-bot"))
        with self.assertRaises(ValueError):
            bridge.claim_task("1_1", "worker")
        with self.assertRaises(ValueError):
            self.queue()

    def failed_image(self):
        message = self.message()
        message["item_list"] = [{"type": 2, "msg_id": "image-item", "image_item": {
            "media": {"encrypt_query_param": "secret-download-param"},
            "thumb_media": {"encrypt_query_param": "secret-thumb-param"}}}]
        with patch.object(media, "download_attachment", side_effect=[
                (False, "network timeout"), (True, "image/png")]):
            self.poll([message])
        return message

    def test_attachment_retry_preserves_thumbnail_and_updates_item_history(self):
        self.failed_image()
        record = self.read_task()
        self.assertNotIn("secret-download-param", json.dumps(record))
        self.assertEqual(bridge.load_cursor(), "next")
        due = record["attachments"][0]["retry"]["next_attempt_at"]
        with patch.object(bridge.time, "time", return_value=due + 1), \
                patch.object(media, "download_attachment", return_value=(True, "image/png")) as download:
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        download.assert_called_once()
        record = self.read_task()
        self.assertTrue(record["attachments"][0]["ok"])
        self.assertTrue(record["attachments"][0]["thumbnail"]["ok"])
        self.assertNotIn("retry", record["attachments"][0])
        self.assertTrue(record["item_messages"][0]["attachments"][0]["ok"])

    def test_attachment_backoff_exhaustion_and_account_isolation(self):
        self.failed_image()
        with patch.object(media, "download_attachment") as download:
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        download.assert_not_called()
        due = self.read_task()["attachments"][0]["retry"]["next_attempt_at"]
        bridge.save_session(dict(self.session, ilink_bot_id="other"))
        with patch.object(bridge.time, "time", return_value=due + 1), \
                patch.object(media, "download_attachment") as download:
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        download.assert_not_called()
        bridge.save_session(self.session)
        for _ in range(4):
            due = self.read_task()["attachments"][0]["retry"]["next_attempt_at"]
            with patch.object(bridge.time, "time", return_value=due + 1), \
                    patch.object(media, "download_attachment", return_value=(False, "timeout")):
                bridge.cmd_retry_media(SimpleNamespace(limit=20))
        self.assertFalse(bridge.media_retry_pending(self.read_task()))

    def test_cleanup_keeps_completed_record_with_pending_media_retry(self):
        self.failed_image()
        record = self.read_task()
        record["status"] = "done"
        path = os.path.join(bridge.INBOX, "1_1.json")
        bridge.atomic_write_json(path, record)
        os.utime(path, (0, 0))
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(os.path.exists(path))

    def test_poll_network_wait_allows_queue_and_blocks_account_switch(self):
        def response(*args, **kwargs):
            self.queue("during-poll")
            with patch.object(bridge, "http_get") as get:
                bridge.cmd_wait_login(SimpleNamespace(timeout=1, verify_code=None))
            get.assert_not_called()
            return {"ret": 0, "msgs": [self.message()], "get_updates_buf": "next"}, None
        with patch.object(bridge, "http_post", side_effect=response):
            bridge.cmd_poll(self.args)
        self.assertEqual(self.read_reply("during-poll")["status"], "pending")
        self.assertEqual(bridge.load_cursor(), "next")

    def test_send_network_wait_allows_claim_and_queue_but_not_second_sender(self):
        self.poll([self.message()])
        self.queue()
        def response(*args, **kwargs):
            claim = bridge.claim_task("1_1", "worker")
            bridge.queue_reply("1_1", "owner", "context", "result", claim_token=claim["claim_token"])
            bridge.cmd_send(self.args)  # skips its held send lock, never recurses into HTTP
            return {"ret": 0}, None
        with patch.object(bridge, "http_post", side_effect=response) as post:
            bridge.cmd_send(self.args)
        post.assert_called_once()
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_retry_download_does_not_overwrite_concurrent_task_claim(self):
        self.failed_image()
        due = self.read_task()["attachments"][0]["retry"]["next_attempt_at"]
        tokens = []
        def download(*args):
            tokens.append(bridge.claim_task("1_1", "worker")["claim_token"])
            return True, "image/png"
        with patch.object(bridge.time, "time", return_value=due + 1), \
                patch.object(media, "download_attachment", side_effect=download):
            bridge.cmd_retry_media(SimpleNamespace(limit=20))
        self.assertEqual(self.read_task()["lease"]["token"], tokens[0])
        self.assertTrue(self.read_task()["attachments"][0]["ok"])

    def test_poll_discards_result_if_session_changes_during_network(self):
        bridge.save_cursor("old")
        def response(*args, **kwargs):
            bridge.save_session(dict(self.session, ilink_bot_id="other"))
            return {"ret": 0, "msgs": [self.message()], "get_updates_buf": "next"}, None
        with patch.object(bridge, "http_post", side_effect=response):
            self.assertEqual(bridge.cmd_poll(self.args), 1)
        self.assertEqual(bridge.load_cursor(), "old")
        self.assertEqual(os.listdir(bridge.INBOX), [])

    def test_same_page_id_only_reference_resolves_after_staging(self):
        first, second = self.message(), self.message(number=2)
        second["item_list"][0]["ref_msg"] = {"message_item": {"type": 0, "msg_id": "1"}}
        self.poll([first, second])
        record = json.loads(Path(bridge.INBOX, "2_2.json").read_text(encoding="utf-8"))
        self.assertEqual(record["references"][0]["resolution"], "local_history")

    def test_deferred_delayed_flush_is_resumed_without_another_inbound(self):
        self.queue()
        entry = self.read_reply()
        entry.update(status="delayed", attempts=3)
        bridge._write_outbox(bridge.outbox_path("reply"), entry)
        bridge.save_delayed({"owner": [entry]})
        with bridge.Locked("send.lock"):
            self.poll([self.message()])
        self.assertFalse(self.read_task()["delayed_flushed"])
        with patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            bridge.cmd_send(self.args)
        self.assertTrue(self.read_task()["delayed_flushed"])
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_transient_backoff_does_not_wait_for_user_after_three_failures(self):
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "http_503")) as post:
            bridge.cmd_send(self.args)
            first = self.read_reply()
            bridge.cmd_send(self.args)
            self.assertEqual(post.call_count, 1)
            self.assertGreater(first["next_attempt_at"], time.time())
            self.send_when_due()
            self.send_when_due()
        self.assertEqual(self.read_reply()["status"], "pending")
        self.assertEqual(self.read_reply()["attempts"], 3)
        with patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            self.send_when_due()
        self.assertEqual(self.read_reply()["status"], "sent")
        self.assertEqual(first["delivery"][0]["client_id"], self.read_reply()["delivery"][0]["client_id"])

    def test_auth_failure_blocks_until_explicit_retry_without_changing_id(self):
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "http_401")) as post:
            bridge.cmd_send(self.args)
            bridge.cmd_send(self.args)
        post.assert_called_once()
        record = self.read_reply()
        self.assertEqual(record["status"], "blocked")
        bridge.cmd_retry_send(SimpleNamespace(key="reply"))
        self.assertEqual(self.read_reply()["delivery"], record["delivery"])
        with patch.object(bridge, "http_post", return_value=({"ret": 0}, None)):
            bridge.cmd_send(self.args)
        self.assertEqual(self.read_reply()["status"], "sent")

    def test_missing_context_waits_for_new_inbound_without_network(self):
        bridge.queue_reply("reply", "owner", "", "result")
        with patch.object(bridge, "http_post") as post:
            bridge.cmd_send(self.args)
        post.assert_not_called()
        self.assertEqual(self.read_reply()["status"], "delayed")
        self.assertEqual(self.read_reply()["error_kind"], "context")
        self.poll([self.message()])
        self.assertEqual(self.read_reply()["status"], "pending")
        self.assertEqual(self.read_reply()["next_attempt_at"], 0)

    def test_unknown_business_error_is_not_guessed_as_context_expiry(self):
        self.queue()
        with patch.object(bridge, "http_post", return_value=({"ret": 12345}, None)):
            bridge.cmd_send(self.args)
            self.send_when_due()
            self.send_when_due()
        self.assertEqual(self.read_reply()["status"], "delayed")
        self.assertEqual(self.read_reply()["error_kind"], "business_unknown")

    def test_transient_exhaustion_retains_reply_for_manual_recovery(self):
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "timeout")):
            for _ in range(8):
                self.send_when_due()
        self.assertEqual(self.read_reply()["status"], "failed")
        bridge.cmd_cleanup(SimpleNamespace(days=1))
        self.assertTrue(os.path.exists(bridge.outbox_path("reply")))

    def status(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            bridge.cmd_status(self.args)
        return json.loads(output.getvalue())

    def test_failed_poll_updates_attempt_but_not_success(self):
        with patch.object(bridge, "http_post", return_value=(None, "http_503")):
            bridge.cmd_poll(self.args)
        health = self.status()["health"]["poll"]
        self.assertIn("last_attempt_at", health)
        self.assertNotIn("last_success_at", health)
        self.assertEqual(health["last_error"], "http_503")
        self.poll([])
        success = self.status()["health"]["poll"]
        self.assertIn("last_success_at", success)
        self.assertIsNone(success["last_error"])

    def test_status_is_account_scoped_and_reports_expired_and_blocked_work(self):
        self.poll([self.message()])
        claim = bridge.claim_task("1_1", "worker", 1)
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "http_401")):
            bridge.cmd_send(self.args)
        with patch.object(bridge.time, "time", return_value=claim["expires_at"] + 1):
            status = self.status()
        self.assertTrue(status["needs_login"])
        self.assertEqual(status["inbox"]["expired_leases"], 1)
        self.assertEqual(status["outbox"]["by_status"]["blocked"], 1)
        bridge.save_session(dict(self.session, ilink_bot_id="new-bot"))
        status = self.status()
        self.assertEqual(status["inbox"]["total"], 0)
        self.assertEqual(status["outbox"]["total"], 0)
        self.assertEqual(status["health"]["poll"], {})
        self.assertFalse(status["needs_login"])

    def test_empty_send_does_not_claim_delivery_success(self):
        bridge.cmd_send(self.args)
        self.assertEqual(self.status()["health"]["send"], {})

    def test_status_counts_backoff_media_and_invalid_records(self):
        self.failed_image()
        self.queue()
        with patch.object(bridge, "http_post", return_value=(None, "timeout")):
            bridge.cmd_send(self.args)
        Path(bridge.INBOX, "broken.json").write_text("{broken", encoding="utf-8")
        status = self.status()
        self.assertEqual(status["outbox"]["waiting_retry"], 1)
        self.assertEqual(status["media"]["pending_retry"], 1)
        self.assertEqual(status["invalid_records"]["inbox"], 1)

    def test_failed_inbox_commit_is_not_reported_as_poll_success(self):
        write = bridge.atomic_write_json
        def fail(path, value):
            if os.path.dirname(path) == bridge.INBOX:
                raise OSError("disk full")
            return write(path, value)
        with patch.object(bridge, "atomic_write_json", side_effect=fail):
            with self.assertRaises(OSError):
                self.poll([self.message()])
        health = self.status()["health"]["poll"]
        self.assertNotIn("last_success_at", health)
        self.assertEqual(health["last_error"], "OSError")

    def test_cross_process_claim_has_only_one_winner(self):
        self.poll([self.message()])
        script = ("import bridge, json; "
                  "bridge.ROOT = %r; bridge.STATE = %r; bridge.INBOX = %r; bridge.OUTBOX = %r; "
                  "print(json.dumps(bridge.claim_task('1_1', 'child')))" %
                  (bridge.ROOT, bridge.STATE, bridge.INBOX, bridge.OUTBOX))
        children = [subprocess.Popen([sys.executable, "-B", "-c", script],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    for _ in range(2)]
        try:
            outputs = [child.communicate(timeout=20) for child in children]
            self.assertEqual(sum(child.returncode == 0 for child in children), 1, outputs)
            winner = next(json.loads(output[0]) for child, output in zip(children, outputs)
                          if child.returncode == 0)
            self.assertEqual(self.read_task()["lease"]["token"], winner["claim_token"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_interrupted_context_delay_move_keeps_fresh_context_recovery(self):
        bridge.queue_reply("reply", "owner", "", "result")
        write = bridge._write_outbox
        def interrupted(path, record):
            if record.get("status") == "delayed":
                raise OSError("interrupted move")
            write(path, record)
        with patch.object(bridge, "_write_outbox", side_effect=interrupted):
            with self.assertRaises(OSError):
                bridge.cmd_send(self.args)
        self.assertEqual(self.read_reply()["status"], "pending")
        self.poll([self.message()])
        self.assertEqual(self.read_reply()["context_token"], "test-context")
        self.assertEqual(self.read_reply()["next_attempt_at"], 0)

    def test_successful_relogin_clears_auth_diagnostic_without_fake_send_success(self):
        bridge.record_health(self.session, "send", error="http_401")
        bridge.atomic_write_json(os.path.join(bridge.STATE, "qr.json"), {"qrcode": "test-qr"})
        response = dict(self.session, status="confirmed")
        with patch.object(bridge, "http_get", return_value=(response, None)):
            bridge.cmd_wait_login(SimpleNamespace(timeout=1, verify_code=None))
        status = self.status()
        self.assertFalse(status["needs_login"])
        self.assertNotIn("last_success_at", status["health"]["send"])

"""Real cryptographic round trips with synthetic credentials only."""
import base64
import copy
import json
import os
import unittest
from unittest.mock import patch

import session_store


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.session = {"bot_token": "synthetic-secret", "ilink_user_id": "测试用户"}
        self.env = patch.dict(os.environ, {
            session_store.KEY_ENV: base64.b64encode(os.urandom(32)).decode("ascii")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.scheme = patch.object(session_store, "_default_scheme", return_value="aes-256-gcm")
        self.scheme.start()
        self.addCleanup(self.scheme.stop)

    def test_aes_round_trip_and_randomized_ciphertext(self):
        first = session_store.protect(self.session)
        second = session_store.protect(self.session)
        self.assertEqual(session_store.unprotect(first), self.session)
        self.assertNotEqual(first["ciphertext"], second["ciphertext"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotIn(self.session["bot_token"], json.dumps(first))
        self.assertNotIn(os.environ[session_store.KEY_ENV], json.dumps(first))

    def test_wrong_key_is_rejected(self):
        envelope = session_store.protect(self.session)
        with patch.dict(os.environ, {session_store.KEY_ENV: base64.b64encode(os.urandom(32)).decode("ascii")}):
            with self.assertRaises(session_store.SessionEncryptionError):
                session_store.unprotect(envelope)

    def test_missing_or_invalid_key_never_downgrades_to_plaintext(self):
        envelope = session_store.protect(self.session)
        for value in ("", "invalid", base64.b64encode(b"short").decode("ascii")):
            with self.subTest(value=value), patch.dict(os.environ, {session_store.KEY_ENV: value}):
                with self.assertRaises(session_store.SessionEncryptionError):
                    session_store.protect(self.session)
                with self.assertRaises(session_store.SessionEncryptionError):
                    session_store.unprotect(envelope)

    def test_tampered_ciphertext_nonce_and_tag_are_rejected(self):
        envelope = session_store.protect(self.session)
        for field in ("ciphertext", "nonce", "tag"):
            damaged = copy.deepcopy(envelope)
            data = bytearray(base64.b64decode(damaged[field]))
            data[0] ^= 1
            damaged[field] = base64.b64encode(data).decode("ascii")
            with self.subTest(field=field), self.assertRaises(session_store.SessionEncryptionError):
                session_store.unprotect(damaged)

    def test_invalid_envelope_is_rejected(self):
        envelope = session_store.protect(self.session)
        for changes in ({"version": 99}, {"scheme": "plaintext"}, {"format": "unknown"},
                        {"ciphertext": "!"}, {"nonce": ""}, {"tag": None}):
            with self.subTest(changes=changes), self.assertRaises(session_store.SessionEncryptionError):
                session_store.unprotect(dict(envelope, **changes))

    def test_atomic_writer_only_receives_encrypted_envelope(self):
        with patch.object(session_store, "atomic_write_json") as write:
            session_store.save("unused", self.session)
        written = write.call_args.args[1]
        self.assertNotIn(self.session["bot_token"], json.dumps(written))
        self.assertEqual(session_store.unprotect(written), self.session)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_real_dpapi_round_trip_and_tamper_rejection(self):
        with patch.object(session_store, "_default_scheme", return_value="windows-dpapi"):
            envelope = session_store.protect(self.session)
        self.assertEqual(session_store.unprotect(envelope), self.session)
        damaged = bytearray(base64.b64decode(envelope["ciphertext"]))
        damaged[-1] ^= 1
        envelope["ciphertext"] = base64.b64encode(damaged).decode("ascii")
        with self.assertRaises(session_store.SessionEncryptionError):
            session_store.unprotect(envelope)


if __name__ == "__main__":
    unittest.main()

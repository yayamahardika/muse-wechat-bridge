"""Offline conversion checks, including real MP3 decoding and SILK encode/decode."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

import pysilk
import voice_codec


class VoiceCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-codec-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"

    def test_real_mp3_to_silk_decodes_and_reuses_verified_cache(self):
        source = self.root / "语音 sample.mp3"
        created = subprocess.run([
            voice_codec.ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.23",
            "-ac", "2", "-c:a", "libmp3lame", str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            **voice_codec._process_options())
        self.assertEqual(created.returncode, 0, created.stderr.decode("utf-8", errors="replace"))
        original = source.read_bytes()
        result = voice_codec.prepare_mp3(str(source), str(self.cache))
        silk = Path(result["path"]).read_bytes()
        self.assertTrue(silk.startswith(b"\x02#!SILK_V3"))
        self.assertEqual(result["encode_type"], 6)
        self.assertEqual(result["sample_rate"], 24000)
        self.assertEqual(result["playtime"], 240)  # Last partial 20 ms frame is preserved.
        decoded = io.BytesIO()
        pysilk.decode(io.BytesIO(silk), decoded, 24000)
        self.assertEqual(len(decoded.getvalue()), 24000 * 2 * result["playtime"] // 1000)
        self.assertNotEqual(decoded.getvalue(), b"\0" * len(decoded.getvalue()))
        self.assertEqual(source.read_bytes(), original)
        with patch.object(voice_codec.subprocess, "run", side_effect=AssertionError("cache should avoid conversion")):
            self.assertEqual(voice_codec.prepare_mp3(str(source), str(self.cache)), result)
        # The cache must detect changed bytes rather than trusting just a filename/signature.
        Path(result["path"]).write_bytes(silk[:-1] + bytes([silk[-1] ^ 1]))
        with patch.object(voice_codec.subprocess, "run", wraps=subprocess.run) as worker:
            rebuilt = voice_codec.prepare_mp3(str(source), str(self.cache))
            worker.assert_called_once()
        self.assertEqual(rebuilt["playtime"], result["playtime"])
        self.assertEqual(list(self.cache.glob(".voice-*")), [])
        # Production delivery decodes existing SILK to a standard WAV FILE card.
        wav_cache = self.root / "audio-cache"
        audio_file = voice_codec.prepare_audio_file(result["path"], str(wav_cache))
        self.assertEqual(audio_file["kind"], "file")
        self.assertTrue(audio_file["path"].endswith(".wav"))
        with wave.open(audio_file["path"], "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
            self.assertEqual(wav.getnframes(), 24000 * result["playtime"] // 1000)
            self.assertNotEqual(wav.readframes(wav.getnframes()), b"\0" * (wav.getnframes() * 2))
        with patch.object(voice_codec.subprocess, "run", side_effect=AssertionError("WAV cache should avoid decoding")):
            self.assertEqual(voice_codec.prepare_audio_file(result["path"], str(wav_cache)), audio_file)

    def test_audio_files_pass_through_without_codec_and_old_silk_prefers_original_mp3(self):
        for extension in (".mp3", ".wav"):
            source = self.root / ("reply" + extension)
            source.write_bytes(b"file content")
            with patch.object(voice_codec.subprocess, "run", side_effect=AssertionError("no conversion needed")):
                result = voice_codec.prepare_audio_file(str(source), str(self.cache))
            self.assertEqual(result["kind"], "file")
            self.assertEqual(result["path"], str(source))
        original = self.root / "reply.mp3"
        recovered = voice_codec.prepare_audio_file(str(self.root / "old.silk"), str(self.cache), str(original))
        self.assertEqual(recovered["path"], str(original))
        self.assertFalse(self.cache.exists())

    def test_invalid_mp3_fails_without_publishing_silk(self):
        source = self.root / "invalid.mp3"
        source.write_bytes(b"ID3not valid MP3 audio")
        with self.assertRaises(voice_codec.VoiceConversionError):
            voice_codec.prepare_mp3(str(source), str(self.cache))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_conversion_timeout_leaves_no_partial_cache(self):
        source = self.root / "source.mp3"
        source.write_bytes(b"ID3test")
        with patch.object(voice_codec.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 30)):
            with self.assertRaisesRegex(voice_codec.VoiceConversionError, "timed out"):
                voice_codec.prepare_mp3(str(source), str(self.cache))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_worker_cannot_publish_inconsistent_output(self):
        source = self.root / "source.mp3"
        source.write_bytes(b"ID3test")

        def worker(command, **kwargs):
            Path(command[-1]).write_bytes(b"\x02#!SILK_V3\x03\x00abc")
            return subprocess.CompletedProcess(command, 0,
                stdout=json.dumps({"playtime": 200, "silk_sha256": "incorrect"}).encode("utf-8"), stderr=b"")

        with patch.object(voice_codec.subprocess, "run", side_effect=worker):
            with self.assertRaises(voice_codec.VoiceConversionError):
                voice_codec.prepare_mp3(str(source), str(self.cache))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_long_decoded_audio_is_rejected_instead_of_truncated(self):
        source = self.root / "source.mp3"
        source.write_bytes(b"ID3test")
        decoded = subprocess.CompletedProcess([], 0, stdout=b"\0" * (24000 * 2 * 60 + 2), stderr=b"")
        with patch.object(voice_codec.subprocess, "run", return_value=decoded), \
                patch.object(pysilk, "encode") as encoder:
            with self.assertRaisesRegex(voice_codec.VoiceConversionError, "60-second"):
                voice_codec._encode_mp3(str(source), str(self.root / "output.silk"))
            encoder.assert_not_called()

    def test_empty_or_oversized_source_never_starts_worker(self):
        source = self.root / "source.mp3"
        for data in (b"", b"123456789"):
            source.write_bytes(data)
            with patch.object(voice_codec, "MAX_SOURCE_BYTES", 8), \
                    patch.object(voice_codec.subprocess, "run") as worker:
                with self.assertRaises(voice_codec.VoiceConversionError):
                    voice_codec.prepare_mp3(str(source), str(self.cache))
                worker.assert_not_called()

    def test_silk_packet_validation_rejects_truncated_or_empty_output(self):
        for data in (b"", b"#!SILK_V3", b"\x02#!SILK_V3", b"\x02#!SILK_V3\x03",
                     b"\x02#!SILK_V3\x03\x00ab", b"\x02#!SILK_V3\xff\xff"):
            with self.subTest(data=data), self.assertRaises(voice_codec.VoiceConversionError):
                voice_codec.silk_duration(data)


if __name__ == "__main__":
    unittest.main()

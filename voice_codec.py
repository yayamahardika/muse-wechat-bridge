"""Audio file preparation. SILK encoding is retained locally; bot replies use files."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

from runtime_io import atomic_write_json, private_directory, private_file

SAMPLE_RATE = 24000
BIT_RATE = 24000
FRAME_BYTES = SAMPLE_RATE // 50 * 2  # mono s16le, 20 ms per encoded packet
MAX_SECONDS = 60  # Local conversion limit; longer audio is rejected, never truncated silently.
MAX_SOURCE_BYTES = 25 * 1024 * 1024
WORKER_TIMEOUT = 30
CACHE_VERSION = 1
TENCENT_HEADER = b"\x02#!SILK_V3"


class VoiceConversionError(ValueError):
    pass


def _process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def ffmpeg_executable():
    override = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if override:
        return override
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise VoiceConversionError(
            "FFmpeg unavailable; install requirements.txt or set IMAGEIO_FFMPEG_EXE") from None


def silk_duration(data):
    """Validate complete Tencent packets and report duration for our 20 ms encoder."""
    if not data.startswith(TENCENT_HEADER):
        raise VoiceConversionError("SILK encoder did not produce Tencent SILK V3")
    offset, packets = len(TENCENT_HEADER), 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise VoiceConversionError("SILK output has a truncated packet header")
        size = int.from_bytes(data[offset:offset + 2], "little", signed=True)
        offset += 2
        if size <= 0 or offset + size > len(data):
            raise VoiceConversionError("SILK output has an invalid or truncated packet")
        offset += size
        packets += 1
    if not packets or packets * 20 > MAX_SECONDS * 1000:
        raise VoiceConversionError("Voice must be nonempty and no longer than %d seconds" % MAX_SECONDS)
    return packets * 20


def _encode_mp3(source, output):
    try:
        import pysilk  # Distribution name: silk-python, not the unrelated pysilk PyPI package.
        if not callable(getattr(pysilk, "encode", None)):
            raise ImportError("missing encoder")
    except ImportError:
        raise VoiceConversionError("SILK encoder unavailable; install silk-python==0.2.8") from None
    command = [ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", source,
               "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-t", str(MAX_SECONDS + 0.02), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
    try:
        decoded = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=20, check=False, **_process_options())
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceConversionError("MP3 decoding failed or timed out; check FFmpeg installation") from None
    pcm = decoded.stdout
    if decoded.returncode or not pcm or len(pcm) % 2:
        raise VoiceConversionError("Invalid or empty MP3 audio; decoding failed")
    if len(pcm) > SAMPLE_RATE * 2 * MAX_SECONDS:
        raise VoiceConversionError("MP3 exceeds the local %d-second voice limit; split it first" % MAX_SECONDS)
    # The encoder consumes complete 20 ms frames; retain the last partial frame with zero padding.
    pcm += b"\0" * (-len(pcm) % FRAME_BYTES)
    encoded = io.BytesIO()
    try:
        pysilk.encode(io.BytesIO(pcm), encoded, SAMPLE_RATE, BIT_RATE, tencent=True)
    except Exception:
        raise VoiceConversionError("SILK encoding failed") from None
    data = encoded.getvalue()
    playtime = silk_duration(data)
    if playtime != len(pcm) // FRAME_BYTES * 20:
        raise VoiceConversionError("SILK output duration does not match decoded audio")
    with open(output, "wb") as stream:
        private_file(output)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"playtime": playtime, "sample_rate": SAMPLE_RATE, "bits_per_sample": 16,
            "encode_type": 6, "silk_sha256": hashlib.sha256(data).hexdigest(), "version": CACHE_VERSION}


def _cached_result(path, metadata_path):
    try:
        with open(metadata_path, encoding="utf-8") as stream:
            metadata = json.load(stream)
        with open(path, "rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        playtime = silk_duration(data)
        if (len(data) <= MAX_SOURCE_BYTES and metadata.get("version") == CACHE_VERSION
                and metadata.get("encode_type") == 6 and metadata.get("sample_rate") == SAMPLE_RATE
                and metadata.get("bits_per_sample") == 16 and metadata.get("playtime") == playtime
                and metadata.get("silk_sha256") == hashlib.sha256(data).hexdigest()):
            private_file(path)
            private_file(metadata_path)
            return metadata
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def prepare_mp3(source, cache_directory):
    """Publish only verified SILK. The original MP3 is preserved and never uploaded as voice."""
    source = os.path.abspath(source)
    if not os.path.isfile(source):
        raise VoiceConversionError("MP3 source file does not exist")
    with open(source, "rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise VoiceConversionError("MP3 source must be nonempty and no larger than 25 MiB")
    digest = hashlib.sha256(b"wechat-voice:24000:24000:mono:v1\0" + raw).hexdigest()
    private_directory(cache_directory)
    path = os.path.abspath(os.path.join(cache_directory, digest + ".silk"))
    metadata_path = path + ".json"
    metadata = _cached_result(path, metadata_path)
    if metadata is None:
        # A private snapshot prevents the source changing between hashing and decoding.
        temporary = tempfile.mkdtemp(prefix=".voice-", dir=os.path.abspath(cache_directory))
        private_directory(temporary)
        snapshot, encoded = os.path.join(temporary, "input.mp3"), os.path.join(temporary, "output.silk")
        try:
            with open(snapshot, "wb") as stream:
                private_file(snapshot)
                stream.write(raw)
            try:
                result = subprocess.run(
                    [sys.executable, "-B", os.path.abspath(__file__), "--worker", snapshot, encoded],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=WORKER_TIMEOUT,
                    check=False, **_process_options())
            except subprocess.TimeoutExpired:
                raise VoiceConversionError("MP3 to SILK conversion timed out") from None
            try:
                metadata = json.loads(result.stdout.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise VoiceConversionError("MP3 to SILK conversion worker failed") from None
            if not isinstance(metadata, dict):
                raise VoiceConversionError("MP3 to SILK conversion worker returned invalid metadata")
            if result.returncode or "error" in metadata:
                raise VoiceConversionError(metadata.get("error") or "MP3 to SILK conversion failed")
            # Verify both the encoder's output and its metadata before publishing the cache.
            with open(encoded, "rb") as stream:
                data = stream.read(MAX_SOURCE_BYTES + 1)
            if silk_duration(data) != metadata.get("playtime") or hashlib.sha256(data).hexdigest() != metadata.get("silk_sha256"):
                raise VoiceConversionError("MP3 to SILK conversion output verification failed")
            os.replace(encoded, path)
            atomic_write_json(metadata_path, metadata)
        finally:
            for filename in (snapshot, encoded):
                if os.path.isfile(filename):
                    os.remove(filename)
            os.rmdir(temporary)
    return {"kind": "voice", "path": path, "source_path": source, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "encode_type": 6, "sample_rate": SAMPLE_RATE, "bits_per_sample": 16,
            "playtime": metadata["playtime"]}


def _wav_info(path):
    with wave.open(path, "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
            raise VoiceConversionError("Invalid decoded WAV format")
        frames = audio.getnframes()
        if not 0 < frames <= SAMPLE_RATE * MAX_SECONDS:
            raise VoiceConversionError("Decoded audio is empty or exceeds the local 60-second limit")
        if len(audio.readframes(frames)) != frames * 2:
            raise VoiceConversionError("Decoded WAV is truncated")
    return {"playtime": (frames * 1000 + SAMPLE_RATE - 1) // SAMPLE_RATE,
            "sample_rate": SAMPLE_RATE, "bits_per_sample": 16}


def _decode_silk(source, output):
    try:
        import pysilk
        if not callable(getattr(pysilk, "decode", None)):
            raise ImportError("missing decoder")
    except ImportError:
        raise VoiceConversionError("SILK decoder unavailable; install silk-python==0.2.8") from None

    class LimitedPCM(io.BytesIO):
        def write(self, data):
            if self.tell() + len(data) > SAMPLE_RATE * 2 * MAX_SECONDS:
                raise VoiceConversionError("SILK exceeds the local 60-second conversion limit")
            return super().write(data)

    pcm = LimitedPCM()
    try:
        with open(source, "rb") as stream:
            pysilk.decode(stream, pcm, SAMPLE_RATE)
    except Exception:
        raise VoiceConversionError("SILK decoding failed or exceeds the local 60-second limit") from None
    if not pcm.getvalue():
        raise VoiceConversionError("SILK decoding produced empty audio")
    with open(output, "wb") as stream:
        private_file(output)
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            audio.writeframes(pcm.getvalue())
        stream.flush()
        os.fsync(stream.fileno())
    return _wav_info(output)


def prepare_audio_file(source, cache_directory, original_source=None):
    """Return FILE media only: MP3/WAV pass through, old SILK is decoded to WAV."""
    if original_source and os.path.splitext(original_source)[1].lower() == ".mp3" and os.path.isfile(original_source):
        source = original_source  # Prefer the original TTS MP3 over an older converted SILK.
    source = os.path.abspath(source)
    extension = os.path.splitext(source)[1].lower()
    if extension not in (".mp3", ".wav", ".silk", ".sil"):
        raise VoiceConversionError("Audio replies require MP3, WAV or SILK input")
    if not os.path.isfile(source) or not 0 < os.path.getsize(source) <= MAX_SOURCE_BYTES:
        raise VoiceConversionError("Audio source must exist, be nonempty and no larger than 25 MiB")
    if extension in (".mp3", ".wav"):
        return {"kind": "file", "path": source, "audio_reply": True}
    with open(source, "rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise VoiceConversionError("SILK source exceeds 25 MiB")
    digest = hashlib.sha256(b"wechat-audio-file:24000:mono:wav:v1\0" + raw).hexdigest()
    private_directory(cache_directory)
    path = os.path.abspath(os.path.join(cache_directory, digest + ".wav"))
    metadata_path = path + ".json"
    try:
        with open(metadata_path, encoding="utf-8") as stream:
            metadata = json.load(stream)
        with open(path, "rb") as stream:
            checksum = hashlib.sha256(stream.read(MAX_SOURCE_BYTES + 1)).hexdigest()
        if metadata.get("wav_sha256") != checksum or metadata.get("version") != CACHE_VERSION:
            raise VoiceConversionError("Invalid WAV cache")
        _wav_info(path)
    except (OSError, ValueError, EOFError, wave.Error, AttributeError):
        temporary = tempfile.mkdtemp(prefix=".audio-", dir=os.path.abspath(cache_directory))
        private_directory(temporary)
        snapshot, decoded = os.path.join(temporary, "input.silk"), os.path.join(temporary, "output.wav")
        try:
            with open(snapshot, "wb") as stream:
                private_file(snapshot)
                stream.write(raw)
            try:
                result = subprocess.run(
                    [sys.executable, "-B", os.path.abspath(__file__), "--decode-worker", snapshot, decoded],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=WORKER_TIMEOUT,
                    check=False, **_process_options())
            except subprocess.TimeoutExpired:
                raise VoiceConversionError("SILK to WAV conversion timed out") from None
            try:
                metadata = json.loads(result.stdout.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise VoiceConversionError("SILK to WAV conversion worker failed") from None
            if not isinstance(metadata, dict) or result.returncode:
                raise VoiceConversionError(metadata.get("error", "SILK to WAV conversion failed")
                                           if isinstance(metadata, dict) else "Invalid conversion result")
            info = _wav_info(decoded)
            with open(decoded, "rb") as stream:
                checksum = hashlib.sha256(stream.read(MAX_SOURCE_BYTES + 1)).hexdigest()
            os.replace(decoded, path)
            atomic_write_json(metadata_path, dict(info, wav_sha256=checksum, version=CACHE_VERSION))
        finally:
            for filename in (snapshot, decoded):
                if os.path.isfile(filename):
                    os.remove(filename)
            os.rmdir(temporary)
    private_file(path)
    return {"kind": "file", "path": path, "source_path": source, "audio_reply": True}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("--worker", "--decode-worker"):
        sys.exit("Internal conversion worker; use bridge.py send-voice")
    try:
        converter = _decode_silk if sys.argv[1] == "--decode-worker" else _encode_mp3
        print(json.dumps(converter(sys.argv[2], sys.argv[3])))
    except VoiceConversionError as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)

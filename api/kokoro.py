"""Kokoro TTS integration for WebUI response audio."""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

KOKORO_VENV_PYTHON = os.getenv("EBURON_KOKORO_PYTHON", "/Users/eburon/Eburon-Vep/.venv/bin/python")

_LANG_CODE_MAP = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese",
    "z": "Mandarin Chinese",
}

ENGLISH_VOICES = [
    {"id": "af_alloy", "name": "Alloy (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_aoede", "name": "Aoede (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_bella", "name": "Bella (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_heart", "name": "Heart (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_jessica", "name": "Jessica (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_kore", "name": "Kore (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_nicole", "name": "Nicole (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_nova", "name": "Nova (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_river", "name": "River (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_sarah", "name": "Sarah (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "af_sky", "name": "Sky (US Female)", "gender": "female", "lang_code": "a", "lang": "American English"},
    {"id": "am_adam", "name": "Adam (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_echo", "name": "Echo (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_eric", "name": "Eric (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_fenrir", "name": "Fenrir (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_liam", "name": "Liam (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_michael", "name": "Michael (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_onyx", "name": "Onyx (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_puck", "name": "Puck (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "am_santa", "name": "Santa (US Male)", "gender": "male", "lang_code": "a", "lang": "American English"},
    {"id": "bf_alice", "name": "Alice (UK Female)", "gender": "female", "lang_code": "b", "lang": "British English"},
    {"id": "bf_emma", "name": "Emma (UK Female)", "gender": "female", "lang_code": "b", "lang": "British English"},
    {"id": "bf_isabella", "name": "Isabella (UK Female)", "gender": "female", "lang_code": "b", "lang": "British English"},
    {"id": "bf_lily", "name": "Lily (UK Female)", "gender": "female", "lang_code": "b", "lang": "British English"},
    {"id": "bm_daniel", "name": "Daniel (UK Male)", "gender": "male", "lang_code": "b", "lang": "British English"},
    {"id": "bm_fable", "name": "Fable (UK Male)", "gender": "male", "lang_code": "b", "lang": "British English"},
    {"id": "bm_george", "name": "George (UK Male)", "gender": "male", "lang_code": "b", "lang": "British English"},
    {"id": "bm_lewis", "name": "Lewis (UK Male)", "gender": "male", "lang_code": "b", "lang": "British English"},
]
OTHER_VOICES = [
    {"id": "ef_dora", "name": "Dora (Spanish Female)", "gender": "female", "lang_code": "e", "lang": "Spanish"},
    {"id": "em_alex", "name": "Alex (Spanish Male)", "gender": "male", "lang_code": "e", "lang": "Spanish"},
    {"id": "em_santa", "name": "Santa (Spanish Male)", "gender": "male", "lang_code": "e", "lang": "Spanish"},
    {"id": "ff_siwis", "name": "Siwis (French Female)", "gender": "female", "lang_code": "f", "lang": "French"},
    {"id": "hf_alpha", "name": "Alpha (Hindi Female)", "gender": "female", "lang_code": "h", "lang": "Hindi"},
    {"id": "hf_beta", "name": "Beta (Hindi Female)", "gender": "female", "lang_code": "h", "lang": "Hindi"},
    {"id": "hm_omega", "name": "Omega (Hindi Male)", "gender": "male", "lang_code": "h", "lang": "Hindi"},
    {"id": "hm_psi", "name": "Psi (Hindi Male)", "gender": "male", "lang_code": "h", "lang": "Hindi"},
    {"id": "if_sara", "name": "Sara (Italian Female)", "gender": "female", "lang_code": "i", "lang": "Italian"},
    {"id": "im_nicola", "name": "Nicola (Italian Male)", "gender": "male", "lang_code": "i", "lang": "Italian"},
    {"id": "jf_alpha", "name": "Alpha (Japanese Female)", "gender": "female", "lang_code": "j", "lang": "Japanese"},
    {"id": "jf_gongitsune", "name": "Gongitsune (Japanese Female)", "gender": "female", "lang_code": "j", "lang": "Japanese"},
    {"id": "jf_nezumi", "name": "Nezumi (Japanese Female)", "gender": "female", "lang_code": "j", "lang": "Japanese"},
    {"id": "jf_tebukuro", "name": "Tebukuro (Japanese Female)", "gender": "female", "lang_code": "j", "lang": "Japanese"},
    {"id": "jm_kumo", "name": "Kumo (Japanese Male)", "gender": "male", "lang_code": "j", "lang": "Japanese"},
    {"id": "pf_dora", "name": "Dora (Portuguese Female)", "gender": "female", "lang_code": "p", "lang": "Portuguese"},
    {"id": "pm_alex", "name": "Alex (Portuguese Male)", "gender": "male", "lang_code": "p", "lang": "Portuguese"},
    {"id": "pm_santa", "name": "Santa (Portuguese Male)", "gender": "male", "lang_code": "p", "lang": "Portuguese"},
    {"id": "zf_xiaobei", "name": "Xiaobei (Chinese Female)", "gender": "female", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zf_xiaoni", "name": "Xiaoni (Chinese Female)", "gender": "female", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zf_xiaoxiao", "name": "Xiaoxiao (Chinese Female)", "gender": "female", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zf_xiaoyi", "name": "Xiaoyi (Chinese Female)", "gender": "female", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zm_yunjian", "name": "Yunjian (Chinese Male)", "gender": "male", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zm_yunxi", "name": "Yunxi (Chinese Male)", "gender": "male", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zm_yunxia", "name": "Yunxia (Chinese Male)", "gender": "male", "lang_code": "z", "lang": "Mandarin Chinese"},
    {"id": "zm_yunyang", "name": "Yunyang (Chinese Male)", "gender": "male", "lang_code": "z", "lang": "Mandarin Chinese"},
]

ALL_VOICES = ENGLISH_VOICES + OTHER_VOICES

_QUEUE_LOCK = threading.Lock()
_QUEUE: queue.Queue[tuple[int, str] | None] = queue.Queue(maxsize=10)


class KokoroTTSError(RuntimeError):
    """Raised when Kokoro TTS synthesis fails."""


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def kokoro_enabled() -> bool:
    if not _env_flag("EBURON_KOKORO_ENABLED", True):
        return False
    engine = os.getenv("EBURON_TTS_ENGINE") or os.getenv("TTS_ENGINE") or ""
    return str(engine).strip().lower() in {"kokoro", "kokoro_tts", ""}


def kokoro_check_available() -> bool:
    python = Path(KOKORO_VENV_PYTHON)
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-c", "from kokoro import KPipeline; print('ok')"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def kokoro_voices() -> list[dict]:
    return ALL_VOICES


def _default_voice() -> str:
    for key in ("EBURON_KOKORO_VOICE", "KOKORO_VOICE"):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return "af_heart"


def _resolve_lang_code(voice_id: str) -> str:
    for v in ALL_VOICES:
        if v["id"] == voice_id:
            return v["lang_code"]
    return "a"


def synthesize_wav(
    text: object,
    *,
    voice: str = "",
    speed: float = 1.0,
    language: str = "",
    timeout: float | None = None,
) -> tuple[bytes, dict]:
    clean = sanitize_tts_text(text)
    if not clean:
        raise KokoroTTSError("No speakable text supplied")

    python = Path(KOKORO_VENV_PYTHON)
    if not python.is_file():
        raise KokoroTTSError(f"Kokoro venv Python not found at {KOKORO_VENV_PYTHON}")

    voice_id = (voice or _default_voice()).strip()
    lang_code = language.strip() if language else _resolve_lang_code(voice_id)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            str(python),
            "-m",
            "kokoro",
            "-t",
            clean,
            "-m",
            voice_id,
            "-l",
            lang_code,
            "-s",
            str(speed),
            "-o",
            tmp_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=float(timeout or 120),
        )
        if result.returncode != 0:
            raise KokoroTTSError(
                f"Kokoro synthesis failed (exit {result.returncode}): {result.stderr.decode('utf-8', errors='replace')[:500]}"
            )

        with open(tmp_path, "rb") as f:
            wav_data = f.read()

        if not wav_data:
            raise KokoroTTSError("Kokoro returned an empty WAV file")

        return wav_data, {"voice_id": voice_id, "lang_code": lang_code, "speed": speed, "engine": "kokoro"}

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def status_payload() -> dict:
    available = kokoro_check_available()
    payload = {
        "enabled": kokoro_enabled(),
        "engine": "kokoro",
        "available": available,
        "default_voice": _default_voice(),
        "voices": kokoro_voices(),
    }
    if not payload["enabled"]:
        return payload
    if not available:
        payload["error"] = "Kokoro venv not found or import failed"
    return payload


def sanitize_tts_text(text: object) -> str:
    value = str(text or "")
    value = re.sub(r"(^|\n)[ ]{0,3}```(?:[\s\S]*?\n)?[ ]{0,3}```(?=\n|$)", " ", value)
    value = re.sub(r"`[^`]+`", " ", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"MEDIA:[^\s]+", "a file", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[*_#>~]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


class RealtimeKokoroBridge:
    """Turns streamed assistant text into ordered Kokoro TTS SSE audio events."""

    def __init__(
        self,
        put_event: Callable[[str, dict], None],
        *,
        session_id: str,
        stream_id: str,
        options: "KokoroOptions | LuxTTSOptions | None",
        is_cancelled: Callable[[], bool],
    ) -> None:
        self.put_event = put_event
        self.session_id = session_id
        self.stream_id = stream_id
        # Accept either KokoroOptions or LuxTTSOptions (for compatibility)
        if options is None:
            options = KokoroOptions()
        self.options = options
        self.is_cancelled = is_cancelled
        self.enabled = bool(self.options.enabled and self.options.realtime and kokoro_enabled())
        self.buffer = ""
        self.seq = 0
        self.had_audio = False
        self._closed = False
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[tuple[int, str] | None] = queue.Queue(
            maxsize=int(os.getenv("EBURON_KOKORO_QUEUE_SIZE", "10"))
        )

    def feed(self, text: str) -> None:
        if not self.enabled or self._closed or self.is_cancelled():
            return
        self.buffer += str(text or "")
        for segment in self._drain_ready_segments(final=False):
            self._submit(segment)

    def finish(self) -> None:
        if not self.enabled or self._closed:
            return
        for segment in self._drain_ready_segments(final=True):
            self._submit(segment)
        self._closed = True
        self._ensure_worker()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self.put_event(
                "tts_error",
                {
                    "session_id": self.session_id,
                    "stream_id": self.stream_id,
                    "message": "Kokoro queue was full at stream end",
                },
            )
            return
        if self._worker is not None:
            self._worker.join(timeout=float(os.getenv("EBURON_KOKORO_FINISH_TIMEOUT_SECONDS", "180")))
            if self._worker.is_alive():
                self.put_event(
                    "tts_error",
                    {
                        "session_id": self.session_id,
                        "stream_id": self.stream_id,
                        "message": "Kokoro did not finish before the stream timeout",
                    },
                )

    def stop(self) -> None:
        self._closed = True
        self.buffer = ""

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name=f"kokoro-{self.stream_id[:8]}", daemon=True)
        self._worker.start()

    def _submit(self, raw_segment: str) -> None:
        segment = sanitize_tts_text(raw_segment)
        if len(segment) < 2:
            return
        self._ensure_worker()
        self.seq += 1
        try:
            self._queue.put_nowait((self.seq, segment))
        except queue.Full:
            self.put_event(
                "tts_error",
                {
                    "session_id": self.session_id,
                    "stream_id": self.stream_id,
                    "message": "Kokoro queue full; skipped one response audio segment",
                },
            )

    def _drain_ready_segments(self, *, final: bool) -> list[str]:
        segments: list[str] = []
        min_chars = int(os.getenv("EBURON_KOKORO_MIN_SEGMENT_CHARS", "90"))
        max_chars = int(os.getenv("EBURON_KOKORO_MAX_SEGMENT_CHARS", "280"))
        punctuation = ".!?;:。！？；：\n"

        while self.buffer:
            text = self.buffer.lstrip()
            dropped = len(self.buffer) - len(text)
            if dropped:
                self.buffer = text
            if not text:
                self.buffer = ""
                break

            cut = -1
            for idx, ch in enumerate(text):
                if idx + 1 >= min_chars and ch in punctuation:
                    cut = idx + 1
                    break
            if cut < 0 and len(text) >= max_chars:
                window = text[:max_chars]
                cut = max(window.rfind(" "), window.rfind(","), window.rfind("，"))
                if cut < min_chars:
                    cut = max_chars
            if cut < 0:
                if final:
                    cut = len(text)
                else:
                    break

            segment = text[:cut].strip()
            self.buffer = text[cut:]
            if segment:
                segments.append(segment)

        return segments

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if self.is_cancelled():
                    break
                seq, text = item
                try:
                    self.put_event(
                        "tts_start",
                        {
                            "session_id": self.session_id,
                            "stream_id": self.stream_id,
                            "seq": seq,
                            "engine": "kokoro",
                            "text": text,
                        },
                    )
                    wav, meta = synthesize_wav(
                        text,
                        voice=self.options.voice,
                        speed=self.options.speed,
                        language=self.options.language,
                    )
                    if self.is_cancelled():
                        break
                    self.had_audio = True
                    self.put_event(
                        "tts_audio",
                        {
                            "session_id": self.session_id,
                            "stream_id": self.stream_id,
                            "seq": seq,
                            "engine": "kokoro",
                            "mime": "audio/wav",
                            "audio_base64": base64.b64encode(wav).decode("ascii"),
                            "text": text,
                            "voice_id": meta.get("voice_id"),
                            "lang_code": meta.get("lang_code"),
                        },
                    )
                except KokoroTTSError as exc:
                    logger.warning("Kokoro segment failed for stream %s: %s", self.stream_id, exc)
                    self.put_event(
                        "tts_error",
                        {
                            "session_id": self.session_id,
                            "stream_id": self.stream_id,
                            "seq": seq,
                            "engine": "kokoro",
                            "message": str(exc),
                        },
                    )
                    break
        finally:
            if self.had_audio and not self.is_cancelled():
                self.put_event(
                    "tts_end",
                    {
                        "session_id": self.session_id,
                        "stream_id": self.stream_id,
                        "engine": "kokoro",
                    },
                )


@dataclass
class KokoroOptions:
    enabled: bool = False
    realtime: bool = False
    voice: str = ""
    speed: float = 1.0
    language: str = ""


def normalize_kokoro_options(raw: object) -> KokoroOptions | None:
    if not isinstance(raw, dict):
        return None
    if not raw.get("enabled") and not raw.get("realtime"):
        return None
    return KokoroOptions(
        enabled=kokoro_enabled(),
        realtime=bool(raw.get("realtime")),
        voice=str(raw.get("voice") or _default_voice()).strip(),
        speed=float(raw.get("speed") or 1.0),
        language=str(raw.get("language") or "").strip(),
    )


import tempfile  # noqa: E402 放在文件末尾避免循环导入
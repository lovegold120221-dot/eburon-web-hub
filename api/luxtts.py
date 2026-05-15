"""Voicebox LuxTTS integration for WebUI response audio."""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

VOICEBOX_URL_ENV_KEYS = ("EBURON_VOICEBOX_URL", "VOICEBOX_URL", "LUXTTS_URL")
VOICEBOX_PROFILE_ENV_KEYS = (
    "EBURON_LUXTTS_PROFILE_ID",
    "VOICEBOX_PROFILE_ID",
    "LUXTTS_PROFILE_ID",
)
VOICEBOX_ROOT_ENV_KEYS = ("EBURON_VOICEBOX_ROOT", "VOICEBOX_ROOT")
VOICEBOX_SERVER_ENV_KEYS = ("EBURON_VOICEBOX_SERVER", "VOICEBOX_SERVER")
VOICEBOX_PYTHON_ENV_KEYS = ("EBURON_VOICEBOX_PYTHON", "VOICEBOX_PYTHON")
VOICEBOX_DATA_DIR_ENV_KEYS = ("EBURON_VOICEBOX_DATA_DIR", "VOICEBOX_DATA_DIR")
VOICEBOX_LOG_ENV_KEYS = ("EBURON_VOICEBOX_LOG", "VOICEBOX_LOG")

_ALLOWED_LANGUAGES = {
    "zh",
    "en",
    "ja",
    "ko",
    "de",
    "fr",
    "ru",
    "pt",
    "es",
    "it",
    "he",
    "ar",
    "da",
    "el",
    "fi",
    "hi",
    "ms",
    "nl",
    "no",
    "pl",
    "sv",
    "sw",
    "tr",
}
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE: dict[str, object] = {"at": 0.0, "profiles": []}
_AUTOSTART_LOCK = threading.Lock()
_AUTOSTART_PROCESS: subprocess.Popen | None = None
_AUTOSTART_LAST_ATTEMPT = 0.0
_AUTOSTART_LAST_ERROR = ""
_AUTOSTART_LOG_PATH = ""


class VoiceboxLuxTTSError(RuntimeError):
    """Raised when the local Voicebox LuxTTS sidecar cannot satisfy a request."""


@dataclass
class LuxTTSOptions:
    enabled: bool = False
    realtime: bool = False
    engine: str = "kokoro"  # "kokoro" or "luxtts"
    profile_id: str = ""   # for luxtts
    voice: str = ""        # for kokoro
    speed: float = 1.0    # for kokoro
    language: str = "en"


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def _env_value(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return ""


def luxtts_enabled() -> bool:
    if not _env_flag("EBURON_LUXTTS_ENABLED", True):
        return False
    engine = (
        os.getenv("EBURON_TTS_ENGINE")
        or os.getenv("TTS_ENGINE")
        or os.getenv("VOICEBOX_TTS_ENGINE")
        or "luxtts"
    )
    return str(engine).strip().lower() in {"luxtts", "voicebox", "voicebox_luxtts"}


def voicebox_base_url() -> str:
    for key in VOICEBOX_URL_ENV_KEYS:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip().rstrip("/")
    return "http://127.0.0.1:17493"


def voicebox_autostart_enabled() -> bool:
    return _env_flag("EBURON_VOICEBOX_AUTOSTART", True)


def _voicebox_endpoint() -> tuple[str, int]:
    parsed = urlparse(voicebox_base_url())
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _is_local_voicebox() -> bool:
    host, _port = _voicebox_endpoint()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _voicebox_backend_dir() -> Path | None:
    configured = _env_value(VOICEBOX_ROOT_ENV_KEYS)
    candidates: list[Path] = []
    if configured:
        root = Path(configured).expanduser()
        candidates.extend([root, root / "backend"])
    home = Path.home()
    candidates.extend([home / "voicebox" / "backend", Path("/Users/eburon/voicebox/backend")])
    for candidate in candidates:
        if (candidate / "main.py").is_file() and (candidate.parent / "backend").is_dir():
            return candidate.resolve()
    return None


def _voicebox_python(backend_dir: Path) -> Path:
    configured = _env_value(VOICEBOX_PYTHON_ENV_KEYS)
    candidates = [
        Path(configured).expanduser() if configured else None,
        backend_dir / "venv" / "bin" / "python",
        backend_dir / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    return Path(sys.executable)


def _voicebox_server_binary() -> Path | None:
    configured = _env_value(VOICEBOX_SERVER_ENV_KEYS)
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path("/Applications/Voicebox.app/Contents/MacOS/voicebox-server"),
            Path.home() / "Applications" / "Voicebox.app" / "Contents" / "MacOS" / "voicebox-server",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _voicebox_data_dir(backend_dir: Path) -> Path:
    configured = _env_value(VOICEBOX_DATA_DIR_ENV_KEYS)
    if configured:
        return Path(configured).expanduser().resolve()
    app_data = Path.home() / "Library" / "Application Support" / "sh.voicebox.app"
    if app_data.exists():
        return app_data.resolve()
    return (backend_dir.parent / "data").resolve()


def _voicebox_log_path() -> Path:
    configured = _env_value(VOICEBOX_LOG_ENV_KEYS)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".hermes" / "voicebox-luxtts.log").resolve()


def _default_profile_id() -> str:
    for key in VOICEBOX_PROFILE_ENV_KEYS:
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return "Jenny"


def normalize_language(raw: object) -> str:
    value = str(raw or "en").strip().lower().replace("_", "-")
    if "-" in value:
        value = value.split("-", 1)[0]
    return value if value in _ALLOWED_LANGUAGES else "en"


def normalize_luxtts_options(raw: object) -> LuxTTSOptions:
    if not isinstance(raw, dict):
        return LuxTTSOptions()
    requested = bool(raw.get("enabled") or raw.get("realtime"))
    engine = str(raw.get("engine") or "kokoro").strip().lower()

    # Only import kokoro here to avoid circular import
    from api import kokoro as _kokoro

    if engine == "luxtts":
        enabled = luxtts_enabled() and requested
    else:
        # kokoro or default
        enabled = _kokoro.kokoro_enabled() and requested

    return LuxTTSOptions(
        enabled=enabled,
        realtime=bool(raw.get("realtime", requested)),
        engine=engine,
        profile_id=str(raw.get("profile_id") or "").strip(),
        voice=str(raw.get("voice") or "").strip(),
        speed=float(raw.get("speed") or 1.0),
        language=normalize_language(raw.get("language")),
    )


def _json_request(path: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 5.0):
    url = voicebox_base_url() + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise VoiceboxLuxTTSError(f"Voicebox HTTP {exc.code}: {_extract_error_message(body)}") from exc
    except Exception as exc:
        raise VoiceboxLuxTTSError(f"Voicebox unavailable at {voicebox_base_url()}: {exc}") from exc


def _probe_health(timeout: float = 2.5):
    return _json_request("/health", timeout=timeout)


def _start_voicebox_process() -> dict:
    global _AUTOSTART_LAST_ATTEMPT, _AUTOSTART_LAST_ERROR, _AUTOSTART_LOG_PATH, _AUTOSTART_PROCESS

    if not voicebox_autostart_enabled():
        raise VoiceboxLuxTTSError("Voicebox autostart is disabled")
    if not _is_local_voicebox():
        raise VoiceboxLuxTTSError("Voicebox autostart only supports local Voicebox URLs")

    with _AUTOSTART_LOCK:
        now = time.time()
        if _AUTOSTART_PROCESS is not None and _AUTOSTART_PROCESS.poll() is None:
            return {"started": False, "pid": _AUTOSTART_PROCESS.pid, "already_running": True}
        if now - _AUTOSTART_LAST_ATTEMPT < 3:
            raise VoiceboxLuxTTSError(_AUTOSTART_LAST_ERROR or "Voicebox autostart is already in progress")

        _AUTOSTART_LAST_ATTEMPT = now
        backend_dir = _voicebox_backend_dir()
        if backend_dir is None:
            _AUTOSTART_LAST_ERROR = "Voicebox backend was not found. Set EBURON_VOICEBOX_ROOT to your voicebox checkout."
            raise VoiceboxLuxTTSError(_AUTOSTART_LAST_ERROR)

        data_dir = _voicebox_data_dir(backend_dir)
        log_path = _voicebox_log_path()
        host, port = _voicebox_endpoint()
        server_binary = _voicebox_server_binary()
        bind_host = "127.0.0.1" if host in {"localhost", "0.0.0.0"} else host
        if server_binary:
            cmd = [
                str(server_binary),
                "--host",
                bind_host,
                "--port",
                str(port),
                "--data-dir",
                str(data_dir),
            ]
            cwd = server_binary.parent
        else:
            python = _voicebox_python(backend_dir)
            cmd = [
                str(python),
                "-m",
                "backend.main",
                "--host",
                bind_host,
                "--port",
                str(port),
                "--data-dir",
                str(data_dir),
            ]
            cwd = backend_dir.parent
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("VOICEBOX_DATA_DIR", str(data_dir))
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as log_file:
                _AUTOSTART_PROCESS = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=env,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )
        except OSError as exc:
            _AUTOSTART_LAST_ERROR = f"Voicebox autostart failed: {exc}"
            raise VoiceboxLuxTTSError(_AUTOSTART_LAST_ERROR) from exc
        _AUTOSTART_LOG_PATH = str(log_path)
        _AUTOSTART_LAST_ERROR = ""
        logger.info("Started Voicebox LuxTTS sidecar pid=%s log=%s", _AUTOSTART_PROCESS.pid, log_path)
        return {
            "started": True,
            "pid": _AUTOSTART_PROCESS.pid,
            "backend_dir": str(backend_dir),
            "server": str(server_binary) if server_binary else "python",
            "data_dir": str(data_dir),
            "log": str(log_path),
        }


def ensure_voicebox_available(*, timeout: float = 20.0) -> dict:
    initial_error: VoiceboxLuxTTSError | None = None
    try:
        health = _probe_health(timeout=min(timeout, 2.5))
        return {"ok": True, "health": health, "autostart": {"started": False, "already_running": True}}
    except VoiceboxLuxTTSError as first_error:
        initial_error = first_error
        if not luxtts_enabled():
            raise VoiceboxLuxTTSError("LuxTTS is disabled by EBURON_LUXTTS_ENABLED or EBURON_TTS_ENGINE") from first_error
        autostart_info = _start_voicebox_process()

    deadline = time.time() + max(1.0, timeout)
    last_error: Exception | None = initial_error
    while time.time() < deadline:
        try:
            health = _probe_health(timeout=2.5)
            return {"ok": True, "health": health, "autostart": autostart_info}
        except VoiceboxLuxTTSError as exc:
            last_error = exc
            time.sleep(0.5)
    log_hint = f" Check {_AUTOSTART_LOG_PATH}." if _AUTOSTART_LOG_PATH else ""
    raise VoiceboxLuxTTSError(f"Voicebox LuxTTS did not become ready: {last_error}.{log_hint}")


def _audio_request(path: str, payload: dict, *, timeout: float = 120.0) -> bytes:
    url = voicebox_base_url() + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if not data:
                raise VoiceboxLuxTTSError("Voicebox returned an empty audio response")
            return data
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise VoiceboxLuxTTSError(f"Voicebox HTTP {exc.code}: {_extract_error_message(body)}") from exc
    except VoiceboxLuxTTSError:
        raise
    except Exception as exc:
        raise VoiceboxLuxTTSError(f"Voicebox audio request failed: {exc}") from exc


def _extract_error_message(body: str) -> str:
    text = str(body or "").strip()
    if not text:
        return "empty error body"
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            detail = parsed.get("detail") or parsed.get("error") or parsed.get("message")
            if detail:
                return str(detail)[:500]
    except Exception:
        pass
    return re.sub(r"\s+", " ", text)[:500]


def _load_profiles(refresh: bool = False) -> list[dict]:
    now = time.time()
    with _PROFILE_CACHE_LOCK:
        cached = list(_PROFILE_CACHE.get("profiles") or [])
        if cached and not refresh and now - float(_PROFILE_CACHE.get("at") or 0) < 10:
            return cached
    profiles = _json_request("/profiles", timeout=6.0)
    if not isinstance(profiles, list):
        profiles = []
    clean = [p for p in profiles if isinstance(p, dict)]
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE["at"] = now
        _PROFILE_CACHE["profiles"] = clean
    return clean


def select_profile_id(preferred: str = "", language: str = "en") -> str:
    preferred = (preferred or _default_profile_id()).strip()

    profiles = _load_profiles()
    if not profiles:
        raise VoiceboxLuxTTSError("Voicebox has no voice profiles. Create/import a profile with a sample first.")

    if preferred:
        # 1) exact ID match
        for p in profiles:
            if str(p.get("id") or "").strip() == preferred:
                return preferred
        # 2) case-insensitive name match
        for p in profiles:
            if str(p.get("name") or "").strip().lower() == preferred.lower():
                return str(p.get("id") or "").strip()

    def has_sample(profile: dict) -> bool:
        try:
            return int(profile.get("sample_count") or 0) > 0
        except Exception:
            return False

    preferred_language = normalize_language(language)

    def profile_language(profile: dict) -> str:
        return normalize_language(profile.get("language") or "")

    ranked = sorted(
        profiles,
        key=lambda p: (
            str(p.get("default_engine") or "").lower() == "luxtts",
            has_sample(p),
            profile_language(p) == preferred_language,
            profile_language(p) == "en",
            str(p.get("updated_at") or p.get("created_at") or ""),
            str(p.get("name") or "").lower(),
        ),
        reverse=True,
    )
    return str(ranked[0].get("id") or "").strip()


def status_payload() -> dict:
    payload = {
        "enabled": luxtts_enabled(),
        "voicebox_url": voicebox_base_url(),
        "engine": "luxtts",
        "healthy": False,
        "selected_profile_id": "",
        "profiles": [],
        "autostart_enabled": voicebox_autostart_enabled(),
    }
    if not payload["enabled"]:
        return payload
    try:
        availability = ensure_voicebox_available(timeout=float(os.getenv("EBURON_VOICEBOX_STATUS_TIMEOUT_SECONDS", "30")))
        payload["health"] = availability.get("health")
        payload["healthy"] = True
        payload["autostart"] = availability.get("autostart")
    except VoiceboxLuxTTSError as exc:
        payload["error"] = str(exc)
        if _AUTOSTART_LOG_PATH:
            payload["log"] = _AUTOSTART_LOG_PATH
        return payload
    try:
        profiles = _load_profiles(refresh=True)
        payload["profiles"] = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "language": p.get("language"),
                "default_engine": p.get("default_engine"),
                "sample_count": p.get("sample_count"),
            }
            for p in profiles
        ]
        payload["selected_profile_id"] = select_profile_id(language="en")
    except VoiceboxLuxTTSError as exc:
        payload["error"] = str(exc)
    return payload


def _old_status_payload() -> dict:
    payload = {
        "enabled": luxtts_enabled(),
        "voicebox_url": voicebox_base_url(),
        "engine": "luxtts",
        "healthy": False,
        "selected_profile_id": "",
        "profiles": [],
    }
    if not payload["enabled"]:
        return payload
    try:
        payload["health"] = _json_request("/health", timeout=2.5)
        payload["healthy"] = True
    except VoiceboxLuxTTSError as exc:
        payload["error"] = str(exc)
        return payload
    try:
        profiles = _load_profiles(refresh=True)
        payload["profiles"] = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "language": p.get("language"),
                "default_engine": p.get("default_engine"),
                "sample_count": p.get("sample_count"),
            }
            for p in profiles
        ]
        payload["selected_profile_id"] = select_profile_id(language="en")
    except VoiceboxLuxTTSError as exc:
        payload["error"] = str(exc)
    return payload


def sanitize_tts_text(text: object) -> str:
    value = str(text or "")
    # Replace balanced fenced code blocks (```...```) with a spoken placeholder.
    value = re.sub(
        r"```[\s\S]*?```",
        " Just look at this code in the chat. ",
        value,
    )
    # If any ``` remains (unbalanced) the rest of this segment is code.
    # Drop it so raw code never reaches the speaker during streaming.
    value = re.sub(r"```.*$", " Just look at this code in the chat.", value, flags=re.DOTALL)
    # Replace inline code `like this` with a short placeholder.
    value = re.sub(r"`[^`]+`", " code snippet ", value)
    # Remove markdown images entirely (not meaningful to speak).
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    # Keep link text, drop the URL.
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"MEDIA:[^\s]+", "a file", value)
    # Remove HTML tags.
    value = re.sub(r"<[^>]+>", " ", value)
    # Collapse markdown formatting chars to spaces.
    value = re.sub(r"[*_#>~]+", " ", value)
    # Normalise whitespace.
    value = re.sub(r"\s+", " ", value).strip()
    # Clean up repeated placeholder phrases caused by adjacent code blocks.
    placeholder = "Just look at this code in the chat."
    value = re.sub(
        r"(?:" + re.escape(placeholder) + r"\s*){2,}",
        placeholder + " ",
        value,
    )
    return value


def synthesize_wav(
    text: object,
    *,
    profile_id: str = "",
    language: str = "en",
    max_chunk_chars: int | None = None,
    timeout: float | None = None,
) -> tuple[bytes, dict]:
    clean = sanitize_tts_text(text)
    if not clean:
        raise VoiceboxLuxTTSError("No speakable text supplied")
    ensure_voicebox_available(timeout=float(os.getenv("EBURON_VOICEBOX_SYNTH_TIMEOUT_SECONDS", "30")))
    resolved_language = normalize_language(language)
    profile = select_profile_id(profile_id, language=resolved_language)
    if not profile:
        raise VoiceboxLuxTTSError("No Voicebox profile id could be selected")
    payload = {
        "text": clean,
        "profile_id": profile,
        "language": resolved_language,
        "engine": "luxtts",
        "normalize": True,
        "crossfade_ms": 0,
        "max_chunk_chars": int(max_chunk_chars or os.getenv("EBURON_LUXTTS_MAX_CHUNK_CHARS", "360")),
    }
    wav = _audio_request(
        "/generate/stream",
        payload,
        timeout=float(timeout or os.getenv("EBURON_LUXTTS_TIMEOUT_SECONDS", "120")),
    )
    return wav, {"profile_id": profile, "language": payload["language"], "engine": "luxtts"}


class RealtimeLuxTTSBridge:
    """Turns streamed assistant text into ordered Voicebox LuxTTS SSE audio events."""

    def __init__(
        self,
        put_event: Callable[[str, dict], None],
        *,
        session_id: str,
        stream_id: str,
        options: LuxTTSOptions | None,
        is_cancelled: Callable[[], bool],
    ) -> None:
        self.put_event = put_event
        self.session_id = session_id
        self.stream_id = stream_id
        self.options = options or LuxTTSOptions()
        self.is_cancelled = is_cancelled
        self.enabled = bool(self.options.enabled and self.options.realtime and luxtts_enabled())
        self.buffer = ""
        self.seq = 0
        self.had_audio = False
        self._closed = False
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[tuple[int, str] | None] = queue.Queue(
            maxsize=int(os.getenv("EBURON_LUXTTS_QUEUE_SIZE", "10"))
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
                    "message": "LuxTTS queue was full at stream end",
                },
            )
            return
        if self._worker is not None:
            self._worker.join(timeout=float(os.getenv("EBURON_LUXTTS_FINISH_TIMEOUT_SECONDS", "180")))
            if self._worker.is_alive():
                self.put_event(
                    "tts_error",
                    {
                        "session_id": self.session_id,
                        "stream_id": self.stream_id,
                        "message": "LuxTTS did not finish before the stream timeout",
                    },
                )

    def stop(self) -> None:
        self._closed = True
        self.buffer = ""

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name=f"luxtts-{self.stream_id[:8]}", daemon=True)
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
                    "message": "LuxTTS queue full; skipped one response audio segment",
                },
            )

    def _drain_ready_segments(self, *, final: bool) -> list[str]:
        segments: list[str] = []
        min_chars = int(os.getenv("EBURON_LUXTTS_MIN_SEGMENT_CHARS", "90"))
        max_chars = int(os.getenv("EBURON_LUXTTS_MAX_SEGMENT_CHARS", "280"))
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
                            "engine": "luxtts",
                            "text": text,
                        },
                    )
                    wav, meta = synthesize_wav(
                        text,
                        profile_id=self.options.profile_id,
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
                            "engine": "luxtts",
                            "mime": "audio/wav",
                            "audio_base64": base64.b64encode(wav).decode("ascii"),
                            "text": text,
                            "profile_id": meta.get("profile_id"),
                            "language": meta.get("language"),
                        },
                    )
                except VoiceboxLuxTTSError as exc:
                    logger.warning("LuxTTS segment failed for stream %s: %s", self.stream_id, exc)
                    self.put_event(
                        "tts_error",
                        {
                            "session_id": self.session_id,
                            "stream_id": self.stream_id,
                            "seq": seq,
                            "engine": "luxtts",
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
                        "engine": "luxtts",
                    },
                )

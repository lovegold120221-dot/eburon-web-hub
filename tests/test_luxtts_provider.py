import io
import inspect

from api import luxtts, routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def test_tts_speak_uses_luxtts_for_luxtts_engine(monkeypatch):
    calls = {}

    def fake_synthesize_wav(text, *, profile_id="", language="en", **_kwargs):
        calls["text"] = text
        calls["profile_id"] = profile_id
        calls["language"] = language
        return b"RIFFfake-wav", {"engine": "luxtts", "profile_id": "voice-1", "language": "en"}

    monkeypatch.setattr(luxtts, "synthesize_wav", fake_synthesize_wav)

    handler = _FakeHandler()
    routes._handle_tts_speak(handler, {"text": "Hello Master E", "engine": "luxtts", "profile_id": "Jenny", "language": "en-US"})

    headers = dict(handler.headers)
    assert handler.status == 200
    assert handler.wfile.getvalue() == b"RIFFfake-wav"
    assert headers["Content-Type"] == "audio/wav"
    assert headers["X-TTS-Engine"] == "luxtts"
    assert headers["X-TTS-Voice"] == "voice-1"
    assert calls == {"text": "Hello Master E", "profile_id": "Jenny", "language": "en-US"}


def test_tts_speak_no_longer_uses_tmp_wrapper():
    src = inspect.getsource(routes._handle_tts_speak)
    assert "/tmp/voicebox_tts.py" not in src
    assert "synthesize_wav" in src
    assert "X-TTS-Engine" in src and "luxtts" in src


def test_luxtts_status_reports_real_voicebox_state(monkeypatch):
    monkeypatch.setenv("EBURON_LUXTTS_ENABLED", "true")
    monkeypatch.setenv("EBURON_TTS_ENGINE", "luxtts")
    monkeypatch.setattr(luxtts, "ensure_voicebox_available", lambda timeout=12.0: {"health": {"status": "healthy"}})
    monkeypatch.setattr(
        luxtts,
        "_load_profiles",
        lambda refresh=False: [{"id": "voice-1", "name": "Jenny", "language": "en", "sample_count": 1}],
    )
    monkeypatch.setattr(luxtts, "select_profile_id", lambda language="en": "voice-1")

    payload = luxtts.status_payload()

    assert payload["engine"] == "luxtts"
    assert payload["healthy"] is True
    assert payload["selected_profile_id"] == "voice-1"
    assert payload["profiles"][0]["name"] == "Jenny"

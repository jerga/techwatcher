import base64

from google import genai
from google.genai import types

from connector_base import BaseTTSConnector


class GoogleAIStudioTTSConnector(BaseTTSConnector):
    """Text-to-Speech connector for Google AI Studio (Gemini)."""

    def generate_audio(self, model: str, text: str, voice_id: str, **kwargs) -> bytes:
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")
        if not model or not model.strip():
            raise ValueError("Model name cannot be empty")
        if not voice_id or not voice_id.strip():
            raise ValueError("Voice ID cannot be empty")

        client = genai.Client(api_key=self.api_key)

        try:
            response = client.models.generate_content(
                model=model,
                contents=text,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_id,
                            )
                        )
                    ),
                ),
            )
        except Exception as exc:
            message = str(exc).lower()
            if any(keyword in message for keyword in ["context", "token", "too long", "length"]):
                raise ValueError("Text is too long for a single TTS request; shorten and retry.") from exc
            raise RuntimeError(f"Google AI Studio TTS failed: {str(exc)}") from exc

        return self._extract_audio_bytes(response)

    @staticmethod
    def _extract_audio_bytes(response) -> bytes:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                if not inline_data:
                    continue
                data = getattr(inline_data, "data", None)
                if data:
                    if isinstance(data, str):
                        return base64.b64decode(data)
                    return data

        raise RuntimeError("Google AI Studio TTS returned no audio data")

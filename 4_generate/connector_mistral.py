import base64

from connector_base import BaseTTSConnector

try:
    from mistralai.client import Mistral
except ImportError:
    Mistral = None


class MistralVoxtralTTSConnector(BaseTTSConnector):
    """Text-to-Speech connector for Mistral Voxtral TTS."""

    def generate_audio(self, model: str, text: str, voice_id: str, **kwargs) -> bytes:
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")
        if not model or not model.strip():
            raise ValueError("Model name cannot be empty")
        if not voice_id or not voice_id.strip():
            raise ValueError("Voice ID cannot be empty")

        if Mistral is None:
            raise RuntimeError("mistralai is required for Mistral Voxtral TTS")

        client = Mistral(api_key=self.api_key)

        try:
            audio = client.audio.speech.complete(
                model=model,
                input=text,
                response_format="wav",
                stream=False,
                voice_id=voice_id,
            )
        except Exception as exc:
            raise RuntimeError(f"Mistral Voxtral TTS failed: {str(exc)}") from exc

        if not audio or not hasattr(audio, "audio_data"):
            raise RuntimeError("Mistral Voxtral TTS returned no audio data")

        try:
            audio_bytes = base64.b64decode(audio.audio_data)
        except Exception as exc:
            raise RuntimeError(f"Failed to decode Mistral audio data: {str(exc)}") from exc

        if not audio_bytes:
            raise RuntimeError("Decoded audio data is empty")

        return audio_bytes

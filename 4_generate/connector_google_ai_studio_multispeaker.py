import base64
import re

from google import genai
from google.genai import types

from connector_base import BaseTTSConnector


class MultiSpeakerGoogleAIStudioTTSConnector(BaseTTSConnector):
    """Multi-speaker Text-to-Speech connector for Google AI Studio (Gemini).
    
    Generates podcast audio with multiple speakers, each with their own voice.
    Text input must be formatted with speaker prefixes: "Speaker1: text\nSpeaker2: text"
    """

    def generate_audio(
        self,
        model: str,
        text: str,
        speaker1_name: str,
        speaker1_voice: str,
        speaker2_name: str,
        speaker2_voice: str,
        **kwargs
    ) -> bytes:
        """Generate multi-speaker audio from formatted text.
        
        Args:
            model: Model name (e.g., "gemini-2.5-flash-preview-tts")
            text: Text with speaker prefixes, e.g., "Speaker1: Hello\nSpeaker2: Hi"
            speaker1_name: Speaker 1 identifier (e.g., "Speaker1")
            speaker1_voice: Speaker 1 voice name (e.g., "Zephyr")
            speaker2_name: Speaker 2 identifier (e.g., "Speaker2")
            speaker2_voice: Speaker 2 voice name (e.g., "Puck")
        
        Returns:
            Raw audio bytes (PCM format)
        
        Raises:
            ValueError: If input validation fails
            RuntimeError: If Google API call fails
        """
        if not text or not text.strip():
            raise ValueError("Text cannot be empty")
        if not model or not model.strip():
            raise ValueError("Model name cannot be empty")
        if not speaker1_name or not speaker1_name.strip():
            raise ValueError("Speaker 1 name cannot be empty")
        if not speaker1_voice or not speaker1_voice.strip():
            raise ValueError("Speaker 1 voice cannot be empty")
        if not speaker2_name or not speaker2_name.strip():
            raise ValueError("Speaker 2 name cannot be empty")
        if not speaker2_voice or not speaker2_voice.strip():
            raise ValueError("Speaker 2 voice cannot be empty")

        # Validate that text contains both speaker names
        speaker1_pattern = re.escape(speaker1_name) + r"\s*:"
        speaker2_pattern = re.escape(speaker2_name) + r"\s*:"
        
        if not re.search(speaker1_pattern, text, re.IGNORECASE):
            raise ValueError(
                f"Text does not contain speaker '{speaker1_name}:' prefix. "
                f"Expected format: '{speaker1_name}: text' and '{speaker2_name}: text'"
            )
        if not re.search(speaker2_pattern, text, re.IGNORECASE):
            raise ValueError(
                f"Text does not contain speaker '{speaker2_name}:' prefix. "
                f"Expected format: '{speaker1_name}: text' and '{speaker2_name}: text'"
            )

        client = genai.Client(api_key=self.api_key)

        try:
            contents = [
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                )
            ]

            config = types.GenerateContentConfig(
                temperature=1,
                response_modalities=["audio"],
                speech_config=types.SpeechConfig(
                    multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                        speaker_voice_configs=[
                            types.SpeakerVoiceConfig(
                                speaker=speaker1_name,
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=speaker1_voice,
                                    )
                                ),
                            ),
                            types.SpeakerVoiceConfig(
                                speaker=speaker2_name,
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=speaker2_voice,
                                    )
                                ),
                            ),
                        ]
                    )
                ),
            )

            response_stream = client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            message = str(exc).lower()
            if any(keyword in message for keyword in ["context", "token", "too long", "length"]):
                raise ValueError("Text is too long for a single TTS request; shorten and retry.") from exc
            raise RuntimeError(f"Google AI Studio multi-speaker TTS failed: {str(exc)}") from exc

        return self._extract_audio_bytes_from_stream(response_stream)

    @staticmethod
    def _extract_audio_bytes_from_stream(response_stream) -> bytes:
        """Extract and concatenate audio bytes from a streaming response."""
        audio_chunks = []

        for chunk in response_stream:
            # Google example exposes chunk.parts directly in stream mode.
            parts = getattr(chunk, "parts", None) or []
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                if not inline_data:
                    continue
                data = getattr(inline_data, "data", None)
                if not data:
                    continue
                if isinstance(data, str):
                    audio_chunks.append(base64.b64decode(data))
                else:
                    audio_chunks.append(data)

        if not audio_chunks:
            raise RuntimeError("Google AI Studio multi-speaker TTS returned no audio data")

        return b"".join(audio_chunks)

    @staticmethod
    def _extract_audio_bytes(response) -> bytes:
        """Extract audio bytes from Google API response."""
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

        raise RuntimeError("Google AI Studio multi-speaker TTS returned no audio data")

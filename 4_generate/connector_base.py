from abc import ABC, abstractmethod


class BaseTTSConnector(ABC):
    """Abstract base class for Text-to-Speech connectors."""

    def __init__(self, api_key: str):
        if not api_key or not api_key.strip():
            raise ValueError("API key cannot be empty")
        self.api_key = api_key

    @abstractmethod
    def generate_audio(self, model: str, text: str, voice_id: str, **kwargs) -> bytes:
        """Generate audio from text and return raw bytes."""
        raise NotImplementedError

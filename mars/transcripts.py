import json
from pathlib import Path
from typing import List, Protocol

from .errors import ProcessingError
from .tools import validate_transcript


class TranscriptSource(Protocol):
    def get(self, video_id: str) -> List[dict]:
        ...


class FixtureTranscriptSource:
    """Локальный адаптер для проверки сценариев до выбора поставщика."""

    def __init__(self, path):
        self._records = json.loads(Path(path).read_text(encoding="utf-8"))

    def get(self, video_id):
        if video_id not in self._records:
            raise ProcessingError("TRANSCRIPT_UNAVAILABLE", "В тестовом наборе нет этого ролика")
        transcript = self._records[video_id]
        try:
            validate_transcript(transcript)
        except ProcessingError as error:
            raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", str(error)) from error
        return transcript

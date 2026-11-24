import re
from typing import List, Protocol

from .errors import ProcessingError
# Сохраняем прежний импорт mars.tools.Registry для сборки S1 и её тестов.
from .registry import Registry


def validate_transcript(transcript):
    if not isinstance(transcript, list) or not transcript:
        raise ProcessingError("INVALID_TRANSCRIPT", "Ожидались непустые субтитры")
    previous = -1
    for segment in transcript:
        if not isinstance(segment, dict):
            raise ProcessingError("INVALID_TRANSCRIPT", "Некорректная реплика")
        start = segment.get("startMs")
        text = segment.get("text")
        if type(start) is not int or start < 0 or start < previous:
            raise ProcessingError("INVALID_TRANSCRIPT", "Некорректный порядок или таймкод")
        if not isinstance(text, str) or not text.strip():
            raise ProcessingError("INVALID_TRANSCRIPT", "Пустой текст реплики")
        previous = start


def extract_points(transcript, limit=5):
    points = []
    for segment in transcript:
        for sentence in re.split(r"(?<=[.!?])\s+", segment["text"].strip()):
            if sentence not in points:
                points.append(sentence)
            if len(points) == limit:
                return points
    return points


class Tool(Protocol):
    def run(self, transcript: List[dict]) -> dict:
        ...


class BriefSummaryTool:
    def run(self, transcript):
        validate_transcript(transcript)
        return {"points": extract_points(transcript)}


class TimelineDigestTool:
    def run(self, transcript):
        validate_transcript(transcript)
        groups = {}
        for segment in transcript:
            minute = segment["startMs"] // 60000
            groups.setdefault(minute, []).append(segment)
        return {"segments": [
            {"startMs": segments[0]["startMs"], "points": extract_points(segments)}
            for segments in groups.values()
        ]}

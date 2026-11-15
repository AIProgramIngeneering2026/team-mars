"""Адаптер публичных субтитров YouTube. Детали библиотеки остаются здесь."""
import json
import logging
import math
import time
from xml.etree.ElementTree import ParseError

import requests
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeTranscriptApi,
)

from .errors import ProcessingError
from .tools import validate_transcript

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_SECONDS = 10


class BoundedSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs["timeout"] = (REQUEST_SECONDS, REQUEST_SECONDS)
        kwargs["stream"] = True
        started = time.monotonic()
        with super().request(method, url, **kwargs) as response:
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ProcessingError("TRANSCRIPT_TOO_LARGE", "Ответ источника превышает предел")
                if time.monotonic() - started > REQUEST_SECONDS:
                    raise requests.Timeout("Истёк срок чтения ответа")
                chunks.append(chunk)
            # Библиотека читает Response.text/json. Сохраняем уже ограниченное тело.
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response


def normalize_transcript(records):
    if not isinstance(records, list) or not records:
        raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", "Пустые или некорректные субтитры")
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", "Некорректная реплика")
        start = record.get("start")
        text = record.get("text")
        if (type(start) not in (int, float) or start < 0
                or (type(start) is float and not math.isfinite(start * 1000))
                or not isinstance(text, str) or not text.strip()):
            raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", "Некорректный таймкод или текст")
        normalized.append({"startMs": round(start * 1000), "text": text.strip()})
    normalized.sort(key=lambda segment: segment["startMs"])
    try:
        validate_transcript(normalized)
    except ProcessingError as error:
        raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", "Некорректные субтитры") from error
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ProcessingError("TRANSCRIPT_TOO_LARGE", "Субтитры превышают предел")
    return normalized


class YouTubeTranscriptSource:
    def get(self, video_id):
        try:
            with BoundedSession() as session:
                api = YouTubeTranscriptApi(http_client=session)
                transcripts = api.list(video_id)
                try:
                    selected = transcripts.find_transcript(["ru", "en"])
                except NoTranscriptFound:
                    selected = next(iter(transcripts), None)
                    if selected is None:
                        raise ProcessingError("TRANSCRIPT_UNAVAILABLE", "Субтитры недоступны")
                return normalize_transcript(selected.fetch().to_raw_data())
        except ProcessingError:
            raise
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, VideoUnplayable, AgeRestricted) as error:
            raise ProcessingError("TRANSCRIPT_UNAVAILABLE", "Ролик или субтитры недоступны") from error
        except requests.Timeout as error:
            raise ProcessingError("TRANSCRIPT_TIMEOUT", "Источник не ответил вовремя") from error
        except (YouTubeDataUnparsable, ParseError, KeyError, TypeError, ValueError) as error:
            logger.warning("Некорректный ответ источника для %s: %s", video_id, type(error).__name__)
            raise ProcessingError("TRANSCRIPT_INVALID_RESPONSE", "Некорректный ответ источника") from error
        except (CouldNotRetrieveTranscript, requests.RequestException) as error:
            logger.warning("Ошибка источника для %s: %s", video_id, type(error).__name__)
            raise ProcessingError("TRANSCRIPT_SOURCE_ERROR", "Источник субтитров недоступен") from error

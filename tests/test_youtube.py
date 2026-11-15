import copy
import unittest
from unittest.mock import MagicMock, patch

import requests
from youtube_transcript_api import (
    FetchedTranscript,
    FetchedTranscriptSnippet,
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeDataUnparsable,
)

from mars.errors import ProcessingError
from mars.youtube import BoundedSession, YouTubeTranscriptSource, normalize_transcript


class YouTubeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.api_patch = patch("mars.youtube.YouTubeTranscriptApi")
        self.api_class = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.transcripts = self.api_class.return_value.list.return_value
        fetched = FetchedTranscript(
            snippets=[
                FetchedTranscriptSnippet(text="Первая реплика.", start=0.125, duration=1.0),
                FetchedTranscriptSnippet(text="Вторая реплика.", start=60.0, duration=2.0),
            ],
            video_id="abcdefghijk", language="Russian", language_code="ru", is_generated=False,
        )
        self.transcripts.find_transcript.return_value.fetch.return_value = fetched

    def test_library_response_is_normalized(self):
        self.assertEqual(YouTubeTranscriptSource().get("abcdefghijk"), [
            {"startMs": 125, "text": "Первая реплика."},
            {"startMs": 60000, "text": "Вторая реплика."},
        ])
        self.api_class.return_value.list.assert_called_once_with("abcdefghijk")
        self.transcripts.find_transcript.assert_called_once_with(["ru", "en"])

    def test_fallback_to_another_available_language(self):
        self.transcripts.find_transcript.side_effect = NoTranscriptFound("abcdefghijk", ["ru", "en"], [])
        selected = MagicMock()
        selected.fetch.return_value.to_raw_data.return_value = [{"start": 1.0, "text": "Bonjour."}]
        self.transcripts.__iter__.return_value = iter([selected])
        self.assertEqual(YouTubeTranscriptSource().get("abcdefghijk")[0]["text"], "Bonjour.")

    def test_provider_errors_are_mapped(self):
        for error, expected in [
            (TranscriptsDisabled("abcdefghijk"), "TRANSCRIPT_UNAVAILABLE"),
            (requests.Timeout("private details"), "TRANSCRIPT_TIMEOUT"),
            (YouTubeDataUnparsable("abcdefghijk"), "TRANSCRIPT_INVALID_RESPONSE"),
            (requests.ConnectionError("private details"), "TRANSCRIPT_SOURCE_ERROR"),
        ]:
            self.api_class.return_value.list.side_effect = error
            with self.subTest(expected=expected), self.assertRaises(ProcessingError) as caught:
                YouTubeTranscriptSource().get("abcdefghijk")
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("private details", str(caught.exception))

    def test_timeout_is_retried_once(self):
        with patch.object(YouTubeTranscriptSource, "_fetch") as fetch:
            fetch.side_effect = [
                ProcessingError("TRANSCRIPT_TIMEOUT", "timeout"),
                [{"startMs": 0, "text": "ok"}],
            ]
            self.assertEqual(YouTubeTranscriptSource().get("abcdefghijk")[0]["text"], "ok")
            self.assertEqual(fetch.call_count, 2)

    def test_repeated_timeout_stops_after_second_attempt(self):
        with patch.object(YouTubeTranscriptSource, "_fetch") as fetch:
            fetch.side_effect = ProcessingError("TRANSCRIPT_TIMEOUT", "timeout")
            with self.assertRaises(ProcessingError):
                YouTubeTranscriptSource().get("abcdefghijk")
            self.assertEqual(fetch.call_count, 2)

    def test_unavailable_transcript_is_not_retried(self):
        with patch.object(YouTubeTranscriptSource, "_fetch") as fetch:
            fetch.side_effect = ProcessingError("TRANSCRIPT_UNAVAILABLE", "unavailable")
            with self.assertRaises(ProcessingError):
                YouTubeTranscriptSource().get("abcdefghijk")
            self.assertEqual(fetch.call_count, 1)

    def test_sorting_is_stable_and_input_is_unchanged(self):
        raw = [
            {"start": 2.0, "text": "b"},
            {"start": 1.0, "text": " a "},
            {"start": 1.0, "text": "c"},
        ]
        before = copy.deepcopy(raw)
        self.assertEqual(normalize_transcript(raw), [
            {"startMs": 1000, "text": "a"},
            {"startMs": 1000, "text": "c"},
            {"startMs": 2000, "text": "b"},
        ])
        self.assertEqual(raw, before)

    def test_malformed_transcripts_are_rejected(self):
        for records in ([], {}, [{"text": "missing start"}], [{"start": -1, "text": "x"}],
                        [{"start": float("nan"), "text": "x"}],
                        [{"start": 1e308, "text": "x"}], [{"start": True, "text": "x"}],
                        [{"start": 0, "text": " "}]):
            with self.subTest(records=records), self.assertRaises(ProcessingError) as caught:
                normalize_transcript(records)
            self.assertEqual(caught.exception.code, "TRANSCRIPT_INVALID_RESPONSE")

    def test_normalized_transcript_size_is_limited(self):
        with patch("mars.youtube.MAX_RESPONSE_BYTES", 10), self.assertRaises(ProcessingError) as caught:
            normalize_transcript([{"start": 0, "text": "x" * 20}])
        self.assertEqual(caught.exception.code, "TRANSCRIPT_TOO_LARGE")


class NetworkBoundaryTests(unittest.TestCase):
    @patch("requests.Session.request")
    def test_network_timeout_and_body_are_bounded(self, request):
        response = requests.Response()
        response.status_code = 200
        response._content = b"test"
        response._content_consumed = True
        request.return_value.__enter__.return_value = response
        with BoundedSession() as session:
            actual = session.get("https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(actual.text, "test")
        self.assertEqual(request.call_args.kwargs["timeout"], (10, 10))
        self.assertTrue(request.call_args.kwargs["stream"])

    @patch("requests.Session.request")
    def test_oversized_stream_is_closed(self, request):
        response = request.return_value.__enter__.return_value
        response.iter_content.return_value = iter([b"123456", b"78901"])
        with patch("mars.youtube.MAX_RESPONSE_BYTES", 10):
            with BoundedSession() as session, self.assertRaises(ProcessingError) as caught:
                session.get("https://www.youtube.com")
        self.assertEqual(caught.exception.code, "TRANSCRIPT_TOO_LARGE")
        request.return_value.__exit__.assert_called_once()

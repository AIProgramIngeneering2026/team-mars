import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mars.dispatcher import Dispatcher
from mars.errors import ApiError, ProcessingError
from mars.inputs import video_id_from_url
from mars.service import JobService
from mars.storage import SQLiteJobs
from mars.tools import BriefSummaryTool, Registry, TimelineDigestTool
from mars.transcripts import FixtureTranscriptSource

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts.json"


class CountingSource:
    def __init__(self):
        self.calls = 0
        self.source = FixtureTranscriptSource(FIXTURES)

    def get(self, video_id):
        self.calls += 1
        return self.source.get(video_id)


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.jobs = SQLiteJobs(self.path)
        self.registry = Registry({"brief": BriefSummaryTool(), "timeline": TimelineDigestTool()})
        self.service = JobService(self.jobs, self.registry)
        self.source = CountingSource()
        self.dispatcher = Dispatcher(self.jobs, self.registry, self.source)

    def create(self, key="first", operation="brief", client="alice", url="https://youtu.be/abcdefghijk"):
        return self.service.create(client, key, {"url": url, "operation": operation})

    def assert_error(self, status, code, call):
        with self.assertRaises(ApiError) as caught:
            call()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_s1_both_modes(self):
        for operation in ("brief", "timeline"):
            with self.subTest(operation=operation):
                status, created = self.create(operation, operation)
                self.assertEqual((status, created["status"]), (202, "CREATED"))
                self.assertTrue(self.dispatcher.run_once())
                job = self.service.get("alice", created["id"])
                self.assertEqual(job["status"], "COMPLETED")
                if operation == "brief":
                    self.assertTrue(job["result"]["points"])
                    self.assertLessEqual(len(job["result"]["points"]), 5)
                else:
                    segments = job["result"]["segments"]
                    self.assertEqual([segment["startMs"] for segment in segments], [0, 60000, 120000])
                    self.assertTrue(all(segment["points"] for segment in segments))
        self.assertEqual(self.source.calls, 2)

    def test_s5_equivalent_url_uses_ready_result_without_tool(self):
        _, first = self.create()
        self.dispatcher.run_once()
        status, cached = self.create("second", url="https://www.youtube.com/watch?v=abcdefghijk&t=30s")
        self.assertEqual((status, cached["status"]), (200, "COMPLETED"))
        self.assertNotEqual(first["id"], cached["id"])
        self.assertEqual(
            self.service.get("alice", first["id"])["result"],
            self.service.get("alice", cached["id"])["result"],
        )
        self.assertFalse(self.dispatcher.run_once())
        self.assertEqual(self.source.calls, 1)

    def test_cache_miss_for_other_operation_and_other_client(self):
        self.create()
        self.dispatcher.run_once()
        self.assertEqual(self.create("timeline", "timeline")[0], 202)
        self.assertEqual(self.create(client="bob")[0], 202)

    def test_retry_replays_original_response_after_completion(self):
        initial = self.create()
        self.dispatcher.run_once()
        self.assertEqual(self.create(url="https://youtube.com/watch?v=abcdefghijk"), initial)
        cached = self.create("cached")
        self.assertEqual(cached[0], 200)
        self.assertEqual(self.create("cached"), cached)

    def test_same_key_different_mode_is_conflict(self):
        self.create()
        self.assert_error(409, "IDEMPOTENCY_CONFLICT", lambda: self.create(operation="timeline"))

    def test_parallel_identical_requests_create_one_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.create(), range(16)))
        self.assertEqual(len({reply[1]["id"] for reply in replies}), 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_data_and_idempotency_survive_reopening_storage(self):
        _, first = self.create()
        self.dispatcher.run_once()
        restarted = JobService(SQLiteJobs(self.path), self.registry)
        self.assertEqual(restarted.get("alice", first["id"])["status"], "COMPLETED")
        self.assertEqual(restarted.create("alice", "new", {"url": "https://youtu.be/abcdefghijk"})[0], 200)
        self.assertEqual(restarted.create("alice", "first", {"url": "https://youtu.be/abcdefghijk"})[1], first)

    def test_owner_is_required_for_read_and_cancel(self):
        _, created = self.create()
        for job_id in (created["id"], "missing"):
            self.assert_error(404, "JOB_NOT_FOUND", lambda: self.service.get("bob", job_id))
            self.assert_error(404, "JOB_NOT_FOUND", lambda: self.service.cancel("bob", job_id))

    def test_cancel_and_late_source_result(self):
        _, created = self.create()
        job, token = self.jobs.claim()
        self.assertEqual(job.status, "EXTRACTING_TRANSCRIPT")
        self.assertEqual(self.service.cancel("alice", job.id)["status"], "CANCELLED")
        self.assertFalse(self.jobs.transition(job.id, token, "ANALYZING"))
        self.assertEqual(self.service.cancel("alice", created["id"])["status"], "CANCELLED")
        self.assertNotIn("result", self.service.get("alice", job.id))

    def test_cancel_while_tool_is_running_discards_result_and_cache(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingTool:
            def run(self, transcript):
                entered.set()
                release.wait(3)
                return {"points": ["Поздний результат"]}

        registry = Registry({"brief": BlockingTool()})
        dispatcher = Dispatcher(self.jobs, registry, self.source)
        _, created = self.create()
        thread = threading.Thread(target=dispatcher.run_once)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.service.cancel("alice", created["id"])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.service.get("alice", created["id"])["status"], "CANCELLED")
        self.assertEqual(self.create("new")[0], 202)

    def test_cannot_cancel_completed_job(self):
        _, created = self.create()
        self.dispatcher.run_once()
        self.assert_error(409, "JOB_ALREADY_FINISHED", lambda: self.service.cancel("alice", created["id"]))

    def test_missing_transcript_fails_without_result_or_cache(self):
        _, created = self.create(url="https://youtu.be/lmnopqrstuv")
        self.dispatcher.run_once()
        job = self.service.get("alice", created["id"])
        self.assertEqual(job["error"]["code"], "TRANSCRIPT_UNAVAILABLE")
        self.assertEqual(job["status"], "FAILED")
        self.assertNotIn("result", job)
        self.assertEqual(self.create("retry", url="https://youtu.be/lmnopqrstuv")[0], 202)

    def test_expired_lease_is_failed_and_stale_worker_cannot_write(self):
        _, created = self.create()
        job, token = self.jobs.claim()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job.id,))
        SQLiteJobs(self.path).maintain((job.id, token))
        self.assertEqual(self.service.get("alice", created["id"])["error"]["code"], "PROCESSING_INTERRUPTED")
        self.assertFalse(self.jobs.transition(job.id, token, "ANALYZING"))

    def test_analysis_timeout_discards_late_completion(self):
        _, created = self.create()
        job, token = self.jobs.claim()
        self.jobs.transition(job.id, token, "ANALYZING")
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE jobs SET analysis_deadline=0 WHERE id=?", (job.id,))
        self.assertFalse(self.jobs.transition(job.id, token, "COMPLETED", result={"points": ["Поздно"]}))
        self.jobs.maintain((job.id, token))
        self.assertEqual(self.service.get("alice", created["id"])["error"]["code"], "ANALYSIS_TIMEOUT")

    def test_invalid_creation_does_not_persist_a_job(self):
        cases = [
            ({"url": "https://youtu.be/abcdefghijk", "operation": "translate"}, "UNSUPPORTED_OPERATION"),
            ({"operation": "brief"}, "INVALID_FIELD"),
            ({"url": "https://youtu.be/abcdefghijk", "operation": None}, "INVALID_FIELD"),
            ({"url": "https://youtu.be/abcdefghijk", "extra": 1}, "INVALID_FIELD"),
        ]
        for payload, code in cases:
            self.assert_error(422, code, lambda: self.service.create("alice", "bad", payload))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)


class URLAndTools(unittest.TestCase):
    def test_supported_url_forms(self):
        for url in (
            "https://youtu.be/abcdefghijk",
            "https://youtube.com/watch?v=abcdefghijk&t=30s",
            "https://www.youtube.com/watch?t=30s&v=abcdefghijk",
        ):
            self.assertEqual(video_id_from_url(url), "abcdefghijk")

    def test_invalid_urls(self):
        for url in (
            None, 123, "", "http://youtu.be/abcdefghijk",
            "https://youtube.com.evil/watch?v=abcdefghijk",
            "https://youtube.com/watch?v=abcdefghijk&v=abcdefghijk",
            "https://youtu.be/abcdefghijk/other",
            "https://user@youtube.com/watch?v=abcdefghijk",
            "https://youtube.com/watch", "https://youtu.be/short",
        ):
            with self.subTest(url=url), self.assertRaises(ApiError):
                video_id_from_url(url)

    def test_tools_reject_invalid_data(self):
        for tool in (BriefSummaryTool(), TimelineDigestTool()):
            for transcript in ([], [{"startMs": -1, "text": "x"}], [{"startMs": 0, "text": ""}],
                               [{"startMs": 5, "text": "x"}, {"startMs": 0, "text": "y"}]):
                with self.assertRaises(ProcessingError):
                    tool.run(transcript)

    def test_fixture_provider_rejects_malformed_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"abcdefghijk": [{"text": "Нет времени"}]}))
            with self.assertRaises(ProcessingError) as caught:
                FixtureTranscriptSource(path).get("abcdefghijk")
            self.assertEqual(caught.exception.code, "TRANSCRIPT_INVALID_RESPONSE")

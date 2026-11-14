import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mars.dispatcher import Dispatcher
from mars.http_api import make_server
from mars.service import JobService
from mars.storage import SQLiteJobs
from mars.tools import BriefSummaryTool, Registry, TimelineDigestTool
from mars.transcripts import FixtureTranscriptSource


class HTTPScenarios(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        jobs = SQLiteJobs(Path(self.directory.name) / "jobs.sqlite3")
        registry = Registry({"brief": BriefSummaryTool(), "timeline": TimelineDigestTool()})
        source = FixtureTranscriptSource(Path(__file__).parent / "fixtures" / "transcripts.json")
        self.dispatcher = Dispatcher(jobs, registry, source)
        self.server = make_server(("127.0.0.1", 0), JobService(jobs, registry), "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.dispatcher.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, payload=None, headers=None):
        request_headers = {
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
            "Idempotency-Key": "first",
        }
        request_headers.update(headers or {})
        raw = json.dumps(payload).encode() if payload is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, raw, request_headers)
            response = connection.getresponse()
            result = (response.status, dict(response.getheaders()), json.loads(response.read()))
        finally:
            connection.close()
        self.assertEqual(result[1]["Cache-Control"], "no-store")
        return result

    def test_create_read_and_s5_through_real_http_and_background_worker(self):
        payload = {"url": "https://youtu.be/abcdefghijk"}
        status, headers, body = self.request("POST", "/jobs", payload)
        self.assertEqual((status, body["status"]), (202, "CREATED"))
        location = headers["Location"]
        self.dispatcher.start()
        deadline = time.monotonic() + 3
        result = None
        while time.monotonic() < deadline:
            _, _, result = self.request("GET", location)
            if result["status"] == "COMPLETED":
                break
            threading.Event().wait(0.01)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(result["result"]["points"])
        cached_status, cached_headers, cached = self.request(
            "POST", "/jobs",
            {"url": "https://youtube.com/watch?v=abcdefghijk&t=30s"},
            {"Idempotency-Key": "second"},
        )
        self.assertEqual((cached_status, cached["status"]), (200, "COMPLETED"))
        self.assertNotEqual(cached["id"], body["id"])
        self.assertEqual(self.request("GET", cached_headers["Location"])[2]["result"], result["result"])
        self.assertEqual(self.request("POST", "/jobs", payload)[2], body)

    def test_authentication(self):
        status, headers, body = self.request("GET", "/jobs/missing", headers={"Authorization": ""})
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_field_error_and_unknown_operation(self):
        cases = [
            ({}, "INVALID_FIELD", "url"),
            ({"url": "https://youtu.be/abcdefghijk", "operation": "translate"}, "UNSUPPORTED_OPERATION", "operation"),
        ]
        for payload, code, field in cases:
            status, _, body = self.request("POST", "/jobs", payload)
            self.assertEqual(status, 422)
            self.assertEqual((body["error"]["code"], body["error"]["field"]), (code, field))

    def test_wrong_media_type_and_oversized_body(self):
        status, _, _ = self.request("POST", "/jobs", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, body = self.request("POST", "/jobs", {"url": "x" * 17000})
        self.assertEqual((status, body["error"]["code"]), (413, "REQUEST_TOO_LARGE"))

    def test_invalid_json(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("POST", "/jobs", "{", {
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["error"]["code"], "INVALID_JSON")
        finally:
            connection.close()

    def test_cancel_and_conflicting_key(self):
        payload = {"url": "https://youtu.be/abcdefghijk"}
        _, headers, _ = self.request("POST", "/jobs", payload)
        location = headers["Location"]
        status, _, cancelled = self.request("POST", location + "/cancel")
        self.assertEqual((status, cancelled["status"]), (200, "CANCELLED"))
        self.assertEqual(self.request("POST", location + "/cancel")[2], cancelled)
        status, _, body = self.request("POST", "/jobs", {**payload, "operation": "timeline"})
        self.assertEqual((status, body["error"]["code"]), (409, "IDEMPOTENCY_CONFLICT"))

    def test_missing_job(self):
        status, _, body = self.request("GET", "/jobs/missing")
        self.assertEqual((status, body["error"]["code"]), (404, "JOB_NOT_FOUND"))

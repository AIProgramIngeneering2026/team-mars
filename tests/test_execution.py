import time
import unittest

from mars.errors import ProcessingError
from mars.execution import ProcessToolRunner
from mars.tools import BriefSummaryTool


class HangingTool:
    def run(self, transcript):
        while True:
            time.sleep(1)


class FailedTool:
    def run(self, transcript):
        raise ProcessingError("INVALID_TRANSCRIPT", "Некорректный вход")


class ExecutionTests(unittest.TestCase):
    def test_hung_tool_is_stopped_and_next_call_succeeds(self):
        started = time.monotonic()
        with self.assertRaises(ProcessingError) as caught:
            ProcessToolRunner(timeout=0.2).run(HangingTool(), [])
        self.assertEqual(caught.exception.code, "ANALYSIS_TIMEOUT")
        self.assertLess(time.monotonic() - started, 5)
        result = ProcessToolRunner(timeout=3).run(
            BriefSummaryTool(), [{"startMs": 0, "text": "Следующее задание."}]
        )
        self.assertEqual(result, {"points": ["Следующее задание."]})

    def test_tool_error_is_transferred_to_dispatcher(self):
        with self.assertRaises(ProcessingError) as caught:
            ProcessToolRunner(timeout=3).run(FailedTool(), [])
        self.assertEqual(caught.exception.code, "INVALID_TRANSCRIPT")

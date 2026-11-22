"""Проверки Dispatcher без HTTP и SQLite (Практика 3)."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

from mars.dispatcher import Dispatcher
from mars.tools import BriefSummaryTool, Registry


def build_dispatcher(operation="brief", transcript=None, tools=None):
    """Заменяем хранилище и источник простыми объектами для проверки ядра."""
    jobs = Mock()
    jobs.claim.return_value = (
        SimpleNamespace(id="job-1", video_id="abcdefghijk", operation=operation),
        "lease-token",
    )
    jobs.transition.return_value = True

    source = Mock()
    source.get.return_value = (
        [{"startMs": 0, "text": "Тестовый текст."}]
        if transcript is None
        else transcript
    )
    registry = Registry(
        {"brief": BriefSummaryTool()} if tools is None else tools
    )
    return Dispatcher(jobs, registry, source), jobs, source


def transition_statuses(jobs):
    return [call.args[2] for call in jobs.transition.call_args_list]


def test_successful_tool_completes_job():
    dispatcher, jobs, _ = build_dispatcher()

    assert dispatcher.run_once() is True
    assert transition_statuses(jobs) == ["ANALYZING", "COMPLETED"]

    completed = jobs.transition.call_args_list[-1]
    assert completed.kwargs["result"]["points"]
    assert completed.kwargs["source_starts"] == {0}


def test_invalid_tool_input_logs_field_but_does_not_expose_it(caplog):
    dispatcher, jobs, _ = build_dispatcher(
        transcript=[{"startMs": 0, "text": "   "}]
    )

    with caplog.at_level(logging.WARNING, logger="mars.dispatcher"):
        assert dispatcher.run_once() is True

    assert transition_statuses(jobs) == ["ANALYZING", "FAILED"]
    assert "INVALID_TRANSCRIPT" in caplog.text
    assert "field=transcript[0].text" in caplog.text

    public_error = jobs.transition.call_args_list[-1].kwargs["error"]
    assert public_error == {
        "code": "ANALYSIS_FAILED",
        "message": "Не удалось выполнить анализ",
    }
    assert "field" not in public_error


def test_unknown_operation_fails_with_explicit_diagnostic(caplog):
    dispatcher, jobs, _ = build_dispatcher(operation="unknown")

    with caplog.at_level(logging.WARNING, logger="mars.dispatcher"):
        assert dispatcher.run_once() is True

    assert transition_statuses(jobs) == ["ANALYZING", "FAILED"]
    assert "UNKNOWN_OPERATION" in caplog.text
    assert "field=operation" in caplog.text
    assert jobs.transition.call_args_list[-1].kwargs["error"]["code"] == "ANALYSIS_FAILED"


def test_unexpected_tool_exception_fails_safely(caplog):
    class BrokenTool:
        def run(self, transcript):
            raise RuntimeError("internal-secret")

    dispatcher, jobs, _ = build_dispatcher(tools={"brief": BrokenTool()})

    with caplog.at_level(logging.ERROR, logger="mars.dispatcher"):
        assert dispatcher.run_once() is True

    assert transition_statuses(jobs) == ["ANALYZING", "FAILED"]
    public_error = jobs.transition.call_args_list[-1].kwargs["error"]
    assert public_error["code"] == "ANALYSIS_FAILED"
    assert "internal-secret" not in str(public_error)


def test_no_job_returns_false():
    dispatcher, jobs, source = build_dispatcher()
    jobs.claim.return_value = None

    assert dispatcher.run_once() is False
    jobs.transition.assert_not_called()
    source.get.assert_not_called()


def test_cancelled_job_is_not_sent_to_tool():
    class SpyTool:
        def __init__(self):
            self.run = Mock()

    tool = SpyTool()
    dispatcher, jobs, _ = build_dispatcher(tools={"brief": tool})
    jobs.transition.return_value = False  # переход в ANALYZING отклонён

    assert dispatcher.run_once() is True
    assert transition_statuses(jobs) == ["ANALYZING"]
    tool.run.assert_not_called()

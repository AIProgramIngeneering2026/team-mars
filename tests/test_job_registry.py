"""Правила Job и Registry: только стандартная библиотека, без HTTP и БД."""

import unittest

from mars.domain import Job
from mars.errors import (
    InvalidToolRegistration, JobStateError, JobTransitionError, UnknownOperation,
)
from mars.registry import Registry


class JobTests(unittest.TestCase):
    def created(self, operation="brief", job_id="job-1"):
        return Job(job_id, "alice", "abcdefghijk", operation, "CREATED")

    def analyzing(self, operation="brief"):
        return self.created(operation).transition("EXTRACTING_TRANSCRIPT").transition("ANALYZING")

    def assert_transition_rejected(self, job, target, **payload):
        before = job.public()
        with self.assertRaises(JobTransitionError) as caught:
            job.transition(target, **payload)
        self.assertEqual(caught.exception.code, "INVALID_JOB_TRANSITION")
        self.assertEqual(caught.exception.from_status, job.status)
        self.assertEqual(caught.exception.to_status, target)
        self.assertEqual(job.public(), before)

    def test_happy_path_brief(self):
        created = self.created()
        extracting = created.transition("EXTRACTING_TRANSCRIPT")
        analyzing = extracting.transition("ANALYZING")
        result = {"points": ["Основной тезис"]}
        completed = analyzing.transition("COMPLETED", result=result)
        self.assertEqual([job.status for job in (created, extracting, analyzing, completed)],
                         ["CREATED", "EXTRACTING_TRANSCRIPT", "ANALYZING", "COMPLETED"])
        self.assertEqual(completed.public(), {"id": created.id, "status": "COMPLETED", "result": result})
        self.assertEqual((completed.client, completed.video_id, completed.operation),
                         (created.client, created.video_id, created.operation))

    def test_happy_path_timeline(self):
        result = {"segments": [{"startMs": 60000, "points": ["Тезис отрезка"]}]}
        job = self.analyzing("timeline").transition("COMPLETED", result=result, source_starts={60000})
        self.assertEqual(job.public()["result"], result)

    def test_failure_from_each_processing_stage(self):
        extracting = self.created().transition("EXTRACTING_TRANSCRIPT")
        for job in (extracting, extracting.transition("ANALYZING")):
            with self.subTest(status=job.status):
                error = {"code": "PROCESSING_INTERRUPTED", "message": "Обработка прервана"}
                failed = job.transition("FAILED", error=error)
                self.assertEqual(failed.public(), {"id": job.id, "status": "FAILED", "error": error})

    def test_z1_start_an_already_running_job_again(self):
        extracting = self.created().transition("EXTRACTING_TRANSCRIPT")
        self.assert_transition_rejected(extracting, "EXTRACTING_TRANSCRIPT")
        self.assert_transition_rejected(extracting.transition("ANALYZING"), "EXTRACTING_TRANSCRIPT")

    def test_z2_late_result_after_interrupted_processing(self):
        # Обнаружение пропавшего исполнителя — обязанность хранения/Dispatcher.
        # Job применяет решение о сбое и запрещает запись старого результата.
        extracting = self.created().transition("EXTRACTING_TRANSCRIPT")
        for job in (extracting, extracting.transition("ANALYZING")):
            with self.subTest(status=job.status):
                failed = job.transition("FAILED", error={
                    "code": "PROCESSING_INTERRUPTED", "message": "Исполнитель потерян",
                })
                self.assert_transition_rejected(failed, "COMPLETED", result={"points": ["Поздно"]})

    def test_z3_late_result_after_cancellation(self):
        extracting = self.created().transition("EXTRACTING_TRANSCRIPT")
        for job in (self.created(), extracting, extracting.transition("ANALYZING")):
            with self.subTest(status=job.status):
                cancelled = job.transition("CANCELLED")
                self.assert_transition_rejected(cancelled, "COMPLETED", result={"points": ["Поздно"]})
                self.assert_transition_rejected(cancelled, "ANALYZING")
                self.assertNotIn("result", cancelled.public())

    def test_z4_late_success_after_tool_failure(self):
        failed = self.analyzing().transition("FAILED", error={
            "code": "ANALYSIS_FAILED", "message": "Не удалось выполнить анализ",
        })
        self.assert_transition_rejected(failed, "COMPLETED", result={"points": ["Поздно"]})
        self.assertEqual(failed.error["code"], "ANALYSIS_FAILED")

    def test_z5_cannot_complete_without_processing(self):
        created = self.created()
        for job in (created, created.transition("EXTRACTING_TRANSCRIPT")):
            with self.subTest(status=job.status):
                self.assert_transition_rejected(job, "COMPLETED", result={"points": ["Подмена"]})

    def test_z5_cannot_complete_without_a_valid_result(self):
        analyzing = self.analyzing()
        for result in (None, {}, {"points": []}, {"points": [" "]}, {"segments": []}):
            with self.subTest(result=result), self.assertRaises(JobStateError) as caught:
                analyzing.transition("COMPLETED", result=result)
            self.assertEqual(caught.exception.field, "result")
        self.assertEqual(analyzing.status, "ANALYZING")

    def test_z6_cannot_rerun_or_finish_a_completed_job(self):
        completed = self.analyzing().transition("COMPLETED", result={"points": ["Первый результат"]})
        for target in ("CREATED", "EXTRACTING_TRANSCRIPT", "ANALYZING", "COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(target=target):
                self.assert_transition_rejected(completed, target, result={"points": ["Подмена"]})

    def test_other_terminal_states_cannot_be_restarted(self):
        terminal_jobs = (
            self.created().transition("CANCELLED"),
            self.analyzing().transition("FAILED", error={"code": "ANALYSIS_FAILED", "message": "Сбой"}),
        )
        for job in terminal_jobs:
            for target in ("CREATED", "EXTRACTING_TRANSCRIPT", "ANALYZING", "FAILED", "CANCELLED"):
                with self.subTest(status=job.status, target=target):
                    self.assert_transition_rejected(job, target)

    def test_backward_transitions_and_repeated_analysis_are_rejected(self):
        extracting = self.created().transition("EXTRACTING_TRANSCRIPT")
        self.assert_transition_rejected(extracting, "CREATED")
        analyzing = extracting.transition("ANALYZING")
        for target in ("CREATED", "ANALYZING"):
            self.assert_transition_rejected(analyzing, target)

    def test_unknown_target_is_a_transition_error(self):
        for target in ("RUNNING", "", None, [], {}):
            with self.subTest(target=target):
                self.assert_transition_rejected(self.created(), target)

    def test_unknown_initial_status_is_a_state_error(self):
        for status in ("RUNNING", "", None, []):
            with self.subTest(status=status), self.assertRaises(JobStateError) as caught:
                Job("id", "alice", "abcdefghijk", "brief", status)
            self.assertEqual((caught.exception.code, caught.exception.field), ("INVALID_JOB_STATE", "status"))

    def test_invalid_initial_payload_is_rejected(self):
        for status, payload in (
            ("CREATED", {"result": {"points": ["Слишком рано"]}}),
            ("ANALYZING", {"error": {"code": "x", "message": "x"}}),
            ("COMPLETED", {}),
            ("COMPLETED", {"result": {"points": []}}),
            ("FAILED", {}),
            ("FAILED", {"error": {}}),
            ("FAILED", {"error": {"code": "x", "message": " "}}),
            ("FAILED", {"error": "not an object"}),
            ("CANCELLED", {"result": {"points": ["Лишний результат"]}}),
        ):
            with self.subTest(status=status, payload=payload), self.assertRaises(JobStateError):
                Job("id", "alice", "abcdefghijk", "brief", status, **payload)

    def test_failure_requires_structured_error(self):
        for error in (None, {}, {"code": "x"}, {"code": 1, "message": "Сбой"}):
            with self.subTest(error=error), self.assertRaises(JobStateError) as caught:
                self.analyzing().transition("FAILED", error=error)
            self.assertEqual(caught.exception.field, "error")

    def test_nonterminal_transition_cannot_carry_result_or_error(self):
        for payload in ({"result": {"points": ["Подмена"]}},
                        {"error": {"code": "x", "message": "Сбой"}}):
            with self.subTest(payload=payload), self.assertRaises(JobStateError):
                self.created().transition("EXTRACTING_TRANSCRIPT", **payload)

    def test_timeline_timestamp_must_exist_in_source(self):
        result = {"segments": [{"startMs": 1, "points": ["Чужое время"]}]}
        with self.assertRaises(JobStateError) as caught:
            self.analyzing("timeline").transition("COMPLETED", result=result, source_starts={0})
        self.assertEqual(caught.exception.field, "result")

    def test_new_completed_job_is_allowed_for_s5(self):
        result = {"points": ["Сохранённый результат"]}
        cached = Job("cached-id", "alice", "abcdefghijk", "brief", "COMPLETED", result=result)
        self.assertEqual(cached.public(), {"id": "cached-id", "status": "COMPLETED", "result": result})
        self.assert_transition_rejected(self.created(), "COMPLETED", result=result)

    def test_new_request_has_an_independent_job(self):
        completed = self.analyzing().transition("COMPLETED", result={"points": ["Первый результат"]})
        new_job = self.created(job_id="job-2").transition("EXTRACTING_TRANSCRIPT")
        self.assertNotEqual(new_job.id, completed.id)
        self.assertEqual(completed.status, "COMPLETED")


class StubTool:
    def run(self, transcript):
        return {"points": ["Тестовый результат"]}


class RegistryTests(unittest.TestCase):
    def test_returns_the_registered_instance_for_each_name(self):
        brief, timeline = StubTool(), StubTool()
        registry = Registry({"brief": brief, "timeline": timeline})
        self.assertIs(registry.get("brief"), brief)
        self.assertIs(registry.get("timeline"), timeline)
        self.assertEqual(registry.operations(), ("brief", "timeline"))

    def test_new_tool_name_does_not_require_registry_changes(self):
        tool = StubTool()
        self.assertIs(Registry({"third-operation": tool}).get("third-operation"), tool)

    def test_unknown_operation_has_an_explicit_code(self):
        registry = Registry({"brief": StubTool()})
        for operation in ("missing", "BRIEF", "", None, [], {}):
            with self.subTest(operation=operation), self.assertRaises(UnknownOperation) as caught:
                registry.get(operation)
            self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")
            self.assertEqual(caught.exception.operation, operation)
            self.assertIn("Неизвестная операция", str(caught.exception))

    def test_empty_registry_rejects_unknown_operation(self):
        registry = Registry({})
        self.assertEqual(registry.operations(), ())
        with self.assertRaises(UnknownOperation):
            registry.get("brief")

    def test_duplicate_name_is_not_silently_overwritten(self):
        with self.assertRaises(InvalidToolRegistration) as caught:
            Registry([("brief", StubTool()), ("brief", StubTool())])
        self.assertEqual(caught.exception.code, "INVALID_TOOL_REGISTRATION")
        self.assertEqual(caught.exception.operation, "brief")

    def test_invalid_registration_is_rejected_at_assembly(self):
        class NonCallableRun:
            run = "not callable"

        for operation, tool in (("", StubTool()), (" ", StubTool()), (None, StubTool()),
                                ("brief", object()), ("brief", NonCallableRun())):
            with self.subTest(operation=operation, tool=tool), self.assertRaises(InvalidToolRegistration):
                Registry([(operation, tool)])

    def test_registration_is_independent_of_the_original_mapping(self):
        original_tool = StubTool()
        tools = {"brief": original_tool}
        registry = Registry(tools)
        tools["brief"] = StubTool()
        tools["new"] = StubTool()
        self.assertIs(registry.get("brief"), original_tool)
        self.assertEqual(registry.operations(), ("brief",))

    def test_previous_import_path_remains_supported(self):
        from mars.tools import Registry as PreviousRegistry
        self.assertIs(PreviousRegistry, Registry)

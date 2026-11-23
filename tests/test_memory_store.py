"""Тесты хранения Job в памяти: интерфейс JobStore и реализация InMemoryJobs.

Не требуют сети, файлов и дочерних процессов. Время подменяется часами FakeClock.
"""
import dataclasses
import inspect
import sys
import threading
import unittest

from mars.errors import ApiError
from mars.job_store import ANALYSIS_SECONDS, LEASE_SECONDS, JobStore
from mars.memory import InMemoryJobs

VIDEO = "abcdefghijk"
FAILURE = {"code": "ANALYSIS_FAILED", "message": "Не удалось выполнить анализ"}
BRIEF = {"points": ["Тезис"]}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = InMemoryJobs(clock=self.clock)

    def create(self, key="k1", client="alice", video=VIDEO, operation="brief"):
        return self.store.create(client, key, video, operation)

    def create_id(self, *args, **kwargs):
        return self.create(*args, **kwargs)[1]["id"]

    def claim_analyzing(self, key="k1", **kwargs):
        """Создаёт Job и доводит её до ANALYZING. Возвращает (job_id, token)."""
        job_id = self.create_id(key, **kwargs)
        job, token = self.store.claim()
        self.assertEqual(job.id, job_id)
        self.assertTrue(self.store.transition(job_id, token, "ANALYZING"))
        return job_id, token

    def status(self, job_id, client="alice"):
        return self.store.get(client, job_id).status

    def assertApiError(self, call, status, code):
        with self.assertRaises(ApiError) as caught:
            call()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))


class InterfaceTests(unittest.TestCase):
    METHODS = ("create", "get", "cancel", "claim", "transition", "maintain")

    def test_protocol_declares_the_expected_methods(self):
        declared = sorted(name for name in vars(JobStore) if not name.startswith("_"))
        self.assertEqual(declared, sorted(self.METHODS))

    def test_in_memory_jobs_implements_every_method_with_same_parameters(self):
        for name in self.METHODS:
            with self.subTest(method=name):
                expected = list(inspect.signature(getattr(JobStore, name)).parameters)
                actual = list(inspect.signature(getattr(InMemoryJobs, name)).parameters)
                self.assertEqual(actual, expected)

    def test_processing_deadlines_are_positive(self):
        self.assertGreater(LEASE_SECONDS, 0)
        self.assertGreater(ANALYSIS_SECONDS, 0)


class CreateTests(StoreTestCase):
    def test_new_job_is_created_for_its_owner(self):
        status, body = self.create(operation="timeline")
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "CREATED")
        job = self.store.get("alice", body["id"])
        self.assertEqual((job.client, job.video_id, job.operation, job.status, job.result, job.error),
                         ("alice", VIDEO, "timeline", "CREATED", None, None))

    def test_new_keys_create_distinct_jobs(self):
        ids = {self.create_id("k%d" % number) for number in range(5)}
        self.assertEqual(len(ids), 5)

    def test_repeated_key_returns_original_response_without_new_job(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        self.assertEqual(self.store.claim()[0].id, first[1]["id"])
        self.assertIsNone(self.store.claim())

    def test_repeated_key_keeps_original_status_after_progress(self):
        status, body = self.create()
        job, token = self.store.claim()
        self.store.transition(job.id, token, "ANALYZING")
        self.assertEqual(self.create(), (status, body))
        self.assertEqual(body["status"], "CREATED")

    def test_same_key_with_other_video_or_operation_conflicts(self):
        self.create()
        for kwargs in ({"operation": "timeline"}, {"video": "bbbbbbbbbbb"}):
            with self.subTest(**kwargs):
                self.assertApiError(lambda: self.create(**kwargs), 409, "IDEMPOTENCY_CONFLICT")

    def test_conflicting_request_creates_no_job(self):
        self.create()
        with self.assertRaises(ApiError):
            self.create(operation="timeline")
        self.assertIsNotNone(self.store.claim())
        self.assertIsNone(self.store.claim())

    def test_same_key_of_different_clients_is_independent(self):
        alice, bob = self.create_id(client="alice"), self.create_id(client="bob")
        self.assertNotEqual(alice, bob)
        self.assertEqual(self.status(bob, "bob"), "CREATED")


class GetTests(StoreTestCase):
    def test_job_is_visible_only_to_its_owner(self):
        job_id = self.create_id()
        self.assertEqual(self.store.get("alice", job_id).id, job_id)
        self.assertApiError(lambda: self.store.get("bob", job_id), 404, "JOB_NOT_FOUND")

    def test_missing_job_is_not_found(self):
        self.assertApiError(lambda: self.store.get("alice", "missing"), 404, "JOB_NOT_FOUND")

    def test_returned_job_cannot_be_modified_in_place(self):
        job = self.store.get("alice", self.create_id())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            job.status = "COMPLETED"
        self.assertEqual(self.status(job.id), "CREATED")


class CancelTests(StoreTestCase):
    def test_cancel_created_job_and_repeat_is_safe(self):
        job_id = self.create_id()
        self.assertEqual(self.store.cancel("alice", job_id), {"id": job_id, "status": "CANCELLED"})
        self.assertEqual(self.store.cancel("alice", job_id), {"id": job_id, "status": "CANCELLED"})

    def test_cancel_active_job_in_each_active_state(self):
        extracting = self.create_id("k1")
        self.store.claim()
        analyzing, _ = self.claim_analyzing("k2")
        for job_id in (extracting, analyzing):
            self.assertEqual(self.store.cancel("alice", job_id)["status"], "CANCELLED")
            self.assertEqual(self.status(job_id), "CANCELLED")

    def test_finished_job_cannot_be_cancelled(self):
        completed, token = self.claim_analyzing("k1")
        self.store.transition(completed, token, "COMPLETED", result=BRIEF)
        self.create("k2")
        job, token = self.store.claim()
        self.store.transition(job.id, token, "FAILED", error=FAILURE)
        for job_id, expected in ((completed, "COMPLETED"), (job.id, "FAILED")):
            self.assertApiError(lambda: self.store.cancel("alice", job_id), 409, "JOB_ALREADY_FINISHED")
            self.assertEqual(self.status(job_id), expected)

    def test_only_the_owner_can_cancel(self):
        job_id = self.create_id()
        self.assertApiError(lambda: self.store.cancel("bob", job_id), 404, "JOB_NOT_FOUND")
        self.assertApiError(lambda: self.store.cancel("alice", "missing"), 404, "JOB_NOT_FOUND")
        self.assertEqual(self.status(job_id), "CREATED")

    def test_cancelled_job_is_not_claimed(self):
        self.store.cancel("alice", self.create_id())
        self.assertIsNone(self.store.claim())


class ClaimTests(StoreTestCase):
    def test_nothing_to_claim_in_empty_store(self):
        self.assertIsNone(self.store.claim())

    def test_claim_moves_job_to_extracting_and_returns_token(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.assertEqual((job.id, job.status), (job_id, "EXTRACTING_TRANSCRIPT"))
        self.assertTrue(token)
        self.assertEqual(self.status(job_id), "EXTRACTING_TRANSCRIPT")

    def test_jobs_are_claimed_oldest_first_and_only_once(self):
        ids = [self.create_id("k%d" % number) for number in range(3)]
        claimed = [self.store.claim()[0].id for _ in ids]
        self.assertEqual(claimed, ids)
        self.assertIsNone(self.store.claim())

    def test_each_claim_has_its_own_token(self):
        self.create("k1")
        self.create("k2")
        self.assertNotEqual(self.store.claim()[1], self.store.claim()[1])


class TransitionTests(StoreTestCase):
    def test_full_path_to_completed_with_result(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.assertTrue(self.store.transition(job_id, token, "ANALYZING"))
        self.assertTrue(self.store.transition(job_id, token, "COMPLETED", result=BRIEF))
        job = self.store.get("alice", job_id)
        self.assertEqual((job.status, job.result, job.error), ("COMPLETED", BRIEF, None))

    def test_failed_job_keeps_error_and_no_result(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.assertTrue(self.store.transition(job_id, token, "FAILED", error=FAILURE))
        job = self.store.get("alice", job_id)
        self.assertEqual((job.status, job.result, job.error), ("FAILED", None, FAILURE))

    def test_timeline_result_is_checked_against_source_timestamps(self):
        job_id, token = self.claim_analyzing(operation="timeline")
        result = {"segments": [{"startMs": 5000, "points": ["Тезис"]}]}
        with self.assertRaises(ValueError):
            self.store.transition(job_id, token, "COMPLETED", result=result, source_starts={0, 60000})
        self.assertTrue(self.store.transition(job_id, token, "COMPLETED", result=result, source_starts={5000}))

    def test_wrong_or_unknown_token_is_rejected(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.assertFalse(self.store.transition(job_id, "чужой-токен", "ANALYZING"))
        self.assertFalse(self.store.transition("missing", token, "ANALYZING"))
        self.assertEqual(self.status(job_id), "EXTRACTING_TRANSCRIPT")

    def test_token_of_other_job_is_rejected(self):
        self.create("k1")
        self.create("k2")
        (first, _), (second, second_token) = self.store.claim(), self.store.claim()
        self.assertFalse(self.store.transition(first.id, second_token, "ANALYZING"))

    def test_forbidden_transition_raises_and_keeps_state(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        for status, kwargs in (("COMPLETED", {"result": BRIEF}), ("CREATED", {}), ("DONE", {})):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.store.transition(job_id, token, status, **kwargs)
                self.assertEqual(self.status(job_id), "EXTRACTING_TRANSCRIPT")

    def test_invalid_result_or_error_is_rejected(self):
        job_id, token = self.claim_analyzing()
        for kwargs in ({"result": {"points": []}}, {"result": {"segments": []}}, {"result": None}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.store.transition(job_id, token, "COMPLETED", **kwargs)
        with self.assertRaises(ValueError):
            self.store.transition(job_id, token, "FAILED")
        self.assertEqual(self.status(job_id), "ANALYZING")

    def test_transition_of_cancelled_or_finished_job_is_discarded(self):
        job_id, token = self.claim_analyzing()
        self.store.cancel("alice", job_id)
        self.assertFalse(self.store.transition(job_id, token, "COMPLETED", result=BRIEF))
        self.assertEqual(self.store.get("alice", job_id).result, None)
        done, done_token = self.claim_analyzing("k2")
        self.store.transition(done, done_token, "COMPLETED", result=BRIEF)
        self.assertFalse(self.store.transition(done, done_token, "FAILED", error=FAILURE))
        self.assertEqual(self.status(done), "COMPLETED")


class LeaseTests(StoreTestCase):
    def failure_code(self, job_id):
        job = self.store.get("alice", job_id)
        self.assertEqual(job.status, "FAILED")
        return job.error["code"]

    def test_expired_lease_blocks_transition_and_interrupts_job(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.clock.advance(LEASE_SECONDS + 1)
        self.assertFalse(self.store.transition(job_id, token, "ANALYZING"))
        self.store.maintain()
        self.assertEqual(self.failure_code(job_id), "PROCESSING_INTERRUPTED")

    def test_job_within_lease_is_left_alone(self):
        job_id = self.create_id()
        self.store.claim()
        self.clock.advance(LEASE_SECONDS - 1)
        self.store.maintain()
        self.assertEqual(self.status(job_id), "EXTRACTING_TRANSCRIPT")

    def test_current_claim_extends_its_lease(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        for _ in range(3):
            self.clock.advance(LEASE_SECONDS - 1)
            self.store.maintain((job_id, token))
        self.assertEqual(self.status(job_id), "EXTRACTING_TRANSCRIPT")
        self.assertTrue(self.store.transition(job_id, token, "ANALYZING"))

    def test_wrong_token_does_not_extend_the_lease(self):
        job_id = self.create_id()
        self.store.claim()
        self.clock.advance(LEASE_SECONDS - 1)
        self.store.maintain((job_id, "чужой-токен"))
        self.clock.advance(2)
        self.store.maintain()
        self.assertEqual(self.failure_code(job_id), "PROCESSING_INTERRUPTED")

    def test_expired_lease_cannot_be_revived_by_maintain(self):
        job_id = self.create_id()
        job, token = self.store.claim()
        self.clock.advance(LEASE_SECONDS + 1)
        self.store.maintain((job_id, token))
        self.assertEqual(self.failure_code(job_id), "PROCESSING_INTERRUPTED")

    def test_maintain_ignores_created_and_finished_jobs(self):
        done, token = self.claim_analyzing("k1")
        self.store.transition(done, token, "COMPLETED", result=BRIEF)
        waiting = self.create_id("k2")
        cancelled = self.create_id("k3")
        self.store.cancel("alice", cancelled)
        self.clock.advance(ANALYSIS_SECONDS + LEASE_SECONDS + 1)
        self.store.maintain()
        self.assertEqual([self.status(waiting), self.status(done), self.status(cancelled)],
                         ["CREATED", "COMPLETED", "CANCELLED"])


class AnalysisDeadlineTests(StoreTestCase):
    def test_late_result_is_discarded_and_job_times_out(self):
        job_id, token = self.claim_analyzing()
        self.clock.advance(ANALYSIS_SECONDS + 1)
        self.assertFalse(self.store.transition(job_id, token, "COMPLETED", result=BRIEF))
        self.store.maintain()
        job = self.store.get("alice", job_id)
        self.assertEqual((job.status, job.error["code"], job.result), ("FAILED", "ANALYSIS_TIMEOUT", None))

    def test_late_result_is_discarded_even_with_valid_lease(self):
        job_id, token = self.claim_analyzing()
        self.clock.advance(ANALYSIS_SECONDS // 2)
        self.store.maintain((job_id, token))
        self.clock.advance(ANALYSIS_SECONDS // 2 + 1)
        self.assertFalse(self.store.transition(job_id, token, "COMPLETED", result=BRIEF))
        self.assertEqual(self.status(job_id), "ANALYZING")

    def test_deadline_applies_even_when_lease_is_renewed(self):
        job_id, token = self.claim_analyzing()
        step = ANALYSIS_SECONDS // 2
        self.assertLess(step, LEASE_SECONDS)
        self.clock.advance(step)
        self.store.maintain((job_id, token))
        self.assertEqual(self.status(job_id), "ANALYZING")
        self.clock.advance(step)
        self.store.maintain((job_id, token))
        self.assertEqual(self.store.get("alice", job_id).error["code"], "ANALYSIS_TIMEOUT")

    def test_result_within_deadline_is_accepted(self):
        job_id, token = self.claim_analyzing()
        self.clock.advance(ANALYSIS_SECONDS - 1)
        self.store.maintain((job_id, token))
        self.assertTrue(self.store.transition(job_id, token, "COMPLETED", result=BRIEF))


class ConcurrencyTests(unittest.TestCase):
    THREADS = 16
    ROUNDS = 20

    def setUp(self):
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)

    def run_threads(self, target):
        barrier = threading.Barrier(self.THREADS)
        results, errors = [], []

        def worker(number):
            barrier.wait()
            try:
                results.append(target(number))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        return results

    def test_parallel_requests_with_one_key_create_one_job(self):
        for _ in range(self.ROUNDS):
            store = InMemoryJobs()
            results = self.run_threads(lambda _: store.create("alice", "same", VIDEO, "brief"))
            self.assertEqual(len({body["id"] for _, body in results}), 1)
            self.assertIsNotNone(store.claim())
            self.assertIsNone(store.claim())

    def test_parallel_workers_never_claim_the_same_job(self):
        for _ in range(self.ROUNDS):
            store = InMemoryJobs()
            for number in range(8):
                store.create("alice", "k%d" % number, VIDEO, "brief")
            results = self.run_threads(lambda _: store.claim())
            claimed = [claim[0].id for claim in results if claim is not None]
            self.assertEqual(len(claimed), 8)
            self.assertEqual(len(set(claimed)), 8)


if __name__ == "__main__":
    unittest.main()
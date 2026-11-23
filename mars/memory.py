"""Хранение Job в памяти процесса. Подходит для тестов и демонстрации.

Поведение совпадает с `SQLiteJobs`, но данные теряются при остановке процесса.
"""
import itertools
import threading
import time
import uuid

from .domain import ACTIVE, Job
from .errors import ApiError
from .job_store import ANALYSIS_SECONDS, LEASE_SECONDS


class InMemoryJobs:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._order = itertools.count()
        self._records = {}
        self._keys = {}

    def _find_owned(self, client, job_id):
        record = self._records.get(job_id)
        if record is None or record["client"] != client:
            raise ApiError(404, "JOB_NOT_FOUND", "Задание не найдено")
        return record

    def create(self, client, key, video_id, operation):
        with self._lock:
            existing = self._keys.get((client, key))
            if existing is not None:
                record = self._records[existing]
                if (record["job"].video_id, record["job"].operation) != (video_id, operation):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Ключ уже использован с другими параметрами")
                return 202, {"id": record["job"].id, "status": record["initial_status"]}
            job = Job(id=str(uuid.uuid4()), client=client, video_id=video_id,
                      operation=operation, status="CREATED")
            self._records[job.id] = {
                "job": job, "client": client, "initial_status": "CREATED",
                "order": next(self._order), "token": None,
                "lease_until": None, "analysis_deadline": None,
            }
            self._keys[(client, key)] = job.id
            return 202, {"id": job.id, "status": "CREATED"}

    def get(self, client, job_id):
        with self._lock:
            return self._find_owned(client, job_id)["job"]

    def cancel(self, client, job_id):
        with self._lock:
            record = self._find_owned(client, job_id)
            job = record["job"]
            if job.status in ("COMPLETED", "FAILED"):
                raise ApiError(409, "JOB_ALREADY_FINISHED", "Задание уже завершено")
            if job.status != "CANCELLED":
                job = record["job"] = job.transition("CANCELLED")
            return {"id": job.id, "status": job.status}

    def claim(self):
        with self._lock:
            waiting = [r for r in self._records.values() if r["job"].status == "CREATED"]
            if not waiting:
                return None
            record = min(waiting, key=lambda r: r["order"])
            record["job"] = record["job"].transition("EXTRACTING_TRANSCRIPT")
            record["token"] = str(uuid.uuid4())
            record["lease_until"] = self._clock() + LEASE_SECONDS
            return record["job"], record["token"]

    def transition(self, job_id, token, status, result=None, error=None, source_starts=None):
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record["job"].status not in ACTIVE:
                return False
            now = self._clock()
            if record["token"] != token or record["lease_until"] <= now:
                return False
            if record["analysis_deadline"] is not None and record["analysis_deadline"] <= now:
                return False
            record["job"] = record["job"].transition(status, result, error, source_starts)
            if status == "ANALYZING":
                record["analysis_deadline"] = now + ANALYSIS_SECONDS
            return True

    def maintain(self, current_claim=None):
        with self._lock:
            now = self._clock()
            for record in self._records.values():
                if record["job"].status not in ("EXTRACTING_TRANSCRIPT", "ANALYZING"):
                    continue
                code = None
                if record["analysis_deadline"] is not None and record["analysis_deadline"] <= now:
                    code = "ANALYSIS_TIMEOUT"
                elif record["lease_until"] <= now:
                    code = "PROCESSING_INTERRUPTED"
                if code is not None:
                    record["job"] = record["job"].transition(
                        "FAILED", error={"code": code, "message": "Обработка прервана или превысила срок"})
            if current_claim is not None:
                job_id, token = current_claim
                record = self._records.get(job_id)
                if (record is not None and record["token"] == token
                        and record["lease_until"] > now
                        and record["job"].status in ("EXTRACTING_TRANSCRIPT", "ANALYZING")):
                    record["lease_until"] = now + LEASE_SECONDS
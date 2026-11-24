import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import ACTIVE, Job
from .errors import ApiError

from .job_store import LEASE_SECONDS, ANALYSIS_SECONDS



class SQLiteJobs:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._transaction() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    client TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    initial_status TEXT NOT NULL,
                    response_code INTEGER NOT NULL,
                    claim_token TEXT,
                    lease_until REAL,
                    analysis_deadline REAL,
                    created_at REAL NOT NULL,
                    UNIQUE(client, request_key)
                )
            """)

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _job(row):
        return Job(
            id=row["id"], client=row["client"], video_id=row["video_id"],
            operation=row["operation"], status=row["status"],
            result=json.loads(row["result"]) if row["result"] is not None else None,
            error=json.loads(row["error"]) if row["error"] is not None else None,
        )

    @staticmethod
    def _save(connection, job):
        connection.execute(
            "UPDATE jobs SET status=?, result=?, error=? WHERE id=?",
            (
                job.status,
                json.dumps(job.result, ensure_ascii=False) if job.result is not None else None,
                json.dumps(job.error, ensure_ascii=False) if job.error is not None else None,
                job.id,
            ),
        )

    @staticmethod
    def _find_owned(connection, client, job_id):
        row = connection.execute(
            "SELECT * FROM jobs WHERE id=? AND client=?", (job_id, client)
        ).fetchone()
        if row is None:
            raise ApiError(404, "JOB_NOT_FOUND", "Задание не найдено")
        return row

    def create(self, client, key, video_id, operation):
        # Проверка повторного ключа и создание Job выполняются в одной транзакции.
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE client=? AND request_key=?", (client, key)
            ).fetchone()
            if row is not None:
                if (row["video_id"], row["operation"]) != (video_id, operation):
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Ключ уже использован с другими параметрами")
                return row["response_code"], {"id": row["id"], "status": row["initial_status"]}

            status = "CREATED"
            response_code = 202
            result = None
            job_id = str(uuid.uuid4())
            connection.execute("""
                INSERT INTO jobs (
                    id, client, request_key, video_id, operation, status,
                    result, initial_status, response_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, client, key, video_id, operation, status,
                result, status, response_code, time.time(),
            ))
            return response_code, {"id": job_id, "status": status}

    def get(self, client, job_id):
        with self._transaction() as connection:
            return self._job(self._find_owned(connection, client, job_id))

    def cancel(self, client, job_id):
        with self._transaction() as connection:
            job = self._job(self._find_owned(connection, client, job_id))
            if job.status in ("COMPLETED", "FAILED"):
                raise ApiError(409, "JOB_ALREADY_FINISHED", "Задание уже завершено")
            if job.status != "CANCELLED":
                job = job.transition("CANCELLED")
                self._save(connection, job)
            return {"id": job.id, "status": job.status}

    def claim(self):
        with self._transaction() as connection:
            row = connection.execute("""
                SELECT * FROM jobs WHERE status='CREATED'
                ORDER BY created_at, id LIMIT 1
            """).fetchone()
            if row is None:
                return None
            job = self._job(row).transition("EXTRACTING_TRANSCRIPT")
            token = str(uuid.uuid4())
            self._save(connection, job)
            connection.execute(
                "UPDATE jobs SET claim_token=?, lease_until=? WHERE id=?",
                (token, time.time() + LEASE_SECONDS, job.id),
            )
            return job, token

    def transition(self, job_id, token, status, result=None, error=None, source_starts=None):
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                return False
            now = time.time()
            if row["claim_token"] != token or row["lease_until"] <= now:
                return False
            if row["analysis_deadline"] is not None and row["analysis_deadline"] <= now:
                return False
            job = self._job(row).transition(status, result, error, source_starts)
            self._save(connection, job)
            if status == "ANALYZING":
                connection.execute(
                    "UPDATE jobs SET analysis_deadline=? WHERE id=?",
                    (now + ANALYSIS_SECONDS, job_id),
                )
            return True

    def maintain(self, current_claim=None):
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute("""
                SELECT * FROM jobs WHERE status IN ('EXTRACTING_TRANSCRIPT', 'ANALYZING')
            """).fetchall()
            for row in rows:
                code = None
                if row["analysis_deadline"] is not None and row["analysis_deadline"] <= now:
                    code = "ANALYSIS_TIMEOUT"
                elif row["lease_until"] <= now:
                    code = "PROCESSING_INTERRUPTED"
                if code is not None:
                    job = self._job(row).transition(
                        "FAILED", error={"code": code, "message": "Обработка прервана или превысила срок"}
                    )
                    self._save(connection, job)
            if current_claim is not None:
                job_id, token = current_claim
                connection.execute("""
                    UPDATE jobs SET lease_until=?
                    WHERE id=? AND claim_token=? AND lease_until>?
                        AND status IN ('EXTRACTING_TRANSCRIPT', 'ANALYZING')
                """, (now + LEASE_SECONDS, job_id, token, now))

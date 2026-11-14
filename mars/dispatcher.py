import logging
import threading

from .errors import ProcessingError

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, jobs, registry, source):
        self.jobs = jobs
        self.registry = registry
        self.source = source
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current = None
        self._threads = []

    def run_once(self):
        claimed = self.jobs.claim()
        if claimed is None:
            return False
        job, token = claimed
        with self._lock:
            self._current = (job.id, token)
        try:
            transcript = self.source.get(job.video_id)
            if not self.jobs.transition(job.id, token, "ANALYZING"):
                return True
            result = self.registry.get(job.operation).run(transcript)
            self.jobs.transition(job.id, token, "COMPLETED", result=result)
        except ProcessingError as error:
            code = error.code
            if code == "INVALID_TRANSCRIPT":
                code = "ANALYSIS_FAILED"
            self.jobs.transition(job.id, token, "FAILED", error={"code": code, "message": str(error)})
        except Exception:
            logger.exception("Ошибка обработки Job %s", job.id)
            self.jobs.transition(
                job.id, token, "FAILED",
                error={"code": "ANALYSIS_FAILED", "message": "Не удалось выполнить анализ"},
            )
        finally:
            with self._lock:
                self._current = None
        return True

    def _work(self):
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(0.1)
            except Exception:
                logger.exception("Не удалось получить или завершить Job")
                self._stop.wait(1)

    def _maintain(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    current = self._current
                self.jobs.maintain(current)
            except Exception:
                logger.exception("Не удалось проверить сроки обработки")
            self._stop.wait(15)

    def start(self):
        self.jobs.maintain()
        self._threads = [
            threading.Thread(target=self._work, daemon=True),
            threading.Thread(target=self._maintain, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

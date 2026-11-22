from dataclasses import dataclass, replace
from typing import Optional

from .errors import JobStateError, JobTransitionError

ACTIVE = ("CREATED", "EXTRACTING_TRANSCRIPT", "ANALYZING")
TRANSITIONS = {
    "CREATED": {"EXTRACTING_TRANSCRIPT", "CANCELLED"},
    "EXTRACTING_TRANSCRIPT": {"ANALYZING", "FAILED", "CANCELLED"},
    "ANALYZING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}


def validate_result(operation, result, source_starts=None):
    if not isinstance(result, dict):
        raise ValueError("Результат должен быть объектом")

    def valid_points(points):
        return (isinstance(points, list) and bool(points)
                and all(isinstance(point, str) and bool(point.strip()) for point in points))

    if operation == "brief":
        if set(result) != {"points"} or not valid_points(result["points"]):
            raise ValueError("Некорректный BriefResult")
        return
    if operation != "timeline" or set(result) != {"segments"}:
        raise ValueError("Результат не соответствует режиму")
    segments = result["segments"]
    if not isinstance(segments, list) or not segments:
        raise ValueError("Таймлайн должен содержать отрезки")
    previous = -1
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"startMs", "points"}:
            raise ValueError("Некорректный отрезок")
        start = segment["startMs"]
        if type(start) is not int or start < 0 or start <= previous:
            raise ValueError("Некорректный порядок таймкодов")
        if source_starts is not None and start not in source_starts:
            raise ValueError("Таймкод отсутствует во входных субтитрах")
        if not valid_points(segment["points"]):
            raise ValueError("Отрезок должен содержать тезисы")
        previous = start


@dataclass(frozen=True)
class Job:
    id: str
    client: str
    video_id: str
    operation: str
    status: str
    result: Optional[dict] = None
    error: Optional[dict] = None

    def __post_init__(self):
        # Проверяем также снимки, восстановленные хранилищем, и новые Job S5.
        if not isinstance(self.status, str) or self.status not in TRANSITIONS:
            raise JobStateError("status", f"Неизвестное состояние Job: {self.status!r}")
        if (self.status == "COMPLETED") != (self.result is not None):
            raise JobStateError("result", "Результат обязателен только при COMPLETED")
        if (self.status == "FAILED") != (self.error is not None):
            raise JobStateError("error", "Ошибка обязательна только при FAILED")
        if self.status == "FAILED":
            if (not isinstance(self.error, dict)
                    or any(not isinstance(self.error.get(key), str) or not self.error[key].strip()
                           for key in ("code", "message"))):
                raise JobStateError("error", "Ошибка должна содержать непустые code и message")
        if self.status == "COMPLETED":
            self._validate_result()

    def _validate_result(self, source_starts=None):
        try:
            validate_result(self.operation, self.result, source_starts)
        except ValueError as error:
            raise JobStateError("result", str(error)) from error

    def transition(self, status, result=None, error=None, source_starts=None):
        if not isinstance(status, str) or status not in TRANSITIONS[self.status]:
            raise JobTransitionError(self.status, status)
        updated = replace(self, status=status, result=result, error=error)
        if status == "COMPLETED" and source_starts is not None:
            updated._validate_result(source_starts)
        return updated

    def public(self):
        body = {"id": self.id, "status": self.status}
        if self.result is not None:
            body["result"] = self.result
        if self.error is not None:
            body["error"] = self.error
        return body

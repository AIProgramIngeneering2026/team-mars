from dataclasses import dataclass, replace
from typing import Optional

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

    def transition(self, status, result=None, error=None, source_starts=None):
        if status not in TRANSITIONS[self.status]:
            raise ValueError("Запрещённый переход Job")
        if (status == "COMPLETED") != (result is not None):
            raise ValueError("Результат допустим только при COMPLETED")
        if (status == "FAILED") != (error is not None):
            raise ValueError("Ошибка обязательна только при FAILED")
        if status == "COMPLETED":
            validate_result(self.operation, result, source_starts)
        return replace(self, status=status, result=result, error=error)

    def public(self):
        body = {"id": self.id, "status": self.status}
        if self.result is not None:
            body["result"] = self.result
        if self.error is not None:
            body["error"] = self.error
        return body

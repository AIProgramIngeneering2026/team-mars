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


@dataclass(frozen=True)
class Job:
    id: str
    client: str
    video_id: str
    operation: str
    status: str
    result: Optional[dict] = None
    error: Optional[dict] = None

    def transition(self, status, result=None, error=None):
        if status not in TRANSITIONS[self.status]:
            raise ValueError("Запрещённый переход Job")
        if (status == "COMPLETED") != (result is not None):
            raise ValueError("Результат допустим только при COMPLETED")
        if (status == "FAILED") != (error is not None):
            raise ValueError("Ошибка обязательна только при FAILED")
        return replace(self, status=status, result=result, error=error)

    def public(self):
        body = {"id": self.id, "status": self.status}
        if self.result is not None:
            body["result"] = self.result
        if self.error is not None:
            body["error"] = self.error
        return body

"""Интерфейсы, через которые ядро обращается к внешнему миру.

Модуль не импортирует веб-фреймворки и драйверы баз данных.
"""
from typing import Optional, Protocol, Tuple

from .domain import Job

LEASE_SECONDS = 60
ANALYSIS_SECONDS = 60


class JobStore(Protocol):
    """Хранение Job. Каждая операция атомарна.

    Реализации: `SQLiteJobs` (mars.storage) и `InMemoryJobs` (mars.memory).
    """

    def create(self, client: str, key: str, video_id: str, operation: str) -> Tuple[int, dict]:
        """Создаёт Job или возвращает исходный ответ на повтор того же ключа.

        Тот же ключ с другим роликом или режимом даёт `ApiError(409, ...)`.
        """

    def get(self, client: str, job_id: str) -> Job:
        """Возвращает Job владельца. Чужая или отсутствующая даёт `ApiError(404, ...)`."""

    def cancel(self, client: str, job_id: str) -> dict:
        """Отменяет активную Job. Завершённая даёт `ApiError(409, ...)`."""

    def claim(self) -> Optional[Tuple[Job, str]]:
        """Берёт старейшую Job в `CREATED`, переводит её в `EXTRACTING_TRANSCRIPT`.

        Возвращает Job и токен права обработки либо `None`, если работы нет.
        """

    def transition(self, job_id: str, token: str, status: str,
                   result: Optional[dict] = None, error: Optional[dict] = None,
                   source_starts: Optional[set] = None) -> bool:
        """Переход по праву обработки.

        `False`, если Job уже отменена или завершена, право просрочено или не совпало,
        либо вышел срок анализа. Недопустимый переход поднимает `InvalidTransition`.
        """

    def maintain(self, current_claim: Optional[Tuple[str, str]] = None) -> None:
        """Завершает просроченные Job и продлевает право текущей обработки."""
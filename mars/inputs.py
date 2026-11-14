import re
from urllib.parse import parse_qs, urlsplit

from .errors import ApiError


def invalid(field, message):
    raise ApiError(422, "INVALID_FIELD", message, field)


def video_id_from_url(url):
    if not isinstance(url, str) or not url or len(url) > 2048:
        invalid("url", "Ожидалась HTTPS-ссылка длиной до 2048 символов")
    if any(character.isspace() for character in url):
        invalid("url", "Пробелы в ссылке недопустимы")
    try:
        parts = urlsplit(url)
        authority = parts.netloc.lower()
    except ValueError:
        invalid("url", "Некорректная ссылка")
    if parts.scheme != "https":
        invalid("url", "Поддерживается только HTTPS")
    if authority == "youtu.be":
        video_id = parts.path.removeprefix("/")
    elif authority in ("youtube.com", "www.youtube.com") and parts.path == "/watch":
        values = parse_qs(parts.query, keep_blank_values=True).get("v", [])
        if len(values) != 1:
            invalid("url", "Требуется ровно один параметр v")
        video_id = values[0]
    else:
        invalid("url", "Поддерживаются ссылки youtube.com/watch и youtu.be")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) is None:
        invalid("url", "ID ролика должен содержать 11 допустимых символов")
    return video_id


def parse_request(payload, key, operations):
    if not isinstance(payload, dict):
        invalid("body", "Тело должно быть JSON-объектом")
    extra = set(payload) - {"url", "operation"}
    if extra:
        invalid(sorted(extra)[0], "Неизвестное поле")
    video_id = video_id_from_url(payload.get("url"))
    operation = payload.get("operation", "brief")
    if not isinstance(operation, str):
        invalid("operation", "Ожидалась строка")
    if operation not in operations:
        raise ApiError(422, "UNSUPPORTED_OPERATION", "Неизвестный режим", "operation")
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) is None:
        invalid("Idempotency-Key", "Укажите ключ из 1–128 букв, цифр, дефисов или подчёркиваний")
    return video_id, operation

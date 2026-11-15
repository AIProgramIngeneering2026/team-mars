import hashlib
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .errors import ApiError

MAX_BODY = 16 * 1024
logger = logging.getLogger(__name__)


def make_server(address, service, token):
    if not token:
        raise ValueError("Укажите MARS_TOKEN")
    client = hashlib.sha256(token.encode("utf-8")).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _read_json(self):
            if self.headers.get("Transfer-Encoding") is not None:
                raise ApiError(400, "INVALID_JSON", "Передайте тело с Content-Length")
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1:
                    raise ValueError
                length = int(lengths[0])
                if length < 0:
                    raise ValueError
            except ValueError:
                raise ApiError(400, "INVALID_JSON", "Некорректный Content-Length")
            if length > MAX_BODY:
                raise ApiError(413, "REQUEST_TOO_LARGE", "Тело превышает 16 КиБ")
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Ожидался application/json")
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                return json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                raise ApiError(400, "INVALID_JSON", "Некорректный JSON")

        def _handle(self):
            self.connection.settimeout(10)
            try:
                expected = ("Bearer " + token).encode("utf-8")
                credentials = self.headers.get_all("Authorization", [])
                if len(credentials) != 1 or not hmac.compare_digest(credentials[0].encode("utf-8"), expected):
                    raise ApiError(401, "UNAUTHORIZED", "Требуется действительный токен")
                path = unquote(urlsplit(self.path).path)
                parts = path.strip("/").split("/")
                if self.command == "POST" and path == "/jobs":
                    payload = self._read_json()
                    keys = self.headers.get_all("Idempotency-Key", [])
                    key = keys[0] if len(keys) == 1 else None
                    status, body = service.create(client, key, payload)
                    self._send(status, body, {"Location": "/jobs/" + body["id"]})
                elif self.command == "GET" and len(parts) == 2 and parts[0] == "jobs":
                    self._send(200, service.get(client, parts[1]))
                elif self.command == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                    self._send(200, service.cancel(client, parts[1]))
                else:
                    raise ApiError(404, "JOB_NOT_FOUND", "Ресурс не найден")
            except ApiError as error:
                headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
                self._send(error.status, error.body(), headers)
            except (TimeoutError, ConnectionError):
                self.close_connection = True
            except Exception:
                logger.exception("Ошибка HTTP-запроса")
                self._send(500, {"error": {"code": "INTERNAL_ERROR", "message": "Внутренняя ошибка"}})

        def _send(self, status, body, headers=None):
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def log_message(self, message, *args):
            logger.info(message, *args)

    return ThreadingHTTPServer(address, Handler)

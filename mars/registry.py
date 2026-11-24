from collections.abc import Mapping

from .errors import InvalidToolRegistration, UnknownOperation


class Registry:
    """Готовые Tool по именам; проверка регистрации выполняется при сборке."""

    def __init__(self, tools):
        self._tools = {}
        entries = tools.items() if isinstance(tools, Mapping) else tools
        for operation, tool in entries:
            if not isinstance(operation, str) or not operation.strip():
                raise InvalidToolRegistration(operation, "Имя операции должно быть непустой строкой")
            if operation in self._tools:
                raise InvalidToolRegistration(operation, f"Операция уже зарегистрирована: {operation!r}")
            if not callable(getattr(tool, "run", None)):
                raise InvalidToolRegistration(operation, "Tool должен предоставлять метод run")
            self._tools[operation] = tool

    def operations(self):
        return tuple(self._tools)

    def get(self, operation):
        if not isinstance(operation, str) or operation not in self._tools:
            raise UnknownOperation(operation)
        return self._tools[operation]

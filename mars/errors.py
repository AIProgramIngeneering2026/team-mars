class ApiError(Exception):
    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.field = field

    def body(self):
        error = {"code": self.code, "message": str(self)}
        if self.field is not None:
            error["field"] = self.field
        return {"error": error}


class ProcessingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class JobTransitionError(ValueError):
    code = "INVALID_JOB_TRANSITION"

    def __init__(self, from_status, to_status):
        super().__init__(f"Запрещённый переход Job: {from_status!r} → {to_status!r}")
        self.from_status = from_status
        self.to_status = to_status


class JobStateError(ValueError):
    code = "INVALID_JOB_STATE"

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


class UnknownOperation(LookupError):
    code = "UNKNOWN_OPERATION"

    def __init__(self, operation):
        super().__init__(f"Неизвестная операция: {operation!r}")
        self.operation = operation


class InvalidToolRegistration(ValueError):
    code = "INVALID_TOOL_REGISTRATION"

    def __init__(self, operation, message):
        super().__init__(message)
        self.operation = operation

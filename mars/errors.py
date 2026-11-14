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

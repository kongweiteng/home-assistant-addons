"""Stable sanitized errors shared by work runtime adapters."""
class WorkRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str = 'Work runtime policy rejected the operation.', *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain

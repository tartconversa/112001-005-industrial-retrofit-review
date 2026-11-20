"""Domain errors for the transition-support service."""


class AppError(Exception):
    """A rule violation that the caller can fix (bad input or illegal transition)."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

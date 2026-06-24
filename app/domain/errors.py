from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    BAD_RESPONSE = "BAD_RESPONSE"
    VERIFY_FAILED = "VERIFY_FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ProviderError(BaseModel):
    provider: str
    error: ErrorCode
    message: str


class ProviderException(Exception):
    def __init__(self, provider_error: ProviderError):
        super().__init__(provider_error.message)
        self.provider_error = provider_error


class AggregateProviderError(Exception):
    def __init__(self, provider_errors: list[ProviderError]):
        super().__init__("All providers failed.")
        self.provider_errors = provider_errors

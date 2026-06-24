from fastapi import status
from fastapi.responses import JSONResponse

from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError


def status_for(errors: list[ProviderError]) -> int:
    codes = {error.error for error in errors}
    if codes == {ErrorCode.NOT_FOUND}:
        return status.HTTP_404_NOT_FOUND
    if ErrorCode.UNSUPPORTED in codes:
        return status.HTTP_400_BAD_REQUEST
    if ErrorCode.VERIFY_FAILED in codes:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_502_BAD_GATEWAY


def error_response(error: AggregateProviderError) -> JSONResponse:
    errors = error.provider_errors
    code = errors[0].error if errors else ErrorCode.BAD_RESPONSE
    return JSONResponse(
        status_code=status_for(errors),
        content={
            "error": code.value,
            "message": "Package or version was not found by enabled providers.",
            "providerErrors": [provider_error.model_dump(mode="json") for provider_error in errors],
        },
    )

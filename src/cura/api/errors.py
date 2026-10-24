"""RFC 9457 problem details for every error the API returns."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str = "", *, headers: dict | None = None, **extra):
        self.status, self.title, self.detail, self.headers, self.extra = status, title, detail, headers or {}, extra


def _response(status: int, title: str, detail: str, request: Request, headers=None, **extra) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        **extra,
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem):
        return _response(exc.status, exc.title, exc.detail, request, exc.headers, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _response(exc.status_code, str(exc.detail), "", request, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _response(422, "Invalid request", "See `errors`.", request, errors=errors)

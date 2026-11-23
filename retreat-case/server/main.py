"""Бэкенд виджета ретрита (кейс 7).

POST /api/chatkit — свой сервер ChatKit: агенты на Agents SDK, RU и EN (chatkit_server.py).
Agent Builder отключают 30.11.2026, поэтому это основной путь.

POST /api/create-session — прежний режим: обмен workflow Agent Builder (wf_…) на
client_secret. Оставлен до окончательного переключения, чтобы можно было откатиться.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

load_dotenv(Path(__file__).resolve().parent / ".env")

DEFAULT_CHATKIT_BASE = "https://api.openai.com"
SESSION_COOKIE_NAME = "chatkit_session_id"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

app = FastAPI(title="ChatKit session (Agent Builder workflow)")


def _cors_origins() -> list[str]:
    raw = os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:8080,http://localhost:8080,http://127.0.0.1:5500,http://localhost:5500",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_chatkit_server = None


def _get_chatkit_server():
    """Создается при первом запросе: без OPENAI_API_KEY старый режим продолжает работать."""
    global _chatkit_server
    if _chatkit_server is None:
        from chatkit_server import RetreatChatKitServer

        _chatkit_server = RetreatChatKitServer(os.getenv("CHATKIT_DATA_DIR", "/app/data"))
    return _chatkit_server


@app.post("/api/chatkit")
async def chatkit_endpoint(request: Request) -> Response:
    from chatkit_server import is_streaming

    user_id, cookie_value = _resolve_user(request.cookies)
    lang = request.headers.get("x-retreat-lang", "ru")
    context = {
        "user_id": user_id,
        "lang": "en" if lang == "en" else "ru",
        "ip": request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown"),
    }
    result = await _get_chatkit_server().process(await request.body(), context)
    if is_streaming(result):
        response: Response = StreamingResponse(
            result, media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
        )
    else:
        response = Response(content=result.json, media_type="application/json")
    if cookie_value:
        response.set_cookie(
            key=SESSION_COOKIE_NAME, value=cookie_value, max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            httponly=True, samesite="lax", secure=_is_prod(), path="/",
        )
    return response


@app.get("/health")
async def health() -> Mapping[str, str]:
    return {"status": "ok"}


@app.get("/api/thread-items/{thread_id}")
async def list_thread_items(thread_id: str) -> JSONResponse:
    """Debug: fetch all items in a ChatKit thread so we can see what the backend stored."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return JSONResponse({"error": "no key"}, 500)
    api_base = _chatkit_api_base()
    async with httpx.AsyncClient(base_url=api_base, timeout=15.0) as client:
        resp = await client.get(
            f"/v1/chatkit/threads/{thread_id}/items",
            params={"order": "asc", "limit": 50},
            headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Beta": "chatkit_beta=v1",
            },
        )
    return JSONResponse(_parse_json(resp), resp.status_code)


@app.post("/api/create-session")
async def create_session(request: Request) -> JSONResponse:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return _respond({"error": "Missing OPENAI_API_KEY"}, 500)

    body = await _read_json_body(request)
    workflow_id = _resolve_workflow_id(body)
    if not workflow_id:
        return _respond({"error": "Missing workflow id (body or CHATKIT_WORKFLOW_ID)"}, 400)

    user_id, cookie_value = _resolve_user(request.cookies)
    api_base = _chatkit_api_base()
    session_json = _build_chatkit_session_payload(workflow_id, body, user_id)

    try:
        async with httpx.AsyncClient(base_url=api_base, timeout=30.0) as client:
            upstream = await client.post(
                "/v1/chatkit/sessions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Beta": "chatkit_beta=v1",
                    "Content-Type": "application/json",
                },
                json=session_json,
            )
    except httpx.RequestError as error:
        return _respond(
            {"error": f"Failed to reach ChatKit API: {error}"},
            502,
            cookie_value,
        )

    payload = _parse_json(upstream)
    if not upstream.is_success:
        message: str | None = None
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, Mapping):
                message = err.get("message") or err.get("code")
            elif isinstance(err, str):
                message = err
        message = message or upstream.reason_phrase or "Failed to create session"
        return _respond({"error": message}, upstream.status_code, cookie_value)

    client_secret = None
    expires_after = None
    if isinstance(payload, Mapping):
        client_secret = payload.get("client_secret")
        expires_after = payload.get("expires_after")

    if not client_secret:
        return _respond({"error": "Missing client secret in response"}, 502, cookie_value)

    return _respond(
        {"client_secret": client_secret, "expires_after": expires_after},
        200,
        cookie_value,
    )


def _respond(
    payload: Mapping[str, Any],
    status_code: int,
    cookie_value: str | None = None,
) -> JSONResponse:
    response = JSONResponse(dict(payload), status_code=status_code)
    if cookie_value:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_value,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_is_prod(),
            path="/",
        )
    return response


def _is_prod() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("NODE_ENV") or "").lower()
    return env == "production"


async def _read_json_body(request: Request) -> Mapping[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _build_chatkit_session_payload(
    workflow_id: str, body: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Собирает тело POST /v1/chatkit/sessions.

    Ключ workflow.tracing передаётся только если задан CHATKIT_WORKFLOW_TRACING
    (или tracing пришёл в теле запроса). Иначе тело совпадает со стартовым
    примером OpenAI — сервер ChatKit применяет свои умолчания.
    """
    workflow: dict[str, Any] = {"id": workflow_id}
    req_wf = body.get("workflow")
    if isinstance(req_wf, Mapping):
        if req_wf.get("version") is not None:
            workflow["version"] = req_wf["version"]
        sv = req_wf.get("state_variables")
        if isinstance(sv, Mapping):
            workflow["state_variables"] = dict(sv)
        tr = req_wf.get("tracing")
        if isinstance(tr, Mapping):
            workflow["tracing"] = {"enabled": bool(tr.get("enabled", True))}

    env_version = (
        os.getenv("CHATKIT_WORKFLOW_VERSION_EN") if _is_english(body) else os.getenv("CHATKIT_WORKFLOW_VERSION")
    ) or ""
    env_version = env_version.strip()
    if env_version and "version" not in workflow:
        workflow["version"] = env_version

    if "tracing" not in workflow:
        raw_trace = (os.getenv("CHATKIT_WORKFLOW_TRACING") or "").strip().lower()
        if raw_trace:
            workflow["tracing"] = {
                "enabled": raw_trace
                in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            }

    return {"workflow": workflow, "user": user_id}


def _is_english(body: Mapping[str, Any]) -> bool:
    """Английская страница передает lang=en. Без EN-workflow остается русский."""
    return body.get("lang") == "en" and bool((os.getenv("CHATKIT_WORKFLOW_ID_EN") or "").strip())


def _resolve_workflow_id(body: Mapping[str, Any]) -> str | None:
    workflow = body.get("workflow", {})
    workflow_id = None
    if isinstance(workflow, Mapping):
        workflow_id = workflow.get("id")
    workflow_id = workflow_id or body.get("workflowId")
    if _is_english(body):
        env_workflow = os.getenv("CHATKIT_WORKFLOW_ID_EN")
    else:
        env_workflow = os.getenv("CHATKIT_WORKFLOW_ID") or os.getenv("VITE_CHATKIT_WORKFLOW_ID")
    if not workflow_id and env_workflow:
        workflow_id = env_workflow
    if workflow_id and isinstance(workflow_id, str) and workflow_id.strip():
        return workflow_id.strip()
    return None


def _resolve_user(cookies: Mapping[str, str]) -> tuple[str, str | None]:
    existing = cookies.get(SESSION_COOKIE_NAME)
    if existing:
        return existing, None
    user_id = str(uuid.uuid4())
    return user_id, user_id


def _chatkit_api_base() -> str:
    return (
        os.getenv("CHATKIT_API_BASE")
        or os.getenv("VITE_CHATKIT_API_BASE")
        or DEFAULT_CHATKIT_BASE
    )


def _parse_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        parsed = response.json()
        return parsed if isinstance(parsed, Mapping) else {}
    except (json.JSONDecodeError, httpx.DecodingError):
        return {}

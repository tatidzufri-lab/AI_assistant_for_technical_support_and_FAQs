"""Свой сервер ChatKit для виджета ретрита (замена workflow из Agent Builder).

Ход одного сообщения, как в прежнем workflow:
  лимиты демо → guardrail «Jailbreak» и классификатор (параллельно) →
  готовый текст (приветствие, отказ) или агент со стримингом ответа в виджет.
Язык треда задает страница (заголовок X-Retreat-Lang) при первом сообщении
и хранится в metadata треда.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from agents import Runner
from chatkit.agents import AgentContext, simple_to_agent_input, stream_agent_response
from chatkit.server import ChatKitServer
from chatkit.types import (
    AssistantMessageContent,
    AssistantMessageItem,
    ThreadItemDoneEvent,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)
from openai import AsyncOpenAI

import retreat_agents as ra
from sqlite_store import Context, SQLiteStore

logger = logging.getLogger("retreat.chatkit")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

MAX_MESSAGE_CHARS = int(os.getenv("DEMO_MAX_MESSAGE_CHARS", "1000"))
MAX_PER_IP_DAY = int(os.getenv("DEMO_MAX_PER_IP_DAY", "40"))
MAX_PER_DAY = int(os.getenv("DEMO_MAX_PER_DAY", "400"))
HISTORY_ITEMS = 20

GUARDRAIL_CONFIG = {
    "version": 1,
    "guardrails": [
        {"name": "Jailbreak", "config": {"model": ra.MODEL, "confidence_threshold": 0.7}},
    ],
}


class DailyQuota:
    """Суточные счетчики сообщений: всего и на IP. Сутки по UTC."""

    def __init__(self, path: str):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, ip TEXT, n INTEGER, PRIMARY KEY(day, ip))")
        self._lock = asyncio.Lock()

    async def acquire(self, ip: str) -> bool:
        day = date.today().isoformat()
        async with self._lock:
            with sqlite3.connect(self.path) as c:
                total = c.execute("SELECT COALESCE(SUM(n), 0) FROM usage WHERE day=?", (day,)).fetchone()[0]
                mine = c.execute("SELECT COALESCE(n, 0) FROM usage WHERE day=? AND ip=?", (day, ip)).fetchone()
                mine = mine[0] if mine else 0
                if (MAX_PER_DAY and total >= MAX_PER_DAY) or (MAX_PER_IP_DAY and mine >= MAX_PER_IP_DAY):
                    return False
                c.execute(
                    "INSERT INTO usage(day, ip, n) VALUES(?,?,1) ON CONFLICT(day, ip) DO UPDATE SET n = n + 1",
                    (day, ip),
                )
        return True


def _text_of(item: UserMessageItem | None) -> str:
    if item is None:
        return ""
    return " ".join(getattr(part, "text", "") for part in item.content).strip()


class RetreatChatKitServer(ChatKitServer[Context]):
    def __init__(self, data_dir: str | Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(SQLiteStore(data_dir / "chatkit.db"))
        self.quota = DailyQuota(str(data_dir / "chatkit.db"))
        from guardrails.runtime import instantiate_guardrails, load_config_bundle

        self._guardrails = instantiate_guardrails(load_config_bundle(GUARDRAIL_CONFIG))
        self._guardrail_ctx = SimpleNamespace(guardrail_llm=AsyncOpenAI())

    # --- шаги хода --------------------------------------------------------

    async def _is_jailbreak(self, text: str) -> bool:
        from guardrails.runtime import run_guardrails

        try:
            results = await run_guardrails(
                self._guardrail_ctx, text, "text/plain", self._guardrails,
                suppress_tripwire=True, raise_guardrail_errors=True,
            )
        except Exception:
            # Как в прежнем workflow: ошибка guardrail — безопасный отказ
            logger.exception("Guardrail failed")
            return True
        return any(getattr(r, "tripwire_triggered", False) for r in results or [])

    async def _route(self, text: str, staff: bool) -> str:
        # Код, введенный раньше в этом треде, действует на весь диалог:
        # классификатору подставляем его, чтобы правило про шаблоны сработало
        has_code = ra.STAFF_CODE.lower() in text.lower()
        result = await Runner.run(ra.classifier(), text if has_code or not staff else f"{ra.STAFF_CODE} {text}")
        route = result.final_output.route
        # Шаблоны — только для команды, даже если классификатор ошибся
        if route == "templates" and not staff:
            route = "general"
        return route

    def _static(self, thread: ThreadMetadata, context: Context, text: str) -> ThreadItemDoneEvent:
        return ThreadItemDoneEvent(
            item=AssistantMessageItem(
                id=self.store.generate_item_id("message", thread, context),
                thread_id=thread.id,
                created_at=datetime.now(timezone.utc),
                content=[AssistantMessageContent(text=text)],
            )
        )

    # --- основной метод ChatKit -------------------------------------------

    async def respond(
        self, thread: ThreadMetadata, input_user_message: UserMessageItem | None, context: Context
    ) -> AsyncIterator[ThreadStreamEvent]:
        lang = thread.metadata.get("lang")
        if lang not in ("ru", "en"):
            lang = "en" if context.get("lang") == "en" else "ru"
            thread.metadata["lang"] = lang  # сервер ChatKit сам сохранит измененный тред
        text = _text_of(input_user_message)
        if not thread.title and text:
            thread.title = text[:60]

        if len(text) > MAX_MESSAGE_CHARS:
            yield self._static(thread, context, ra.STATIC["too_long"][lang])
            return
        if not await self.quota.acquire(str(context.get("ip") or "unknown")):
            yield self._static(thread, context, ra.STATIC["limit"][lang])
            return

        has_code = ra.STAFF_CODE.lower() in text.lower()
        if has_code:
            thread.metadata["staff"] = True
        staff = bool(thread.metadata.get("staff"))

        blocked, route = await asyncio.gather(self._is_jailbreak(text), self._route(text, staff))
        logger.info("thread=%s lang=%s route=%s blocked=%s staff=%s", thread.id, lang, route, blocked, staff)
        if blocked:
            yield self._static(thread, context, ra.STATIC["blocked"][lang])
            return
        # Только код без просьбы — подтверждаем доступ и спрашиваем, какое письмо нужно
        rest = re.sub(re.escape(ra.STAFF_CODE), "", text, flags=re.IGNORECASE).strip(" \t\n,.:;!-—")
        if has_code and len(rest) < 3:
            yield self._static(thread, context, ra.STATIC["staff_on"][lang])
            return
        if route == "welcome":
            yield self._static(thread, context, ra.STATIC["welcome"][lang])
            return

        # Агент видит последние сообщения треда: прежний workflow помнил только текущий вопрос
        page = await self.store.load_thread_items(thread.id, None, HISTORY_ITEMS, "desc", context)
        items = list(reversed(page.data))
        input_items = await simple_to_agent_input(items)

        agent_context = AgentContext(thread=thread, store=self.store, request_context=context)
        result = Runner.run_streamed(ra.agent_for(route, lang), input_items, context=agent_context)
        async for event in stream_agent_response(agent_context, result):
            yield event


def is_streaming(result: Any) -> bool:
    from chatkit.server import StreamingResult

    return isinstance(result, StreamingResult)

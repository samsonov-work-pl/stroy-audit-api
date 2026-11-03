"""Серверная проверка оплаты для эндпоинтов аудита.

Тикет — короткоживущая подпись сайта (HMAC-SHA256), по которой API сам
списывает кредит из Postgres. Секрет AUDIT_TICKET_SECRET общий для
Vercel и Render; без него тикеты не проверяются, и запуск аудита
закрывается (fail closed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from typing import Any

log = logging.getLogger("audit-flow")

TICKET_TTL_SECONDS = 15 * 60
MAX_FILE_BYTES = 50 * 1024 * 1024
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
PAID_PLANS = ("full", "choice", "business")


def database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def ticket_secret() -> str:
    return (os.getenv("AUDIT_TICKET_SECRET") or "").strip()


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def verify_ticket(raw: str | None) -> dict[str, Any] | None:
    """Проверяет подпись и срок тикета. Возвращает payload или None."""
    secret = ticket_secret()
    if not secret or not raw or raw.count(".") != 1:
        return None
    payload_text, signature_text = raw.split(".", 1)
    try:
        expected = hmac.new(secret.encode("utf-8"), payload_text.encode("ascii"), hashlib.sha256).digest()
        signature = _b64decode(signature_text)
        payload = json.loads(_b64decode(payload_text).decode("utf-8"))
    except Exception:
        return None
    if not hmac.compare_digest(expected, signature):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    email = payload.get("email")
    plan = payload.get("tier")
    jti = payload.get("jti")
    if not isinstance(email, str) or not isinstance(jti, str) or plan not in PAID_PLANS:
        return None
    return {"email": email.strip().lower(), "plan": plan, "jti": jti}


def ensure_flow_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_tickets_used (
            jti TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_credits (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            payment_event_id TEXT UNIQUE,
            contract_id TEXT,
            plan TEXT NOT NULL,
            granted INTEGER NOT NULL,
            remaining INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS audit_credits_email_idx ON audit_credits (email)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_credit_uses (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            credit_id TEXT NOT NULL,
            restored BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def consume_ticket_once(jti: str) -> bool:
    """Тикет одноразовый: повторный запуск того же тикета отклоняется."""
    dsn = database_url()
    if not dsn:
        log.error("DATABASE_URL не задан: тикет не может быть погашен")
        return False
    try:
        import psycopg
    except ImportError:
        log.error("psycopg не установлен: тикет не может быть погашен")
        return False
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                ensure_flow_tables(cur)
                cur.execute(
                    "INSERT INTO audit_tickets_used (jti) VALUES (%s) ON CONFLICT (jti) DO NOTHING",
                    (jti,),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        return inserted
    except Exception:
        log.exception("Не удалось погасить тикет")
        return False


def consume_credit(email: str, preferred_plan: str | None = None) -> dict[str, Any] | None:
    """Списывает один оплаченный проект, предпочитая свежекупленный тариф."""
    dsn = database_url()
    if not dsn:
        log.error("DATABASE_URL не задан: списание невозможно")
        return None
    try:
        import psycopg
    except ImportError:
        log.error("psycopg не установлен: списание невозможно")
        return None
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_flow_tables(cur)
            cur.execute(
                """
                SELECT id, plan FROM audit_credits
                WHERE email = %s AND remaining > 0
                ORDER BY CASE WHEN plan = %s THEN 0 ELSE 1 END, created_at
                LIMIT 1
                FOR UPDATE
                """,
                (email, (preferred_plan or "").strip().lower()),
            )
            row = cur.fetchone()
            if not row:
                return None
            credit_id, plan = row
            cur.execute("UPDATE audit_credits SET remaining = remaining - 1 WHERE id = %s", (credit_id,))
            claim_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO audit_credit_uses (id, email, credit_id) VALUES (%s, %s, %s)",
                (claim_id, email, credit_id),
            )
        conn.commit()
    return {"claim_id": claim_id, "plan": plan}


def restore_credit(claim_id: str) -> bool:
    """Возвращает списанный проект на баланс (например, при сбое анализа)."""
    dsn = database_url()
    if not dsn:
        return False
    try:
        import psycopg
    except ImportError:
        return False
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_flow_tables(cur)
            cur.execute(
                """
                UPDATE audit_credit_uses
                SET restored = TRUE
                WHERE id = %s AND restored = FALSE
                RETURNING credit_id
                """,
                (claim_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("UPDATE audit_credits SET remaining = remaining + 1 WHERE id = %s", (row[0],))
        conn.commit()
    return True


class SlidingWindowLimiter:
    """Скользящий лимитер по ключу (например, IP) в памяти процесса."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        hits = self._hits[key]
        while hits and hits[0] <= moment - self.window_seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(moment)
        return True


def validate_upload(file) -> str | None:
    """Проверяет тип и размер файла. Возвращает текст проблемы или None."""
    mime = (getattr(file, "content_type", "") or "").split(";")[0].strip().lower()
    if mime and mime not in ALLOWED_MIME_TYPES:
        return "Поддерживаются PDF, изображения (JPEG, PNG, HEIC) и таблицы Excel."
    size = getattr(file, "size", None)
    if size is not None and size > MAX_FILE_BYTES:
        return "Файл больше 50 МБ. Разделите проект на части или пришлите его письмом."
    return None

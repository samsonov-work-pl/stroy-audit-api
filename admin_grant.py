"""Ручное начисление оплаченных проектов: запасной канал оплаты.

POST /api/admin/credits/grant (заголовок X-Admin-Key = ADMIN_GRANT_SECRET):
клиент платит (деньги в Lava приходят независимо от вебхука), пишет на
hello@ — администратор сверяет платёж в кабинете Lava и начисляет кредиты
руками. Кредиты кладутся в ту же таблицу audit_credits, что и вебхук,
поэтому цепочка claim → тикет → deep не меняется. payment_id из кабинета
Lava даёт идемпотентность: повторное начисление того же платежа вернёт
already_granted и не задвоит кредиты; совпадающий contract_id вебхука
тоже не задвоит начисление.

GET /api/admin/credits/balance?email=... — диагностика баланса (на Render
публичного GET /api/credits нет, это ожидаемо).
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import uuid

from fastapi import APIRouter, HTTPException, Request

from payments import ensure_credit_tables

log = logging.getLogger("admin-grant")

PLAN_SLOTS = {"full": 1, "choice": 3, "business": 10}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

router = APIRouter()


def admin_secret() -> str:
    secret = (os.getenv("ADMIN_GRANT_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="ADMIN_GRANT_SECRET не задан.")
    return secret


def check_admin(request: Request) -> None:
    expected = admin_secret()
    received = request.headers.get("x-admin-key") or request.headers.get("X-Admin-Key") or ""
    if not received or not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Неверный ключ.")


def database_dsn() -> str:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        raise HTTPException(status_code=500, detail="DATABASE_URL не задан.")
    return dsn


def import_psycopg():
    try:
        import psycopg
    except ImportError:
        raise HTTPException(status_code=500, detail="psycopg не установлен.") from None
    return psycopg


@router.post("/api/admin/credits/grant")
async def grant_credits(request: Request):
    """Начисляет оплаченные проекты на email вручную."""
    check_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидался JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")

    email = str(body.get("email") or "").strip().lower()
    plan = str(body.get("plan") or "").strip().lower()
    payment_id = str(body.get("payment_id") or "").strip() or None
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Укажите корректный email.")
    if plan not in PLAN_SLOTS:
        raise HTTPException(status_code=400, detail="plan должен быть full, choice или business.")
    slots = PLAN_SLOTS[plan]

    psycopg = import_psycopg()
    event_id = payment_id or f"manual-{uuid.uuid4()}"
    contract_id = payment_id or None
    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            ensure_credit_tables(cur)
            if payment_id:
                cur.execute(
                    "SELECT 1 FROM audit_credits WHERE payment_event_id = %s OR contract_id = %s LIMIT 1",
                    (payment_id, payment_id),
                )
                if cur.fetchone():
                    log.info("Ручное начисление %s для %s уже сделано", payment_id, email)
                    return {"ok": True, "duplicate": True, "email": email, "plan": plan}
            cur.execute(
                """
                INSERT INTO audit_credits (
                    id, email, payment_event_id, contract_id, plan, granted, remaining
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (payment_event_id) DO NOTHING
                """,
                (str(uuid.uuid4()), email, event_id, contract_id, plan, slots, slots),
            )
            inserted = cur.rowcount > 0
        conn.commit()

    if not inserted:
        return {"ok": True, "duplicate": True, "email": email, "plan": plan}
    log.info("Ручное начисление plan=%s email=%s payment_id=%s slots=%s", plan, email, payment_id, slots)
    return {"ok": True, "duplicate": False, "email": email, "plan": plan, "granted": slots}


@router.get("/api/admin/credits/balance")
def credits_balance(request: Request, email: str = ""):
    """Баланс оплаченных проектов по email (диагностика)."""
    check_admin(request)
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Укажите корректный email.")
    psycopg = import_psycopg()
    with psycopg.connect(database_dsn()) as conn:
        with conn.cursor() as cur:
            ensure_credit_tables(cur)
            cur.execute(
                """
                SELECT plan, granted, remaining, created_at
                FROM audit_credits WHERE email = %s ORDER BY created_at
                """,
                (email,),
            )
            rows = cur.fetchall()
        conn.commit()
    credits = [
        {
            "plan": row[0],
            "granted": row[1],
            "remaining": row[2],
            "created_at": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]
    remaining = sum(row[2] for row in rows)
    return {"ok": True, "email": email, "remaining": remaining, "credits": credits}

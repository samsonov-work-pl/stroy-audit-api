"""Вебхук Lava.top для сервиса аудита: POST /api/webhook/lava."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("lava-webhook")

SUCCESS_EVENTS = {
    "payment.success",
}
REVOKE_EVENTS = {
    "refund.success",
    "chargeback.initiated",
}
PAID_PLANS = ("full", "choice", "business")
PLAN_SLOTS = {"full": 1, "choice": 3, "business": 10}
KNOWN_FULL_PRODUCTS = {"e262a88a-aabe-4a5b-8cf8-d0774e25f98a"}
KNOWN_FULL_OFFERS = {"897a9501-3853-49cd-b22c-1cada7b220ba"}
KNOWN_CHOICE_PRODUCTS = {"46156488-439c-4f84-86b9-7628f5a6e93e"}
KNOWN_CHOICE_OFFERS = {"5cf765a3-e5b3-4d79-8f4c-2f603aebf703"}
KNOWN_BUSINESS_PRODUCTS = {"0ecbb153-f2a5-415b-93e0-03fd0a2b04c6"}
KNOWN_BUSINESS_OFFERS = {"98783786-61c8-4f37-aa28-ffc1a4477c94"}

PLAN_LABELS = {
    "express": "Экспресс",
    "full": "Полный аудит",
    "choice": "Выбор застройщика",
    "business": "Профи / Бизнес",
}

router = APIRouter()


def webhook_secret() -> str:
    secret = (os.getenv("LAVA_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="LAVA_WEBHOOK_SECRET не задан.")
    return secret


def csv_ids(name: str) -> set[str]:
    raw = os.getenv(name) or ""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        current: Any = payload
        found = True
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and current not in (None, ""):
            return current
    return None


def as_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        for key in ("email", "id", "uuid", "value", "name"):
            nested = value.get(key)
            if nested not in (None, ""):
                return str(nested).strip()
        return None
    return str(value).strip()


def verify_api_key(request: Request, secret: str) -> bool:
    received = request.headers.get("x-api-key") or request.headers.get("X-Api-Key") or ""
    if not received:
        auth = request.headers.get("authorization") or ""
        prefix = "ApiKey "
        if auth.startswith(prefix):
            received = auth[len(prefix) :]
    if not received:
        return False
    return hmac.compare_digest(received.encode("utf-8"), secret.encode("utf-8"))


def verify_hmac_signature(raw_body: bytes, request: Request, secret: str) -> bool:
    signature = (
        request.headers.get("signature")
        or request.headers.get("x-signature")
        or request.headers.get("X-Lava-Signature")
        or ""
    )
    if not signature:
        return False
    try:
        data = json.loads(raw_body.decode("utf-8"))
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (UnicodeDecodeError, json.JSONDecodeError):
        canonical_bytes = raw_body
    else:
        canonical_bytes = canonical.encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature.strip().lower())


def authenticate(request: Request, raw_body: bytes) -> None:
    secret = webhook_secret()
    if verify_api_key(request, secret) or verify_hmac_signature(raw_body, request, secret):
        return
    raise HTTPException(status_code=401, detail="Неверный секретный ключ.")


def resolve_plan(payload: dict[str, Any], data: dict[str, Any]) -> str | None:
    offer_id = as_text(
        first_value(
            payload,
            "offerId",
            "offer_id",
            "offer.id",
            "product.offerId",
            "product.tierId",
            "product.tier_id",
        )
        or first_value(data, "offerId", "offer_id", "tier_id", "product.tier_id", "product.tierId")
    )
    product_id = as_text(
        first_value(payload, "productId", "product_id", "product.id", "product.productId", "product.product_id")
        or first_value(data, "productId", "product_id", "product.id", "product.product_id")
    )
    product_name = (
        as_text(
            first_value(
                payload,
                "productName",
                "product_name",
                "product.name",
                "product.productName",
                "product.product_name",
                "productTitle",
            )
            or first_value(data, "product_name", "product.name")
        )
        or ""
    ).lower()

    mapping = (
        ("full", csv_ids("LAVA_OFFER_FULL") | csv_ids("LAVA_PRODUCT_FULL") | KNOWN_FULL_PRODUCTS | KNOWN_FULL_OFFERS),
        ("choice", csv_ids("LAVA_OFFER_CHOICE") | csv_ids("LAVA_PRODUCT_CHOICE") | KNOWN_CHOICE_PRODUCTS | KNOWN_CHOICE_OFFERS),
        ("business", csv_ids("LAVA_OFFER_BUSINESS") | csv_ids("LAVA_PRODUCT_BUSINESS") | KNOWN_BUSINESS_PRODUCTS | KNOWN_BUSINESS_OFFERS),
        ("express", csv_ids("LAVA_OFFER_EXPRESS") | csv_ids("LAVA_PRODUCT_EXPRESS")),
    )
    for plan, ids in mapping:
        if offer_id and offer_id.lower() in ids:
            return plan
        if product_id and product_id.lower() in ids:
            return plan

    if any(token in product_name for token in ("профи", "бизнес")):
        return "business"
    if "выбор" in product_name and "застройщик" in product_name:
        return "choice"
    if any(token in product_name for token in ("полный", "full", "pro", "deep")):
        return "full"
    if "экспресс" in product_name or "express" in product_name:
        return "express"
    return None


def parse_event(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event_type = as_text(first_value(payload, "eventType", "event_type", "type")) or ""
    event_id = as_text(first_value(payload, "eventId", "event_id", "id")) or str(uuid.uuid4())
    contract_id = as_text(
        first_value(payload, "contractId", "contract_id", "invoiceId", "invoice_id")
        or first_value(data, "contract_id", "invoice_id")
    )
    buyer = first_value(payload, "buyer", "buyerEmail", "buyer_email", "email", "customer.email") or first_value(
        data, "customer_email", "email"
    )
    email = as_text(buyer)
    amount_raw = first_value(payload, "amount", "totalPrice", "total_price", "price") or first_value(data, "amount")
    currency = as_text(first_value(payload, "currency") or first_value(data, "currency"))
    if isinstance(amount_raw, dict):
        currency = currency or as_text(amount_raw.get("currency"))
        amount_raw = amount_raw.get("amount") or amount_raw.get("value")
    return {
        "event_id": event_id,
        "event_type": event_type.lower(),
        "contract_id": contract_id,
        "email": email.lower() if email else None,
        "amount": str(amount_raw) if amount_raw not in (None, "") else None,
        "currency": currency,
        "offer_id": as_text(first_value(payload, "offerId", "offer_id") or first_value(data, "tier_id", "offer_id")),
        "product_id": as_text(
            first_value(payload, "productId", "product.id", "product.productId")
            or first_value(data, "product.product_id")
        ),
        "product_name": as_text(
            first_value(payload, "productName", "product.name", "product.productName")
            or first_value(data, "product.product_name")
        ),
        "plan": resolve_plan(payload, data),
    }


def sqlite_conn() -> sqlite3.Connection:
    path = Path(os.getenv("PAYMENTS_DB_PATH") or "payments.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            event_id TEXT UNIQUE,
            contract_id TEXT,
            email TEXT,
            plan TEXT,
            amount TEXT,
            currency TEXT,
            event_type TEXT,
            status TEXT,
            product_id TEXT,
            offer_id TEXT,
            payload TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def record_sqlite(event: dict[str, Any], status: str, payload: dict[str, Any]) -> bool:
    conn = sqlite_conn()
    try:
        existing = conn.execute(
            "SELECT 1 FROM payments WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO payments (
                id, event_id, contract_id, email, plan, amount, currency,
                event_type, status, product_id, offer_id, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                event["event_id"],
                event["contract_id"],
                event["email"],
                event["plan"],
                event["amount"],
                event["currency"],
                event["event_type"],
                status,
                event["product_id"],
                event["offer_id"],
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


CREDIT_STATEMENTS = (
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
    """,
    "CREATE INDEX IF NOT EXISTS audit_credits_email_idx ON audit_credits (email)",
)


def ensure_credit_tables(cur) -> None:
    for statement in CREDIT_STATEMENTS:
        cur.execute(statement)


def grant_project_slots(cur, event: dict[str, Any]) -> None:
    slots = PLAN_SLOTS.get(event.get("plan") or "")
    email = (event.get("email") or "").strip().lower()
    event_id = event.get("event_id")
    if not slots or not email or not event_id:
        return
    ensure_credit_tables(cur)
    cur.execute(
        """
        INSERT INTO audit_credits (
            id, email, payment_event_id, contract_id, plan, granted, remaining
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (payment_event_id) DO NOTHING
        """,
        (
            str(uuid.uuid4()),
            email,
            event_id,
            event.get("contract_id"),
            event.get("plan"),
            slots,
            slots,
        ),
    )


def revoke_project_slots(cur, event: dict[str, Any]) -> None:
    email = (event.get("email") or "").strip().lower()
    if not email:
        return
    ensure_credit_tables(cur)
    contract_id = event.get("contract_id")
    if contract_id:
        cur.execute(
            "UPDATE audit_credits SET remaining = 0 WHERE email = %s AND contract_id = %s",
            (email, contract_id),
        )
        return
    cur.execute(
        """
        UPDATE audit_credits SET remaining = 0
        WHERE id = (
            SELECT id FROM audit_credits
            WHERE email = %s AND remaining > 0
            ORDER BY created_at DESC
            LIMIT 1
        )
        """,
        (email,),
    )


def mark_project_paid(cur, event: dict[str, Any], status: str) -> None:
    """Не роняет вебхук, если таблицы projects в Supabase ещё нет."""
    paid = status == "paid" and event["plan"] in PAID_PLANS
    if not event["email"] or status not in {"paid", "revoked"} or (status == "paid" and not paid):
        return
    flag = "TRUE" if paid else "FALSE"
    cur.execute("SAVEPOINT project_paid")
    try:
        cur.execute(
            f"UPDATE projects SET paid = {flag} WHERE lower(email) = %s",
            (event["email"],),
        )
        cur.execute("RELEASE SAVEPOINT project_paid")
    except Exception as error:
        log.warning("Не удалось обновить projects.paid для %s: %s", event["email"], error)
        cur.execute("ROLLBACK TO SAVEPOINT project_paid")


def record_postgres(event: dict[str, Any], status: str, payload: dict[str, Any]) -> bool:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return True
    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError:
        log.warning("psycopg не установлен, Postgres пропущен")
        return True

    inserted = True
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    event_id TEXT UNIQUE,
                    contract_id TEXT,
                    email TEXT,
                    plan TEXT,
                    amount TEXT,
                    currency TEXT,
                    event_type TEXT,
                    status TEXT,
                    product_id TEXT,
                    offer_id TEXT,
                    payload JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("SELECT 1 FROM payments WHERE event_id = %s", (event["event_id"],))
            if cur.fetchone():
                inserted = False
            else:
                cur.execute(
                    """
                    INSERT INTO payments (
                        id, event_id, contract_id, email, plan, amount, currency,
                        event_type, status, product_id, offer_id, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        event["event_id"],
                        event["contract_id"],
                        event["email"],
                        event["plan"],
                        event["amount"],
                        event["currency"],
                        event["event_type"],
                        status,
                        event["product_id"],
                        event["offer_id"],
                        Json(payload),
                    ),
                )
            mark_project_paid(cur, event, status)
            if inserted and status == "paid":
                grant_project_slots(cur, event)
            elif status == "revoked":
                revoke_project_slots(cur, event)
        conn.commit()
    return inserted


@router.post("/api/webhook/lava")
async def lava_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    authenticate(request, raw_body)

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Некорректный JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")

    event = parse_event(payload)
    event_type = event["event_type"]

    if event_type in SUCCESS_EVENTS:
        status = "paid"
    elif event_type in REVOKE_EVENTS:
        status = "revoked"
    elif event_type == "payment.failed":
        status = "failed"
    else:
        log.info("Пропущено событие Lava: %s", event_type or "unknown")
        return {"ok": True, "ignored": True, "event_type": event_type or None}

    if status == "paid" and not event["plan"]:
        log.warning("Оплата без известного тарифа: product=%s offer=%s", event["product_name"], event["offer_id"])

    sqlite_new = record_sqlite(event, status, payload)
    pg_new = record_postgres(event, status, payload)
    created = sqlite_new and pg_new

    log.info(
        "Lava %s plan=%s email=%s contract=%s duplicate=%s",
        event_type,
        event["plan"],
        event["email"],
        event["contract_id"],
        not created,
    )
    return {
        "ok": True,
        "duplicate": not created,
        "status": status,
        "plan": event["plan"],
        "plan_label": PLAN_LABELS.get(event["plan"] or "", event["plan"]),
        "email": event["email"],
        "contract_id": event["contract_id"],
    }


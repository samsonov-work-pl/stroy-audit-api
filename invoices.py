"""Создание инвойсов Lava.top через API: POST /api/invoice/lava.

Вебхуки Lava приходят только по оплатам, созданным через API (Custom Price
Invoice). Виджет и прямые ссылки платформы вебхуков не шлют — поэтому оплата
сайта переводится с виджетов на инвойсы: сервер сам создаёт счёт с email
покупателя, ценой тарифа и адресом возврата, покупатель платит на странице
Lava.top и возвращается на ?payment=success, а вебхук payment.success
начисляет кредиты в Postgres.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import uuid
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from audit_flow import SlidingWindowLimiter

log = logging.getLogger("lava-invoices")

INVOICE_URL = "https://gate.lava.top/api/v3/invoice"
PRODUCTS_URL = "https://gate.lava.top/api/v1/products"

# Сумма задаётся на сервере: клиент не может повлиять на цену.
PLAN_PRICES = {"full": 990, "choice": 1990, "business": 4990}
CURRENCY = "RUB"

# offerId продуктов кабинета. Продукт должен быть опубликован с признаком
# «Цена по запросу API». Env-переменные перекрывают значения по умолчанию.
OFFER_ENV = {
    "full": "LAVA_OFFER_FULL",
    "choice": "LAVA_OFFER_CHOICE",
    "business": "LAVA_OFFER_BUSINESS",
}
OFFER_FALLBACK = {
    "full": "897a9501-3853-49cd-b22c-1cada7b220ba",
    "choice": "5cf765a3-e5b3-4d79-8f4c-2f603aebf703",
    "business": "98783786-61c8-4f37-aa28-ffc1a4477c94",
}

SITE_URL = (os.getenv("SITE_URL") or "https://stroyaudit-ai.com").rstrip("/")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

INVOICE_LIMITER = SlidingWindowLimiter(limit=10, window_seconds=600)

LAVA_INVOICES_TABLE = """
    CREATE TABLE IF NOT EXISTS lava_invoices (
        invoice_id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        plan TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        currency TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

router = APIRouter()


def lava_api_key() -> str:
    """Ключ из кабинета Lava (Интеграции → API → Создать API key)."""
    key = (os.getenv("LAVA_API_KEY") or "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="LAVA_API_KEY не задан в окружении.")
    return key


def offer_for(plan: str) -> str | None:
    env_name = OFFER_ENV.get(plan)
    from_env = (os.getenv(env_name) or "").strip() if env_name else ""
    return from_env or OFFER_FALLBACK.get(plan)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def pick_field(data: Any, *keys: str) -> str | None:
    """Достаёт строковое значение, в том числе из вложенного объекта data."""
    for candidate in (data, data.get("data") if isinstance(data, dict) else None):
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def record_invoice_intent(invoice_id: str, email: str, plan: str, amount: int) -> None:
    """Запоминает намерение оплаты: вебхук возьмёт отсюда email и тариф,
    если его payload окажется без них (страховка от смены формата Lava)."""
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn or not invoice_id:
        return
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg не установлен: намерение инвойса не записано")
        return
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(LAVA_INVOICES_TABLE)
                cur.execute(
                    """
                    INSERT INTO lava_invoices (invoice_id, email, plan, amount, currency)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (invoice_id) DO NOTHING
                    """,
                    (invoice_id, email, plan, amount, CURRENCY),
                )
            conn.commit()
    except Exception:
        log.exception("Не удалось записать намерение инвойса %s", invoice_id)


@router.post("/api/invoice/lava")
async def create_lava_invoice(request: Request):
    """Создаёт счёт Lava на оплату тарифа и отдаёт ссылку на оплату."""
    if not INVOICE_LIMITER.allow(client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"detail": "Слишком много запросов. Подождите пару минут и попробуйте снова."},
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидался JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидался объект JSON.")

    email = str(body.get("email") or "").strip().lower()
    plan = str(body.get("tier") or body.get("plan") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Укажите корректный email — на него привяжутся оплаченные проекты.")
    if plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Неизвестный тариф.")
    offer = offer_for(plan)
    if not offer:
        raise HTTPException(status_code=500, detail=f"Не настроен offerId для тарифа {plan} ({OFFER_ENV[plan]}).")

    payload = {
        "email": email,
        "offerId": offer,
        "currency": CURRENCY,
        "amount": PLAN_PRICES[plan],
        "successful_return_url": f"{SITE_URL}/?payment=success&tier={plan}",
        "failure_return_url": f"{SITE_URL}/?payment=failed",
        "cancel_return_url": f"{SITE_URL}/?payment=cancelled",
    }
    try:
        response = requests.post(
            INVOICE_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "X-Api-Key": lava_api_key(),
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except requests.RequestException:
        log.exception("Lava не отвечает на создание инвойса")
        raise HTTPException(status_code=502, detail="Lava.top не отвечает. Попробуйте ещё раз через минуту.") from None

    if response.status_code >= 400:
        log.error("Lava отклонила инвойс: %s %s", response.status_code, response.text[:500])
        try:
            error_data = response.json()
        except ValueError:
            error_data = {}
        message = pick_field(error_data, "message", "error", "detail") or response.text[:200]
        raise HTTPException(
            status_code=502,
            detail=f"Lava.top не создал счёт: {message}",
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Lava.top вернул нечитаемый ответ.") from None

    invoice_id = pick_field(data, "id", "invoiceId", "invoice_id", "contractId", "contract_id")
    payment_url = pick_field(data, "paymentUrl", "payment_url", "url", "checkoutUrl")
    if not payment_url:
        log.error("Lava не вернула ссылку на оплату: %s", str(data)[:500])
        raise HTTPException(status_code=502, detail="Lava.top не вернул ссылку на оплату.")

    record_invoice_intent(invoice_id or str(uuid.uuid4()), email, plan, PLAN_PRICES[plan])

    log.info(
        "Lava invoice created plan=%s email=%s amount=%s invoice=%s",
        plan,
        email,
        PLAN_PRICES[plan],
        invoice_id,
    )
    return {
        "ok": True,
        "invoiceId": invoice_id,
        "paymentUrl": payment_url,
        "plan": plan,
        "amount": PLAN_PRICES[plan],
        "currency": CURRENCY,
    }


@router.get("/api/invoice/lava/products")
def lava_products(request: Request):
    """Диагностика: показывает продукты и их offerId, чтобы подставить
    правильные значения в LAVA_OFFER_*. Защищён ключом вебхука."""
    secret = (os.getenv("LAVA_WEBHOOK_SECRET") or "").strip()
    received = request.headers.get("x-api-key") or request.headers.get("X-Api-Key") or ""
    if not secret or not hmac.compare_digest(received.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Неверный ключ.")
    try:
        response = requests.get(
            PRODUCTS_URL,
            headers={"Accept": "application/json", "X-Api-Key": lava_api_key()},
            timeout=20,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Lava.top не отвечает.") from None
    try:
        return JSONResponse(status_code=response.status_code, content=response.json())
    except ValueError:
        return JSONResponse(status_code=response.status_code, content={"raw": response.text[:2000]})
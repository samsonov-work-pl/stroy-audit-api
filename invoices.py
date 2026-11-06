"""Создание инвойсов Lava.top через API: POST /api/invoice/lava.

Вебхуки Lava приходят только по оплатам, созданным через API (Custom Price
Invoice). Виджет и прямые ссылки платформы вебхуков не шлют — поэтому оплата
сайта переводится с виджетов на инвойсы: сервер сам создаёт счёт с email
покупателя, ценой тарифа и адресом возврата, покупатель платит на странице
Lava.top и возвращается на ?payment=success, а вебхук payment.success
начисляет кредиты в Postgres.

POST /api/invoice/lava/verify — страховка от потерянного вебхука: после
возврата покупателя сервер сам спрашивает Lava о счёте
(GET /api/v1/invoices/{id}) и, если счёт оплачен, начисляет кредиты.
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
from payments import ensure_credit_tables, record_postgres, record_sqlite

log = logging.getLogger("lava-invoices")

INVOICE_URL = "https://gate.lava.top/api/v3/invoice"
PRODUCTS_URL = "https://gate.lava.top/api/v1/products"

# Сумма задаётся на сервере: клиент не может повлиять на цену.
PLAN_PRICES = {"full": 990, "choice": 1990, "business": 4990}
# Для тестовых оплат сумму и валюту можно переопределить переменными окружения
# LAVA_PRICE_FULL / LAVA_PRICE_CHOICE / LAVA_PRICE_BUSINESS и LAVA_CURRENCY,
# не трогая код. После теста переменные удалить — вернутся базовые цены в рублях.
CURRENCY = (os.getenv("LAVA_CURRENCY") or "RUB").strip().upper()

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


def plan_amount(plan: str) -> int | float:
    """Базовая цена тарифа; env LAVA_PRICE_<PLAN> перекрывает (для тестов)."""
    env_name = f"LAVA_PRICE_{plan.upper()}"
    raw = (os.getenv(env_name) or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return int(value) if value.is_integer() else value
            log.warning("%s=%r неположительна, использую базовую цену", env_name, raw)
        except ValueError:
            log.warning("%s=%r не число, использую базовую цену", env_name, raw)
    return PLAN_PRICES[plan]


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


def record_invoice_intent(invoice_id: str, email: str, plan: str, amount: int | float) -> None:
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
    amount = plan_amount(plan)

    payload = {
        "email": email,
        "offerId": offer,
        "currency": CURRENCY,
        "amount": amount,
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

    record_invoice_intent(invoice_id or str(uuid.uuid4()), email, plan, amount)

    log.info(
        "Lava invoice created plan=%s email=%s amount=%s currency=%s invoice=%s",
        plan,
        email,
        amount,
        CURRENCY,
        invoice_id,
    )
    return {
        "ok": True,
        "invoiceId": invoice_id,
        "paymentUrl": payment_url,
        "plan": plan,
        "amount": amount,
        "currency": CURRENCY,
    }


INVOICE_STATUS_URL = "https://gate.lava.top/api/v1/invoices/{invoice_id}"
# Статусы оплаченного счёта в ответе GET /api/v1/invoices/{id}. Lava в разных
# местах дока пишет по-разному, поэтому принимаем все известные варианты.
PAID_INVOICE_STATUSES = {"success", "paid", "succeeded", "completed"}


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def find_status_value(value: Any, depth: int = 0) -> str | None:
    """Ищет статус счёта в ответе Lava: формат ответа нестабилен, поэтому
    проходим по вложенным объектам и берём первый похожий ключ."""
    if depth > 3 or not isinstance(value, dict):
        return None
    for key in ("status", "state", "invoice_status", "invoiceStatus"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for child in value.values():
        found = find_status_value(child, depth + 1)
        if found:
            return found
    return None


def stored_invoice(invoice_id: str) -> dict[str, Any] | None:
    """Email и тариф из lava_invoices: счёт создан нашим сервером, поэтому
    там есть всё, чего может не быть в ответе Lava."""
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg не установлен: данные счёта %s недоступны", invoice_id)
        return None
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(LAVA_INVOICES_TABLE)
                cur.execute(
                    "SELECT email, plan, amount, currency FROM lava_invoices WHERE invoice_id = %s",
                    (invoice_id,),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception:
        log.exception("Не удалось прочитать lava_invoices для счёта %s", invoice_id)
        return None
    if not row:
        return None
    return {
        "email": str(row[0]).strip().lower(),
        "plan": str(row[1]).strip().lower(),
        "amount": row[2],
        "currency": str(row[3]).strip().upper(),
    }


@router.post("/api/invoice/lava/verify")
async def verify_lava_invoice(request: Request):
    """Возврат покупателя с Lava: сверяем счёт напрямую в Lava API и, если он
    оплачен, начисляем кредиты. Страховка от потерянного/несуществующего
    вебхука: источник истины — GET /api/v1/invoices/{id}, а не запрос клиента.
    Начисление идемпотентно: event_id "invoice:<id>" + contract_id счёта."""
    if not INVOICE_LIMITER.allow(client_ip(request)):
        return JSONResponse(
            status_code=429,
            content={"ok": False, "reason": "rate_limited", "detail": "Слишком много запросов. Подождите пару минут."},
        )

    try:
        body = await request.json()
    except Exception:
        body = None
    invoice_id = ""
    if isinstance(body, dict):
        raw = body.get("invoiceId") or body.get("invoice_id")
        if isinstance(raw, str):
            invoice_id = raw.strip()
    if not invoice_id or not is_uuid(invoice_id):
        return {"ok": False, "reason": "bad_invoice_id"}

    try:
        response = requests.get(
            INVOICE_STATUS_URL.format(invoice_id=invoice_id),
            headers={"Accept": "application/json", "X-Api-Key": lava_api_key()},
            timeout=20,
        )
    except requests.RequestException:
        log.exception("Lava не отвечает на запрос счёта %s", invoice_id)
        return {"ok": False, "reason": "lava_unavailable"}
    if response.status_code == 404:
        return {"ok": False, "reason": "invoice_not_found"}
    if response.status_code >= 400:
        log.error("Lava вернула %s на запрос счёта %s: %s", response.status_code, invoice_id, response.text[:300])
        return {"ok": False, "reason": "lava_error", "lava_status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        log.error("Lava вернула не-JSON по счёту %s: %s", invoice_id, response.text[:300])
        return {"ok": False, "reason": "lava_bad_response"}

    status = (find_status_value(data) or "").lower()
    if status not in PAID_INVOICE_STATUSES:
        log.info("Счёт %s ещё не оплачен: status=%r", invoice_id, status or None)
        return {"ok": False, "reason": "not_paid", "status": status or None}

    stored = stored_invoice(invoice_id)
    if not stored:
        # Счёт не создан нашим сервером (или БД недоступна): email и тариф
        # взять неоткуда, кредить нельзя.
        log.warning("Оплачен неизвестный счёт %s — нет записи в lava_invoices", invoice_id)
        return {"ok": False, "reason": "unknown_invoice"}

    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if dsn:
        try:
            import psycopg
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    ensure_credit_tables(cur)
                    cur.execute(
                        "SELECT 1 FROM audit_credits WHERE contract_id = %s LIMIT 1",
                        (invoice_id,),
                    )
                    if cur.fetchone():
                        log.info("Кредит по счёту %s уже начислен", invoice_id)
                        return {"ok": True, "duplicate": True, "email": stored["email"], "plan": stored["plan"]}
                conn.commit()
        except Exception:
            # Не блокируем начисление: grant_project_slots сам проверит contract_id.
            log.exception("Не удалось проверить начисление по счёту %s", invoice_id)

    event = {
        "event_id": f"invoice:{invoice_id}",
        "event_type": "payment.success",
        "contract_id": invoice_id,
        "email": stored["email"],
        "plan": stored["plan"],
        "amount": str(stored["amount"]),
        "currency": stored["currency"],
        "offer_id": None,
        "product_id": None,
        "product_name": None,
    }
    payload = {"invoice_id": invoice_id, "status": status, "source": "verify"}
    record_sqlite(event, "paid", payload)
    inserted = record_postgres(event, "paid", payload)
    log.info(
        "Verify счёта %s: email=%s plan=%s duplicate=%s",
        invoice_id,
        stored["email"],
        stored["plan"],
        not inserted,
    )
    return {"ok": True, "duplicate": not inserted, "email": stored["email"], "plan": stored["plan"]}


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
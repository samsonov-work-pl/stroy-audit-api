"""Подписка «Для застройщиков»: email + contract id Lava.top, без аккаунта."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

log = logging.getLogger("subscriptions")

STATUSES = {"active", "cancelled", "expired"}
PERIOD = timedelta(days=30)
MIGRATION = Path(__file__).with_name("migrations") / "001_subscriptions.sql"

RECEIPT_KEYS = (
    "receiptUrl",
    "receipt_url",
    "receipt.url",
    "fiscalReceiptUrl",
    "chequeUrl",
    "receiptLink",
)
MANAGE_KEYS = (
    "manageUrl",
    "managementUrl",
    "subscriptionManagementUrl",
    "subscriptionUrl",
    "cancelUrl",
    "clientUrl",
    "buyer.subscriptionUrl",
    "subscription.manageUrl",
)
PERIOD_KEYS = (
    "willExpireAt",
    "expiredAt",
    "expireAt",
    "nextPaymentAt",
    "currentPeriodEnd",
    "subscription.willExpireAt",
    "subscription.expiredAt",
)
SUBSCRIPTION_KEYS = (
    "parentContractId",
    "parent_contract_id",
    "subscriptionId",
    "subscription_id",
    "subscription.id",
)


def database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def https_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("https://"):
        return text
    return None


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def lookup(payload: dict[str, Any], data: dict[str, Any], *keys: str) -> Any:
    for source in (payload, data):
        for key in keys:
            current: Any = source
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


def subscription_fields(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    subscription_id = lookup(payload, data, *SUBSCRIPTION_KEYS) or event.get("contract_id")
    if isinstance(subscription_id, str):
        subscription_id = subscription_id.strip() or None
    period_end = parse_datetime(lookup(payload, data, *PERIOD_KEYS))
    receipt_url = https_url(lookup(payload, data, *RECEIPT_KEYS))
    manage_url = https_url(lookup(payload, data, *MANAGE_KEYS))
    cancelled_flag = lookup(payload, data, "subscription_cancelled", "subscriptionCancelled")
    return {
        "subscription_id": subscription_id,
        "period_end": period_end,
        "receipt_url": receipt_url,
        "manage_url": manage_url,
        "cancelled_with_refund": cancelled_flag is True,
    }


def concerns_subscription(event: dict[str, Any], fields: dict[str, Any]) -> bool:
    event_type = event.get("event_type") or ""
    if event.get("plan") == "developers":
        return True
    if event_type.startswith("subscription."):
        return True
    return bool(fields["cancelled_with_refund"])


def next_status(event: dict[str, Any], payment_status: str, fields: dict[str, Any]) -> str | None:
    event_type = event.get("event_type") or ""
    if event_type == "subscription.cancelled" or fields["cancelled_with_refund"]:
        return "cancelled"
    if event_type == "subscription.recurring.payment.failed":
        return "expired"
    if payment_status == "paid" and event.get("plan") == "developers":
        return "active"
    return None


def ensure_table(cur) -> None:
    script = MIGRATION.read_text(encoding="utf-8")
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            cur.execute(sql)


def upsert(cur, event: dict[str, Any], status: str, fields: dict[str, Any]) -> str:
    """Возвращает inserted, updated или missing."""
    subscription_id = fields["subscription_id"]
    email = event.get("email")
    period_end = fields["period_end"] or (
        datetime.now(timezone.utc) + PERIOD if status == "active" else None
    )
    if subscription_id:
        cur.execute(
            "SELECT id, status FROM subscriptions WHERE subscription_id = %s",
            (subscription_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE subscriptions
                SET email = COALESCE(%s, email),
                    status = %s,
                    current_period_end = COALESCE(%s, current_period_end)
                WHERE subscription_id = %s
                """,
                (email, status, period_end, subscription_id),
            )
            return "updated"
        if status != "active" and email:
            cur.execute(
                """
                UPDATE subscriptions
                SET status = %s,
                    current_period_end = COALESCE(%s, current_period_end)
                WHERE email = %s AND status = 'active'
                """,
                (status, period_end, email),
            )
            if cur.rowcount:
                return "updated"
        if not email:
            return "missing"
        cur.execute(
            """
            INSERT INTO subscriptions (id, email, subscription_id, status, current_period_end)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), email, subscription_id, status, period_end),
        )
        return "inserted"
    if not email:
        return "missing"
    cur.execute(
        """
        UPDATE subscriptions
        SET status = %s,
            current_period_end = COALESCE(%s, current_period_end)
        WHERE email = %s AND status = 'active'
        """,
        (status, period_end, email),
    )
    return "updated" if cur.rowcount else "missing"


def apply(event: dict[str, Any], payment_status: str, payload: dict[str, Any]) -> str | None:
    """Пишет статус в Supabase. None — событие не про подписку."""
    fields = subscription_fields(event, payload)
    status = next_status(event, payment_status, fields)
    if status is None or not concerns_subscription(event, fields):
        return None
    dsn = database_url()
    if not dsn:
        log.warning("DATABASE_URL не задан, подписка не записана")
        return None
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg не установлен, подписка не записана")
        return None

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            result = upsert(cur, event, status, fields)
        conn.commit()
    log.info(
        "Подписка %s status=%s email=%s id=%s",
        result,
        status,
        event.get("email"),
        fields["subscription_id"],
    )
    return result


def cancel_by_support(email: str | None, subscription_id: str | None) -> int:
    dsn = database_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL не задан")
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("psycopg не установлен") from error

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            if subscription_id and email:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'cancelled'
                    WHERE subscription_id = %s AND email = %s AND status = 'active'
                    """,
                    (subscription_id, email),
                )
            elif subscription_id:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'cancelled'
                    WHERE subscription_id = %s AND status = 'active'
                    """,
                    (subscription_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'cancelled'
                    WHERE email = %s AND status = 'active'
                    """,
                    (email,),
                )
            updated = cur.rowcount
        conn.commit()
    return updated


def send_welcome(event: dict[str, Any], payload: dict[str, Any]) -> bool:
    email = event.get("email")
    if not email:
        return False
    fields = subscription_fields(event, payload)
    lines = [
        "Подписка «Для застройщиков» оформлена.",
        "",
    ]
    if event.get("amount"):
        money = event["amount"]
        if event.get("currency"):
            money = f"{money} {event['currency']}"
        lines.append(f"Оплата: {money}.")
    if fields["subscription_id"]:
        lines.append(f"Идентификатор подписки: {fields['subscription_id']}.")
    lines.append("")
    if fields["receipt_url"]:
        lines.append(f"Чек: {fields['receipt_url']}")
    else:
        lines.append("Чек приходит в письме платёжного сервиса Lava.top на этот адрес.")
    if fields["manage_url"]:
        lines.append(f"Управление подпиской: {fields['manage_url']}")
    else:
        lines.append(
            "Ссылка на управление подпиской приходит в письме Lava.top при первой оплате."
        )
    lines.extend(
        [
            "",
            "Отменить автопродление без личного кабинета можно не позднее чем за 24 часа до списания:",
            "по ссылке из письма Lava.top или письмом на stroyaudit.ai@gmail.com с этого адреса.",
        ]
    )
    return _resend(email, "Подписка «Для застройщиков» — СтройАудит.ai", "\n".join(lines))


def _resend(to: str, subject: str, text: str) -> bool:
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        log.warning("RESEND_API_KEY не задан, письмо о подписке не отправлено")
        return False
    sender = (os.getenv("EMAIL_FROM") or "СтройАудит.ai <reports@stroyaudit.ai>").strip()
    body = json.dumps(
        {"from": sender, "to": [to], "subject": subject, "text": text},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urlrequest.Request(
        "https://api.resend.com/emails",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=20) as response:
            return 200 <= response.status < 300
    except urlerror.URLError:
        log.exception("Resend не принял письмо о подписке")
        return False

"""API аудита СтройАудит.ai: экспресс- и глубокий аудит, вебхуки Lava.top.

Оплата глубокого аудита проверяется на сервере: сайт выдаёт одноразовый
подписанный тикет, API погашает его и сам списывает кредит.
"""

import base64
import logging
import os

import requests
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from audit_flow import (
    SlidingWindowLimiter,
    consume_credits,
    consume_ticket_once,
    restore_credit,
    validate_upload,
    verify_ticket,
)
from payments import router as payments_router
from invoices import router as invoices_router
from admin_grant import router as admin_grant_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stroy-audit-api")

app = FastAPI()

# Прямые запросы из браузера идут только с наших доменов.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://stroyaudit-ai.com",
        "https://stroy-audit-web.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(payments_router)
app.include_router(invoices_router)
app.include_router(admin_grant_router)


@app.get("/")
def health_check():
    return {"status": "online", "service": "stroy-audit-api"}


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
EXPRESS_MODEL = "google/gemini-2.5-flash"
DEEP_MODEL = "google/gemini-2.5-pro"

EXPRESS_LIMITER = SlidingWindowLimiter(limit=5, window_seconds=600)
DEEP_LIMITER = SlidingWindowLimiter(limit=4, window_seconds=600)

# Сколько файлов одного проекта можно прикрепить к одному глубокому запуску.
TIER_MAX_FILES = {"full": 1, "choice": 3, "business": 10}

# Логирование согласий с дисклеймером в Google-таблицу (вебхук Apps Script).
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbzBeIqxEy3j38ZjsEvg31NRdPW36qZ4trttabPISAo-uVYDdAZvAcEZrr8eypMycmEiwQ/exec"


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def rate_limited(limiter: SlidingWindowLimiter, request: Request) -> JSONResponse | None:
    if limiter.allow(client_ip(request)):
        return None
    return JSONResponse(
        status_code=429,
        content={"detail": "Слишком много запросов. Подождите пару минут и попробуйте снова."},
    )


@lru_cache(maxsize=16)
def load_prompt_from_github(jurisdiction: str, audit_type: str = "express") -> str:
    clean_jurisdiction = jurisdiction.strip().lower()
    allowed_files = ["belarus", "russia", "kazakhstan", "eurocodes"]
    if clean_jurisdiction not in allowed_files:
        clean_jurisdiction = "russia"
        
    github_url = f"https://raw.githubusercontent.com/samsonov-work-pl/stroy-audit-api/main/{clean_jurisdiction}.md"
    
    try:
        response = requests.get(github_url, timeout=5)
        if response.status_code == 200:
            base_prompt = response.text
            
            # Если это экспресс-аудит, дополняем Pro-шаблон инструкцией для распределения замечаний
            if audit_type == "express":
                express_modifier = (
                    "\n\n--- СТРОГИЕ ИНСТРУКЦИИ ДЛЯ ЭКСПРЕСС-РЕЖИМА --\n"
                    "Обязательно распределяй найденные замечания по всем разделам отчета, чтобы счетчики в интерфейсе были заполнены:\n"
                    "1. В раздел '🔴 2. Критически важное' помести самые опасные несоответствия (например, сейсмика, общие нормы).\n"
                    "2. В раздел '🟡 3. Спорные решения' (Важно) обязательно помести 1-2 замечания по материалам, конструкциям или узлам (например, фундамент, утеплитель, перекрытия).\n"
                    "3. В раздел '🔵 4. Нужно уточнить' (Рекомендации) помести замечания по неполноте данных и добавь фразу: 'Рекомендуется выполнить полный аудит с добавлением файлов по смежным разделам (Генеральный план, ОВиК, ВК, ЭОМ) при их наличии'.\n"
                    "4. В конце добавь блок о том, что полная таблица рисков и официальный PDF-отчет доступны после разблокировки Pro-версии.\n"
                )
                return base_prompt + express_modifier
                
            return base_prompt
            
    except Exception as e:
        print(f"Ошибка загрузки промта с GitHub: {e}")
        
    return "Проведи строительный экспресс-аудит проекта."


COMPARISON_REPORT_ADDENDUM = (
    "\n\n--- ТАРИФЫ «ВЫБОР ЗАСТРОЙЩИКА» (3 ПРОЕКТА) И «ПРОФИ / БИЗНЕС» (10 ПРОЕКТОВ) ---\n"
    "⚠ ВАЖНО: ЭТОТ РАЗДЕЛ ОБЯЗАТЕЛЕН В КАЖДОМ ОТЧЁТЕ ДАННОГО ТАРИФА. НИКОГДА НЕ ПРОПУСКАЙ ЕГО И НЕ ОПРАВДЫВАЙ ОТСУТСТВИЕ ТЕМ, ЧТО СРАВНИВАТЬ НЕЧЕГО. ЕСЛИ В ФАЙЛЕ НЕТ СМЕТЫ И ДЕНЕЖНЫХ СУММ, СРАВНИВАЙ КАЧЕСТВЕННО (РИСК АВАРИИ, ПЕРЕДЕЛКА, ОТКАЗ В СОГЛАСОВАНИИ, ЛИШНЯЯ РАБОТА) И ПИШИ «СУММА В ФАЙЛЕ НЕ УКАЗАНА».\n"
    "К стандартной структуре отчёта добавь новый раздел. Вставь его после раздела "
    "«📊 5. Инженерно-техническая таблица рисков» и до раздела «💡 6. Выводы аудита». "
    "Если для тарифа есть ещё дополнительные разделы, этот раздел идёт первым среди них. "
    "Нумерацию разделов 1–6 не меняй. Заголовок пиши дословно:\n"
    "### ⚖️ Сравнение рисков и переплат\n"
    "Сравни найденные риски по цене последствий и по переплате в смете. Оформи таблицей:\n"
    "| Риск или узел | Чем грозит | Переплата или экономия | Что дешевле исправить сейчас |\n"
    "Правила:\n"
    "- Сравнивай только то, что есть в загруженном файле: замечания между собой, сметные позиции с чертежами и спецификациями. Если в файле несколько вариантов проекта или несколько смет, сравни эти варианты между собой.\n"
    "- Для каждой строки укажи, откуда взята сумма: позиция сметы, объём на чертеже или пометка, что числа в файле нет.\n"
    "- Не выдумывай цены, объёмы и вторые проекты, которых нет в файле. Если суммы нет, напиши «сумма в файле не указана» и сравни риск качественно: авария, переделка, отказ в согласовании, лишняя работа.\n"
    "- В конце раздела одной фразой назови, какой риск дешевле снять до стройки и какой даёт наибольшую переплату.\n"
    "Строк таблицы — не менее трёх, даже если сравнение качественное.\n"
    "Этот раздел не заменяет разделы 2–6.\n"
)


BUSINESS_REPORT_ADDENDUM = (
    "\n\n--- ТАРИФ «ПРОФИ / БИЗНЕС» (10 ПРОЕКТОВ) ---\n"
    "⚠ ВАЖНО: ЭТОТ РАЗДЕЛ ОБЯЗАТЕЛЕН В КАЖДОМ ОТЧЁТЕ ДАННОГО ТАРИФА. НИКОГДА НЕ ПРОПУСКАЙ ЕГО, ДАЖЕ ЕСЛИ ЗАГРУЖЕННЫЙ ФАЙЛ НЕБОЛЬШОЙ ИЛИ ЗАМЕЧАНИЙ МАЛО. В КАЖДОМ ПОДРАЗДЕЛЕ МИНИМУМ ТРИ ПУНКТА ПО ЗАГРУЖЕННЫМ МАТЕРИАЛАМ.\n"
    "К стандартной структуре отчёта добавь новый раздел. Вставь его после раздела "
    "«⚖️ Сравнение рисков и переплат» и до раздела «💡 6. Выводы аудита». "
    "Нумерацию разделов 1–6 не меняй. Заголовок раздела пиши дословно:\n"
    "### 🛠️ Рекомендации для строителей и проектировщиков\n"
    "Внутри раздела сделай два подраздела и не оставляй их пустыми:\n"
    "#### Для проектировщиков\n"
    "- Что исправить в чертежах, расчётах, узлах, спецификациях и смете по замечаниям этого отчёта.\n"
    "- Для каждого пункта укажи лист или узел, если он читается в файле, и норму, на которую опирается правка.\n"
    "#### Для строителей\n"
    "- Что проверить на площадке до закупки и до закрытия работ.\n"
    "- Какие узлы контролировать и какой риск брака, удорожания или срыва сроков следует из найденных замечаний.\n"
    "Пиши только по загруженным материалам. Не добавляй общие советы, которых нет в файле. "
    "Не выдумывай марки, объёмы и пункты норм: если данных нет, так и напиши в этом пункте. "
    "Этот раздел не заменяет разделы 2–6.\n"
)


READABILITY_ADDENDUM = (
    "\n\n--- КАЧЕСТВО РАСПОЗНАВАНИЯ ФАЙЛА ---\n"
    "Сразу после раздела «💡 6. Выводы аудита», до финального дисклеймера, добавь служебный раздел. "
    "Заголовок пиши дословно:\n"
    "### 🔍 Качество распознавания файла\n"
    "Правила раздела:\n"
    "- Раздел выводи, только если часть информации в загруженном файле не удалось распознать: нечеткие линии, мелкий масштаб листа, низкое разрешение, рукописный текст, подписи и штампы, повреждённые страницы. Если весь файл читается нормально — раздел полностью пропусти, пустого заголовка не оставляй.\n"
    "- По каждому затронутому листу или странице укажи: что именно не распознано (размеры, узлы, текст пояснений, отметки) и почему (нечеткие линии, мелкий масштаб, печать поверх текста).\n"
    "- Не выдумывай содержимое нечитаемых фрагментов и не считай их нарушениями. Если из-за нечитаемого фрагмента не удалось проверить важный узел — отметь это в соответствующем замечании («фрагмент не читается, узел не проверен»).\n"
    "- Перечисляй только фрагменты, влияющие на выводы аудита, а не каждую размытую линию.\n"
    "- В конце раздела предложи два решения: прислать затронутый лист в более крупном масштабе или более высоком разрешении, либо обсудить детали в чате по проекту.\n"
    "Нумерацию разделов 1–6 не меняй; этот раздел без номера.\n"
)


def report_prompt(base_prompt: str, tier: str) -> str:
    paid = (tier or "").strip().lower()
    prompt = base_prompt + READABILITY_ADDENDUM
    if paid not in {"choice", "business"}:
        return prompt
    prompt += COMPARISON_REPORT_ADDENDUM
    if paid == "business":
        prompt += BUSINESS_REPORT_ADDENDUM
    return prompt


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 60 * 1024 * 1024


async def read_file_data_url(file: UploadFile) -> str:
    file_bytes = await file.read(MAX_FILE_BYTES + 1)
    if len(file_bytes) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="Файл больше 50 МБ. Разделите проект на части.")
    file_base64 = base64.b64encode(file_bytes).decode("utf-8")
    mime_type = file.content_type or "application/pdf"
    return f"data:{mime_type};base64,{file_base64}"


async def read_files_data_urls(files: list[UploadFile]) -> list[str]:
    """Читает все файлы запуска в data-URL; проверяет лимит каждого файла и суммарный размер."""
    data_urls: list[str] = []
    total_bytes = 0
    for upload in files:
        file_bytes = await upload.read(MAX_FILE_BYTES + 1)
        if len(file_bytes) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Файл «{upload.filename or 'без имени'}» больше 50 МБ. Разделите проект на части.",
            )
        total_bytes += len(file_bytes)
        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail="Суммарный размер файлов больше 60 МБ. Разделите запуск на несколько.",
            )
        file_base64 = base64.b64encode(file_bytes).decode("utf-8")
        mime_type = upload.content_type or "application/pdf"
        data_urls.append(f"data:{mime_type};base64,{file_base64}")
    return data_urls


def run_openrouter(prompt: str, file_data_urls: list[str], model: str) -> str:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for file_data_url in file_data_urls:
        content.append({"type": "image_url", "image_url": {"url": file_data_url}})
    ai_payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": content,
        }],
    }

    response = requests.post(
        OPENROUTER_URL,
        json=ai_payload,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        timeout=180,
    )
    response.raise_for_status()
    ai_data = response.json()
    report_text = ai_data["choices"][0]["message"].get("content") if "choices" in ai_data else None
    if not report_text:
        raise ValueError("OpenRouter не вернул текст отчёта.")
    return report_text


# 1. ЭНДПОИНТ ЭКСПРЕСС-АУДИТА (Flash)
@app.post("/api/audit/express")
async def run_express_audit(
    request: Request,
    email: str = Form(...), 
    file: UploadFile = File(...),
    consent_accepted: str = Form(...),
    consent_version: str = Form(default=""),
    jurisdiction: str = Form(default="russia")
):
    limited = rate_limited(EXPRESS_LIMITER, request)
    if limited:
        return limited
    if not str(consent_accepted).lower() in ("true", "1", "yes", "on"):
        raise HTTPException(status_code=400, detail="Необходимо согласие с дисклеймером.")

    # Записываем согласие в Google-таблицу; сбой лога не должен ломать аудит.
    if GOOGLE_SHEET_URL:
        try:
            requests.post(
                GOOGLE_SHEET_URL,
                json={
                    "email": email,
                    "consent_accepted": True,
                    "consent_version": consent_version,
                    "jurisdiction": jurisdiction,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                timeout=3,
            )
        except Exception as sheet_error:
            log.warning("Не удалось записать согласие в Google-таблицу: %s", sheet_error)

    problem = validate_upload(file)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    active_prompt = load_prompt_from_github(jurisdiction, audit_type="express")
    file_data_url = await read_file_data_url(file)
    active_prompt += f"\n\n--- ДАННЫЕ ЗАПУСКА ---\nИмя загруженного файла для строки «Файл:» в начале раздела 1: {file.filename or 'не указано'}.\nБольше никаких служебных данных этого запроса в отчет не выводи."
    try:
        report_text = run_openrouter(active_prompt, [file_data_url], EXPRESS_MODEL)
    except Exception as e:
        report_text = f"Ошибка экспресс-анализа: {e}"

    return {"status": "success", "tier": "express", "jurisdiction": jurisdiction, "report": report_text}


# 2. ЭНДПОИНТ ГЛУБОКОГО АУДИТА (Pro + PDF ссылка). Поддерживает несколько файлов одного проекта.
@app.post("/api/audit/deep")
async def run_deep_audit(
    request: Request,
    file: list[UploadFile] = File(...),
    jurisdiction: str = Form(default="russia"),
    ticket: str = Form(...),
):
    limited = rate_limited(DEEP_LIMITER, request)
    if limited:
        return limited
    uploads = [item for item in file if item is not None]
    if not uploads:
        raise HTTPException(status_code=400, detail="Прикрепите хотя бы один файл проекта.")
    for upload in uploads:
        problem = validate_upload(upload)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    payload = verify_ticket(ticket)
    if not payload:
        raise HTTPException(
            status_code=401,
            detail="Ссылка на запуск недействительна. Обновите страницу и попробуйте ещё раз.",
        )
    if not consume_ticket_once(payload["jti"]):
        raise HTTPException(status_code=409, detail="Этот запуск уже использован.")
    audit_email = payload["email"]
    audit_tier = payload["plan"]
    max_files = TIER_MAX_FILES.get(audit_tier, 1)
    if len(uploads) > max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Для этого тарифа можно прикрепить не более {max_files} файл(ов) за один запуск.",
        )

    # Читаем файлы до списания кредитов: проблема размера не должна стоить проектов.
    file_data_urls = await read_files_data_urls(uploads)
    file_count = len(file_data_urls)

    claims = consume_credits(audit_email, audit_tier, file_count)
    if not claims:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Для запуска с {file_count} файл(ами) нужно {file_count} оплаченных проект(ов). "
                "На этой почте их не хватает."
            ),
        )

    file_names = [upload.filename or "не указано" for upload in uploads]
    if len(file_names) == 1:
        files_line = f"Имя загруженного файла для строки «Файл:» в начале раздела 1: {file_names[0]}."
    else:
        files_line = (
            f"Файлов в запуске: {len(file_names)} — это разделы одного проекта. "
            f"Имена загруженных файлов для строки «Файл:» в начале раздела 1 (перечисли все): "
            + "; ".join(file_names)
            + "."
        )
    active_prompt = report_prompt(load_prompt_from_github(jurisdiction, audit_type="deep"), audit_tier)
    active_prompt += f"\n\n--- ДАННЫЕ ЗАПУСКА ---\n{files_line}\nБольше никаких служебных данных этого запроса в отчет не выводи."
    try:
        report_text = run_openrouter(active_prompt, file_data_urls, DEEP_MODEL)
    except Exception:
        log.exception("Глубокий аудит не удался")
        for claim in claims:
            restore_credit(claim["claim_id"])
        return JSONResponse(
            status_code=502,
            content={"detail": "Не удалось проанализировать проект. Оплаченные проекты возвращены на баланс — попробуйте позже."},
        )

    return {
        "status": "success", 
        "tier": "business" if audit_tier == "business" else "deep", 
        "plan": claims[0]["plan"],
        "jurisdiction": jurisdiction, 
        "report": report_text,
        "files": file_names,
        "credits_spent": file_count,
    }

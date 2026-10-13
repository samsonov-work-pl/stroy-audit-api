import base64
import os
import requests
from datetime import datetime
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GOOGLE_SHEET_URL = "ЗДЕСЬ_ВАША_ССЫЛКА_ОТ_GOOGLE"  # Ваша ссылка на гугл-таблицу

@app.post("/api/audit")
async def run_audit(
    email: str = Form(...), 
    file: UploadFile = File(...),
    # Принимаем consent_accepted как строку или булево, чтобы избежать ошибок парсинга:
    consent_accepted: str = Form(...),
    consent_version: str = Form(default="2026-10-12")
):
    # Превращаем пришедшее значение (строку или булево) в надежный Python boolean (True/False)
    if isinstance(consent_accepted, str):
        is_accepted = consent_accepted.lower() in ("true", "1", "yes", "on")
    else:
        is_accepted = bool(consent_accepted)

    # 1. Проверка согласия с дисклеймером
    if not is_accepted:
        raise HTTPException(status_code=400, detail="Необходимо согласие с условиями дисклеймера.")

    # 2. Отправка данных о согласии в Google Таблицу
    if GOOGLE_SHEET_URL and "macros/s" in GOOGLE_SHEET_URL:
        try:
            sheet_payload = {
                "email": email,
                "consent_accepted": is_accepted,
                "consent_version": consent_version
            }
            requests.post(GOOGLE_SHEET_URL, json=sheet_payload, timeout=5)
        except Exception as sheet_error:
            print(f"Ошибка отправки в Google Таблицу: {sheet_error}")

    # 3. Чтение файла и отправка в OpenRouter
    file_bytes = await file.read()
    file_base64 = base64.b64encode(file_bytes).decode("text/plain" if hasattr(base64, 'plain') else "utf-8") # стандартный кодек
    file_base64 = base64.b64encode(file_bytes).decode("utf-8")
    
    mime_type = file.content_type or "application/pdf"
    file_data_url = f"data:{mime_type};base64,{file_base64}"

    ai_headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    ai_payload = {
        "model": "google/gemini-2.5-pro",
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Проведи строительный экспресс-аудит проекта по нормам"
                        " Беларуси (СН и СП РБ). Найди базовые ошибки (мокрые зоны,"
                        " котельные, инсоляция)."
                    ),
                },
                {"type": "image_url", "image_url": {"url": file_data_url}},
            ],
        }],
    }

    try:
        ai_response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=ai_payload,
            headers=ai_headers,
            timeout=60,
        )
        ai_data = ai_response.json()

        if "choices" in ai_data and len(ai_data["choices"]) > 0:
            report_text = ai_data["choices"][0]["message"]["content"]
        else:
            report_text = f"Ошибка от OpenRouter: {str(ai_data)}"
    except Exception as e:
        report_text = f"Ошибка запроса к ИИ: {str(e)}"

    return {"status": "success", "report": report_text}

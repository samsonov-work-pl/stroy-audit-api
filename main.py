import base64
import os
import requests
from datetime import datetime
from functools import lru_cache
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

# Health-check эндпоинт для мгновенного прохождения проверки Render
@app.get("/")
def health_check():
    return {"status": "online", "service": "stroy-audit-api"}

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbzBeIqxEy3j38ZjsEvg31NRdPW36qZ4trttabPISAo-uVYDdAZvAcEZrr8eypMycmEiwQ/exec"

# Функция динамической загрузки промта из репозитория GitHub с кэшированием
@lru_cache(maxsize=16)
def load_prompt_from_github(jurisdiction: str) -> str:
    clean_jurisdiction = jurisdiction.strip().lower()
    
    allowed_files = ["belarus", "russia", "kazakhstan", "eurocodes"]
    if clean_jurisdiction not in allowed_files:
        clean_jurisdiction = "russia"  # Fallback по умолчанию
        
    github_url = f"https://raw.githubusercontent.com/samsonov-work-pl/stroy-audit-api/main/{clean_jurisdiction}.md"
    
    try:
        response = requests.get(github_url, timeout=5)
        if response.status_code == 200:
            return response.text
    except Exception as e:
        print(f"Ошибка загрузки промта с GitHub для {clean_jurisdiction}: {e}")
        
    return (
        "Проведи строительный экспресс-аудит проекта по нормам РФ (СП и СНиП). "
        "Найди базовые ошибки (мокрые зоны, котельные, инсоляция)."
    )

@app.post("/api/audit")
async def run_audit(
    email: str = Form(...), 
    file: UploadFile = File(...),
    consent_accepted: str = Form(...),
    consent_version: str = Form(default="2026-10-12"),
    jurisdiction: str = Form(default="russia")
):
    # 1. Проверка согласия
    if isinstance(consent_accepted, str):
        is_accepted = consent_accepted.lower() in ("true", "1", "yes", "on")
    else:
        is_accepted = bool(consent_accepted)

    if not is_accepted:
        raise HTTPException(status_code=400, detail="Необходимо согласие с условиями дисклеймера.")

    # 2. Отправка данных о согласии в Google Таблицу (некритично)
    if GOOGLE_SHEET_URL and "macros/s" in GOOGLE_SHEET_URL:
        try:
            sheet_payload = {
                "email": email,
                "consent_accepted": is_accepted,
                "consent_version": consent_version,
                "jurisdiction": jurisdiction,
                "timestamp": datetime.utcnow().isoformat()
            }
            requests.post(GOOGLE_SHEET_URL, json=sheet_payload, timeout=3)
        except Exception as sheet_error:
            print(f"Ошибка отправки в Google Таблицу: {sheet_error}")

    # 3. Загружаем промт с GitHub
    active_prompt = load_prompt_from_github(jurisdiction)

    # 4. Чтение файла и отправка в OpenRouter
    file_bytes = await file.read()
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
                    "text": active_prompt,
                },
                {"type": "image_url", "image_url": {"url": file_data_url}},
            ],
        }],
    }

    try:
        print(f"Отправка запроса в OpenRouter (юрисдикция: {jurisdiction})...")
        ai_response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=ai_payload,
            headers=ai_headers,
            timeout=120,  # Увеличенный таймаут для больших PDF
        )
        
        print(f"OpenRouter Status Code: {ai_response.status_code}")
        ai_data = ai_response.json()
        print(f"OpenRouter Response JSON: {ai_data}")

        if "choices" in ai_data and len(ai_data["choices"]) > 0:
            message_obj = ai_data["choices"][0]["message"]
            report_text = message_obj.get("content")
            
            # Защита от пустого ответа / тайм-аута рассуждений модели
            if not report_text:
                report_text = (
                    "⚠️ Превышено время ожидания ответа модели (тайм-аут генерации или скрытые рассуждения). "
                    "Попробуйте отправить файл меньшего размера или повторить запрос через минуту."
                )
        elif "error" in ai_data:
            err_msg = ai_data['error'].get('message', str(ai_data['error']))
            report_text = f"Ошибка OpenRouter API: {err_msg}"
        else:
            report_text = f"Неизвестный ответ от ИИ: {str(ai_data)}"
            
    except Exception as e:
        print(f"Критическая ошибка запроса к OpenRouter: {str(e)}")
        report_text = f"Ошибка запроса к ИИ: {str(e)}"

    return {"status": "success", "jurisdiction": jurisdiction, "report": report_text}

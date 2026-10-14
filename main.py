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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbzBeIqxEy3j38ZjsEvg31NRdPW36qZ4trttabPISAo-uVYDdAZvAcEZrr8eypMycmEiwQ/exec"

# Функция динамической загрузки промта из твоего репозитория GitHub с кэшированием
@lru_cache(maxsize=16)
def load_prompt_from_github(jurisdiction: str) -> str:
    # Очищаем название юрисдикции на всякий случай
    clean_jurisdiction = jurisdiction.strip().lower()
    
    # Допустимые варианты файлов в репозитории
    allowed_files = ["belarus", "russia", "kazakhstan", "eurocodes"]
    if clean_jurisdiction not in allowed_files:
        clean_jurisdiction = "belarus"  # Fallback по умолчанию
        
    github_url = f"https://raw.githubusercontent.com/samsonov-work-pl/stroy-audit-api/main/{clean_jurisdiction}.md"
    
    try:
        response = requests.get(github_url, timeout=5)
        if response.status_code == 200:
            return response.text
    except Exception as e:
        print(f"Ошибка загрузки промта с GitHub для {clean_jurisdiction}: {e}")
        
    # Жесткий Fallback, если GitHub недоступен
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
    # Принимаем юрисдикцию от фронтенда (по умолчанию Российская федирация)
    jurisdiction: str = Form(default="russia")
):
    # 1. Превращаем пришедшее значение согласия в надежный Python boolean
    if isinstance(consent_accepted, str):
        is_accepted = consent_accepted.lower() in ("true", "1", "yes", "on")
    else:
        is_accepted = bool(consent_accepted)

    if not is_accepted:
        raise HTTPException(status_code=400, detail="Необходимо согласие с условиями дисклеймера.")

    # 2. Отправка данных о согласии в Google Таблицу
    if GOOGLE_SHEET_URL and "macros/s" in GOOGLE_SHEET_URL:
        try:
            sheet_payload = {
                "email": email,
                "consent_accepted": is_accepted,
                "consent_version": consent_version,
                "jurisdiction": jurisdiction,
                "timestamp": datetime.utcnow().isoformat()
            }
            requests.post(GOOGLE_SHEET_URL, json=sheet_payload, timeout=5)
        except Exception as sheet_error:
            print(f"Ошибка отправки в Google Таблицу: {sheet_error}")

    # 3. Загружаем нужный промт с GitHub на основе выбранной юрисдикции
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
                    "text": active_prompt,  # Подставляем динамический промт с GitHub
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

    return {"status": "success", "jurisdiction": jurisdiction, "report": report_text}

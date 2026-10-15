import base64
import os
import requests
from datetime import datetime
from functools import lru_cache
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "online", "service": "stroy-audit-api"}

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GOOGLE_SHEET_URL = "https://script.google.com/macros/s/AKfycbzBeIqxEy3j38ZjsEvg31NRdPW36qZ4trttabPISAo-uVYDdAZvAcEZrr8eypMycmEiwQ/exec"

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
            
            # Если это экспресс-аудит, дополняем Pro-шаблон инструкцией для лид-магнита
            if audit_type == "express":
                express_modifier = (
                    "\n\n--- ИНСТРУКЦИЯ ДЛЯ ЭКСПРЕСС-РЕЖИМА (ЛИД-МАГНИТ) ---\n"
                    "Выдай подробный предварительный аудит:\n"
                    "1. Заполни разделы: краткое резюме, достоинства проекта и основные замечания.\n"
                    "2. Таблицу рисков заполни только для топ-3 главных нарушений, остальные строки скрой.\n"
                    "3. В конце добавь блок-напоминание о том, что полный глубокий анализ, расширенная таблица и официальный PDF-отчет доступны после разблокировки Pro-версии.\n"
                )
                return base_prompt + express_modifier
                
            return base_prompt
            
    except Exception as e:
        print(f"Ошибка загрузки промта с GitHub: {e}")
        
    return "Проведи строительный экспресс-аудит проекта."

# 1. ЭНДПОИНТ ЭКСПРЕСС-АУДИТА (Flash)
@app.post("/api/audit/express")
async def run_express_audit(
    email: str = Form(...), 
    file: UploadFile = File(...),
    consent_accepted: str = Form(...),
    jurisdiction: str = Form(default="russia")
):
    if not str(consent_accepted).lower() in ("true", "1", "yes", "on"):
        raise HTTPException(status_code=400, detail="Необходимо согласие с дисклеймером.")

    active_prompt = load_prompt_from_github(jurisdiction, audit_type="express")

    file_bytes = await file.read()
    file_base64 = base64.b64encode(file_bytes).decode("utf-8")
    mime_type = file.content_type or "application/pdf"
    file_data_url = f"data:{mime_type};base64,{file_base64}"

    ai_payload = {
        "model": "google/gemini-2.5-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": active_prompt},
                {"type": "image_url", "image_url": {"url": file_data_url}},
            ],
        }],
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=ai_payload,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            timeout=45,
        )
        ai_data = response.json()
        report_text = ai_data["choices"][0]["message"].get("content") if "choices" in ai_data else "Ошибка анализа."
    except Exception as e:
        report_text = f"Ошибка экспресс-анализа: {e}"

    return {"status": "success", "tier": "express", "jurisdiction": jurisdiction, "report": report_text}


# 2. ЭНДПОИНТ ГЛУБОКОГО АУДИТА (Pro + PDF ссылка)
@app.post("/api/audit/deep")
async def run_deep_audit(
    email: str = Form(...), 
    file: UploadFile = File(...),
    jurisdiction: str = Form(default="russia"),
    payment_verified: str = Form(default="true")
):
    active_prompt = load_prompt_from_github(jurisdiction, audit_type="deep")

    file_bytes = await file.read()
    file_base64 = base64.b64encode(file_bytes).decode("utf-8")
    mime_type = file.content_type or "application/pdf"
    file_data_url = f"data:{mime_type};base64,{file_base64}"

    ai_payload = {
        "model": "google/gemini-2.5-pro",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": active_prompt},
                {"type": "image_url", "image_url": {"url": file_data_url}},
            ],
        }],
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=ai_payload,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            timeout=180,
        )
        ai_data = response.json()
        report_text = ai_data["choices"][0]["message"].get("content") if "choices" in ai_data else "Ошибка глубокого анализа."
    except Exception as e:
        report_text = f"Ошибка глубокого анализа: {e}"

    return {
        "status": "success", 
        "tier": "deep", 
        "jurisdiction": jurisdiction, 
        "report": report_text,
        "pdf_url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
    }

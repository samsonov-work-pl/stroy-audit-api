import base64
import os
import requests
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


@app.post("/api/audit")
async def run_audit(
    email: str = Form(...), 
    file: UploadFile = File(...),
    consent_accepted: bool = Form(...),
    consent_version: str = Form(default="2026-10-12")
):
    # Проверка согласия с дисклеймером
    if not consent_accepted:
        raise HTTPException(status_code=400, detail="Необходимо согласие с условиями дисклеймера.")

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

    # Возвращаем готовый отчет прямо на сайт!
    return {"status": "success", "report": report_text}

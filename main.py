import os
import requests
from fastapi import FastAPI, File, Form, UploadFile
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
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "onboarding@resend.dev")


@app.post("/api/audit")
async def run_audit(email: str = Form(...), file: UploadFile = Form(...)):
  file_bytes = await file.read()

  # Запрос к Gemini через OpenRouter
  ai_headers = {
      "Authorization": f"Bearer {OPENROUTER_API_KEY}",
      "Content-Type": "application/json",
  }

  ai_payload = {
      "model": "google/gemini-1.5-pro",
      "messages": [
          {
              "role": "user",
              "content": (
                  "Проведи строительный экспресс-аудит проекта по нормам"
                  " Беларуси (СН и СП РБ). Найди базовые ошибки (мокрые зоны,"
                  " котельные, инсоляция)."
              ),
          }
      ],
  }

  ai_response = requests.post(
      "https://openrouter.ai/api/v1/chat/completions",
      json=ai_payload,
      headers=ai_headers,
  )
  ai_data = ai_response.json()
  report_text = (
      ai_data.get("choices", [{}])[0]
      .get("message", {})
      .get("content", "Не удалось сгенерировать отчет.")
  )

  # Отправка письма через Resend
  resend_headers = {
      "Authorization": f"Bearer {RESEND_API_KEY}",
      "Content-Type": "application/json",
  }

  resend_payload = {
      "from": SENDER_EMAIL,
      "to": [email],
      "subject": "Ваш экспресс-аудит ИЖС готов 🏗️",
      "html": (
          f"<h2>Результаты экспресс-аудита</h2><div"
          f" style='white-space: pre-wrap;'>{report_text}</div>"
      ),
  }

  resend_response = requests.post(
      "https://api.resend.com/emails", json=resend_payload, headers=resend_headers
  )

  return {
      "status": "success",
      "email_sent": resend_response.status_code == 200,
  }

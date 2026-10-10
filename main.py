import base64
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
async def run_audit(email: str = Form(...), file: UploadFile = File(...)):
  file_bytes = await file.read()

  # Кодируем файл в base64 для передачи в Gemini
  file_base64 = base64.b64encode(file_bytes).decode("utf-8")
  mime_type = file.content_type or "application/pdf"
  file_data_url = f"data:{mime_type};base64,{file_base64}"

  # Запрос к Gemini через OpenRouter с передачей файла
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
                      " котельные, инсоляция, размеры)."
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
      # Если OpenRouter вернул ошибку (например, нехватка средств)
      report_text = f"Ошибка от OpenRouter: {str(ai_data)}"
  except Exception as e:
    report_text = f"Ошибка запроса к ИИ: {str(e)}"

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

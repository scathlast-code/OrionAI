"""
Безкоштовний музичний сервер для бота: запускається на вашому комп'ютері й генерує музику моделлю MusicGen.

Встановлення (один раз):
    pip install torch transformers scipy fastapi uvicorn
Запуск:
    python music_server.py
Потім у боті задайте MUSIC_LOCAL_URL=http://АДРЕСА-КОМП'ЮТЕРА:8800
(якщо бот на хостингу — відкрийте доступ через Cloudflare Tunnel або Tailscale).

Модель facebook/musicgen-small (~1,2 ГБ) скачується при першому запуску. З відеокартою 30 секунд
музики генеруються за хвилину-дві, без неї — помітно довше. Ліцензія ваг MusicGen — CC BY-NC 4.0:
лише некомерційне використання.
"""

import io

import scipy.io.wavfile
import torch
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from transformers import AutoProcessor, MusicgenForConditionalGeneration

MODEL = "facebook/musicgen-small"
device = "cuda" if torch.cuda.is_available() else "cpu"
processor = AutoProcessor.from_pretrained(MODEL)
model = MusicgenForConditionalGeneration.from_pretrained(MODEL).to(device)
app = FastAPI()


class Req(BaseModel):
    prompt: str
    seconds: int = 30


@app.post("/generate")
def generate(r: Req):
    seconds = max(5, min(r.seconds, 30))
    inputs = processor(text=[r.prompt], padding=True, return_tensors="pt").to(device)
    audio = model.generate(**inputs, max_new_tokens=int(seconds * 51.2))
    buf = io.BytesIO()
    rate = model.config.audio_encoder.sampling_rate
    scipy.io.wavfile.write(buf, rate, audio[0, 0].cpu().numpy())
    return Response(buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8800)

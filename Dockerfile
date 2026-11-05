FROM python:3.13-slim

# ffmpeg — аудіо/відео; DejaVu — кирилиця у зображеннях; tzdata — час завдань
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо весь безпечний вміст репозиторію, щоб «Повна копія OrionAI»
# могла включати код, README та docs. Секрети відсікає .dockerignore.
COPY . .

# База — у /data. На Railway ОБОВ'ЯЗКОВО підключіть Volume з Mount path /data (docs/09-railway.md),
# інакше дані зникатимуть при кожному оновленні. Рядок VOLUME тут навмисно відсутній: Railway його не приймає.
ENV DB_PATH=/data/bot.db MANAGER_DB_PATH=/data/manager.db BOT_TZ=Europe/Kyiv TZ=Europe/Kyiv PYTHONUNBUFFERED=1
RUN mkdir -p /data

# На Railway запускаємо основний бот. manager.py можна винести в окремий service.
CMD ["python", "bot.py"]

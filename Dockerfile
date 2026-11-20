# ---- YouTube PO Token: скрипт генерації bgutil (Node 22). Якщо збірка тут не вдасться — бот усе одно збереться,
# просто YouTube працюватиме без токена (див. «🛠 Інструменти» в 📥 Медіа).
FROM node:22-bookworm-slim AS pot-build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ pkg-config \
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/bgutil/server && ( \
        git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /tmp/bgutil \
        && cd /tmp/bgutil/server && npm ci --no-audit --no-fund && npx tsc && npm prune --omit=dev --no-audit --no-fund \
        && rm -rf /opt/bgutil/server && mv /tmp/bgutil/server /opt/bgutil/server \
    ) || echo "bgutil build skipped — YouTube без PO Token"

FROM python:3.13-slim-bookworm
# Node 22 — для yt-dlp (розгадування захисту YouTube) і для скрипта PO Token
COPY --from=pot-build /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-build /opt/bgutil/server /opt/bgutil/server
# ffmpeg — слайдшоу й голос, шрифти DejaVu — підписи кирилицею, tzdata — час завдань
# 📥 Медіа: exiftool, mat2, 7z — метадані; cairo/pango/jpeg/gif/rsvg — бібліотеки для скрипта PO Token
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
    libimage-exiftool-perl mat2 p7zip-full \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Браузер для /browse (~400 МБ). Щоб зібрати без нього: --build-arg WITH_BROWSER=0
ARG WITH_BROWSER=1
RUN if [ "$WITH_BROWSER" = "1" ]; then python -m playwright install --with-deps chromium; fi

# Копіюємо весь безпечний вміст репозиторію, щоб «Повна копія OrionAI»
# могла включати код, README та docs. Секрети відсікає .dockerignore.
COPY . .

# База — у /data. На Railway ОБОВ'ЯЗКОВО підключіть Volume з Mount path /data (docs/20-railway.md),
# інакше дані зникатимуть при кожному оновленні. Рядок VOLUME тут навмисно відсутній: Railway його не приймає.
ENV BGUTIL_SERVER_HOME=/opt/bgutil/server DB_PATH=/data/bot.db MANAGER_DB_PATH=/data/manager.db BOT_TZ=Europe/Kyiv TZ=Europe/Kyiv PYTHONUNBUFFERED=1
RUN mkdir -p /data

# На Railway запускаємо основний бот. manager.py можна винести в окремий service.
CMD ["python", "bot.py"]

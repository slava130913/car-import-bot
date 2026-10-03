FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/bot.sqlite3
VOLUME ["/data"]
CMD ["python", "-m", "bot.main"]

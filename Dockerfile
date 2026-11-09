FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Команда задаётся в docker-compose.yml: бот, backend или миграции
CMD ["python", "-m", "bot.main"]

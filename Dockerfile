FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY monitor.py README.md Procfile .env.example ./

RUN mkdir -p /app/.state

CMD ["python", "monitor.py"]

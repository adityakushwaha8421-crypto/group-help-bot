FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data/evidence data/browser
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]

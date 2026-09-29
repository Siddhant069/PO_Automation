# Official Playwright image: matches playwright==1.48.0 in requirements.txt and ships Chromium + its OS deps
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata

# tzdata so TZ=Asia/Kolkata takes effect (PO dates in the DB are stored in IST local time)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY *.py ./

CMD ["python", "main.py"]

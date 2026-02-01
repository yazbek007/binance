# Dockerfile
FROM python:3.11-slim

# تعيين دليل العمل
WORKDIR /app

# تثبيت حزم النظام المطلوبة
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# نسخ ملف المتطلبات
COPY requirements.txt .

# تثبيت حزم Python
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# نسخ باقي الملفات
COPY . .

# التعرض للبورت (استخدمي 10000 كما في Render)
EXPOSE 10000

# تشغيل التطبيق
CMD ["uvicorn", "signals:app", "--host", "0.0.0.0", "--port", "10000"]

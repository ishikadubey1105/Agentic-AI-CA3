# Works on Hugging Face Spaces (Docker SDK), Railway, Fly.io or any container host.
FROM python:3.11-slim

# matplotlib and pandas wheels need nothing extra, but fonts make the PDF look right
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces expects 7860, everything else passes $PORT
ENV PORT=7860
EXPOSE 7860

CMD ["python", "backend.py"]

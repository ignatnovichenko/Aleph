FROM node:24-alpine AS frontend
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci
COPY frontend/ frontend/
COPY scripts/ scripts/
COPY web/ web/
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_INSTALL_DIR=/opt/uv-python ALEPH_HERMES_PYTHON=/opt/hermes/.venv/bin/python HF_HOME=/data/models ALEPH_DB=/data/aleph.sqlite3 BIND=0.0.0.0
WORKDIR /app
COPY requirements*.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 git curl ffmpeg ca-certificates fonts-dejavu-core fonts-dejavu-extra poppler-utils tesseract-ocr-eng tesseract-ocr-rus bubblewrap libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-draw && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r requirements-voice.txt && useradd --system --uid 10001 --create-home aleph && install -d -o aleph -g aleph -m 700 /data
RUN pip install --no-cache-dir uv
COPY deploy/install-hermes.sh /usr/local/bin/install-hermes
RUN install-hermes /opt/hermes
ENV ALEPH_BLUETTS_HOME=/opt/aleph-bluetts
COPY deploy/install-bluetts.py /tmp/install-bluetts.py
RUN if [ "$(uname -m)" = x86_64 ]; then python /tmp/install-bluetts.py && chmod -R a+rX "$ALEPH_BLUETTS_HOME"; fi
COPY aleph/ aleph/
COPY --from=frontend /app/dist/ dist/
USER aleph
EXPOSE 8787
CMD ["python", "-m", "aleph"]

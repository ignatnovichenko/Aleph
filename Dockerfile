FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_INSTALL_DIR=/opt/uv-python ALEPH_HERMES_PYTHON=/opt/hermes/.venv/bin/python HF_HOME=/data/models ALEPH_DB=/data/aleph.sqlite3 BIND=0.0.0.0
WORKDIR /app
COPY requirements*.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 git ca-certificates && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r requirements-voice.txt && useradd --system --uid 10001 --create-home aleph && install -d -o aleph -g aleph -m 700 /data
RUN pip install --no-cache-dir uv
COPY deploy/install-hermes.sh /usr/local/bin/install-hermes
RUN install-hermes /opt/hermes
COPY aleph/ aleph/
COPY web/ web/
USER aleph
EXPOSE 8787
CMD ["python", "-m", "aleph"]

ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --no-create-home appuser
COPY service ./service
USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=3)"
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

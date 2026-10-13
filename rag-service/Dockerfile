ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home rag
COPY enterprise ./enterprise
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "enterprise.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]

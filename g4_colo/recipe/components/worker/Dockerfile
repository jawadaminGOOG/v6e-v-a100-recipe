FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir "numpy>=2.0,<3" "prometheus_client>=0.20"
WORKDIR /app
COPY worker.py /app/worker.py
USER 65534:65534
EXPOSE 9100
ENTRYPOINT ["python", "/app/worker.py"]

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY loadgen.py hist.py prompts.py /app/
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/app/loadgen.py"]

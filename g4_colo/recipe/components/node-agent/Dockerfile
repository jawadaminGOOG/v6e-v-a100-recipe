FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY node_agent.py /app/node_agent.py
# Runs as root so it can read /var/lib/kubelet/cpu_manager_state (0644 on GKE,
# but the parent dir may be 0750); mounts are read-only.
EXPOSE 8080
ENTRYPOINT ["python", "/app/node_agent.py"]

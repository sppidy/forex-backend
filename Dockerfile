# Janus Forex backend — must be built FROM the super-repo context so the
# backend can COPY both forex-agent (which it dynamically imports) and
# forex-backend.
#
#   # from the super root (where forex-agent/ and forex-backend/ are both visible):
#   docker build -f forex-backend/Dockerfile -t janus/forex-backend .
#
# The super-level docker-compose.yml does this automatically.

FROM python:3.14-slim

WORKDIR /app

# Backend + agent deps in a single layer.
COPY forex-backend/requirements.txt /tmp/backend-requirements.txt
COPY forex-agent/requirements.txt   /tmp/agent-requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend-requirements.txt -r /tmp/agent-requirements.txt \
    && rm -f /tmp/*-requirements.txt

# Agent code on PYTHONPATH at /app/agent; backend at /app/backend.
COPY forex-agent   /app/agent
COPY forex-backend /app/backend

# Where the auto-generated admin key gets written (mode 0600). Mount a tmpfs
# / named volume here so the key survives container restarts.
RUN mkdir -p /run/forex && chmod 0700 /run/forex

ENV AGENT_DIR=/app/agent \
    PYTHONPATH=/app/agent:/app/backend \
    PYTHONUNBUFFERED=1

EXPOSE 8445

WORKDIR /app/backend
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8445"]

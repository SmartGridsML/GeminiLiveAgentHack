FROM python:3.12-slim

WORKDIR /app

# Install uv for fast, deterministic dependency installs
RUN pip install --no-cache-dir uv

# Install dependencies using uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt \
    && python -c "import google.adk; import google.genai; print('dependency import check: ok')"

# Copy source
COPY backend/ backend/
COPY frontend/ frontend/

# Non-root user for Cloud Run security best practices
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

# --loop uvloop    : faster async event loop
# --timeout-keep-alive 120 : prevents Cloud Run from killing idle WebSocket connections
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--timeout-keep-alive", "120"]

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[prod]" 2>/dev/null || pip install --no-cache-dir -e .

COPY . .

# Non-root user
RUN useradd -m celerp && chown -R celerp /app
USER celerp

EXPOSE 8000 8080

CMD ["uvicorn", "celerp.main:app", "--host", "0.0.0.0", "--port", "8000"]

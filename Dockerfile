# Build/test environment for PCUTP. Same image is used locally and in CI.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY picocalc ./picocalc

RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "pcutp.daemon", "selftest"]

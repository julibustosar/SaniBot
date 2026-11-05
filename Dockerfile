FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 sanibot
COPY pyproject.toml README.md ./
COPY sanibot ./sanibot
COPY mock_sanitas ./mock_sanitas
COPY simulator ./simulator
RUN pip install --no-cache-dir . && mkdir -p /data && chown sanibot /data
USER sanibot
EXPOSE 8000
CMD ["uvicorn", "sanibot.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

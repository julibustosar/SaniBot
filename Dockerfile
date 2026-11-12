FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 sanibot
ENV PYTHONPATH=/app
# Versiones exactas con hash y solo paquetes binarios: no se ejecuta código de instalación de terceros.
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.txt && mkdir -p /data && chown sanibot /data
COPY sanibot ./sanibot
COPY mock_sanitas ./mock_sanitas
COPY simulator ./simulator
USER sanibot
EXPOSE 8000
CMD ["uvicorn", "sanibot.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/hngyhngyhobo/WeatherAlert-HomeAssistant" \
      org.opencontainers.image.description="Lightning proximity and NWS severe weather alerts for Home Assistant via MQTT" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      com.centurylinklabs.watchtower.enable="true"
ENV PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
# Unraid convention: run as nobody/users (99:100)
RUN mkdir -p /config && chown 99:100 /config
USER 99:100
VOLUME /config
EXPOSE 8099
ENTRYPOINT ["python", "-m", "stormwatch"]

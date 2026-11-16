FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
 && pip install --no-cache-dir --target /deps -r requirements.txt \
 && pip install --no-cache-dir --no-deps --target /app .

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --shell /usr/sbin/nologin --create-home app && mkdir /data && chown app /data
COPY --from=build /deps /opt/deps
COPY --from=build /app /opt/app
COPY LICENSE NOTICE /opt/
ENV PYTHONPATH=/opt/app:/opt/deps PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VKTEAMS_MCP_DATABASE=/data/vkteams-mcp.sqlite PORT=8000
USER 10001
EXPOSE 8000
CMD ["python", "-m", "vkteams_mcp.app"]

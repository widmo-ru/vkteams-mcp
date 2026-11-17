# Alpine: the Debian slim base carried about a hundred HIGH and CRITICAL findings with no fix from Debian;
# on Alpine every finding had a fixed package, which `apk upgrade` installs. CI scans the result with Trivy.
FROM python:3.12-alpine AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
 && pip install --no-cache-dir --only-binary=:all: --target /deps -r requirements.txt \
 && pip install --no-cache-dir --no-deps --target /app .

FROM python:3.12-alpine
RUN apk upgrade --no-cache \
 && pip uninstall -y pip \
 && adduser -D -u 10001 -s /sbin/nologin app
COPY --from=build /deps /opt/deps
COPY --from=build /app /opt/app
COPY LICENSE NOTICE /opt/
ENV PYTHONPATH=/opt/app:/opt/deps PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PORT=8000
USER 10001
EXPOSE 8000
CMD ["python", "-m", "vkteams_mcp.app"]

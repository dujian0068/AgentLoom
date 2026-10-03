FROM node:22-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY apps/api/ apps/api/
COPY packages/ packages/
COPY --from=web /build/dist apps/web/dist
ENV AGENT_LOOM_DATA=/app/data
EXPOSE 8766
CMD ["uvicorn", "agentloom.app:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8766"]

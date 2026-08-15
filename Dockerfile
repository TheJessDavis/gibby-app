# Gibby Class Manager - pure Python standard library, no dependencies.
FROM python:3.11-slim
WORKDIR /app
COPY . /app
# DATA_DIR should point at a mounted persistent volume in production so the
# SQLite database (and its registrations) survive restarts and redeploys.
ENV PORT=8000 DATA_DIR=/data
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "server.py"]

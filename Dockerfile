FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    TRACERT_DATA_DIR=/data \
    TRACERT_RUNTIME_DIR=/tmp
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tracert \
    && useradd --uid 10001 --gid tracert --no-create-home tracert \
    && mkdir /data \
    && chown tracert:tracert /data
COPY server.py update_scheduler.py update_avoid_points.py container_entrypoint.py ./
COPY web/ ./web/
COPY controlled_area_gcj.json ./
COPY camera_points.json data_update_status.json ./seed/
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "container_entrypoint.py"]
CMD ["serve"]

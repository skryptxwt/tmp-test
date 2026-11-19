FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir /data && chown 10001:10001 /data
COPY mock.py web_console.py idt_proxy_pb2.py idt_proxy_pb2_grpc.py idt_proxy.proto changes.example.json ./
COPY web/ ./web/
USER 10001:10001
EXPOSE 50051 8765
ENTRYPOINT ["python", "mock.py", "--db", "/data/changes.sqlite3"]
CMD ["serve", "--listen", "0.0.0.0:50051", "--web-host", "0.0.0.0"]

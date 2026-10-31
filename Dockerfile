# Legis-Link MCP server - container build (LL-38). Railway uses this when the service's builder is "Dockerfile".
# Everything the server needs (pages, icons, data) is inside legis_link_mcp_server.py.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY legis_link_mcp_server.py .
# Railway sets PORT (8080 on the Legis-Link service); the server listens on it
EXPOSE 8080
CMD ["python", "legis_link_mcp_server.py"]

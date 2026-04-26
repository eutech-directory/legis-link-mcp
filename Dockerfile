FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY legis_link_mcp_server.py .
EXPOSE 8000
CMD ["python", "legis_link_mcp_server.py"]

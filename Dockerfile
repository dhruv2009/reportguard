FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m reportguard.cli setup

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "run_server.py", "--http"]

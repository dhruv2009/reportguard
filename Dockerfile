FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m reportguard.cli setup

# RG_API_TOKEN must be set at run time: the server refuses to start publicly without it
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "run_server.py", "--http"]

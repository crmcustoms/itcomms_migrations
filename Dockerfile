FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Create dirs for outputs
RUN mkdir -p logs data

EXPOSE 8000

CMD ["python", "-u", "server.py"]

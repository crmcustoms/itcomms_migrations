FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Create dirs for outputs
RUN mkdir -p logs data

# Keep container alive so we can docker exec into it
CMD ["tail", "-f", "/dev/null"]

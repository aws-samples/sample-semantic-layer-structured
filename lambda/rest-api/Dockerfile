FROM public.ecr.aws/docker/library/python:3.12-slim
# checkov:skip=CKV_DOCKER_2: Lambda containers managed by AWS Lambda service - healthcheck not applicable
# Build version: 2025-01-08 - Fixed Python bytecode permission issue

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    portaudio19-dev \
    python3-pyaudio \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -r appuser

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir awslambdaric
# Copy application code (.dockerignore file automatically excludes unwanted files)
COPY . .

# Pre-compile Python bytecode and create __pycache__ directories
# This prevents runtime permission errors when Python tries to create .pyc files
RUN python -m compileall -q /app && \
    find /app -type d -name __pycache__ -exec chmod 755 {} \; && \
    find /app -type f -name "*.pyc" -exec chmod 644 {} \;

# Set environment variables for Lambda
ENV PYTHONUNBUFFERED=1 \
    AWS_DEFAULT_REGION=us-east-1 \
    PYTHONDONTWRITEBYTECODE=1

# Change ownership of application files (including pre-compiled bytecode)
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app

# Switch to non-root user
USER appuser

# Entry point for Lambda container
# awslambdaric is the Lambda Runtime Interface Client
ENTRYPOINT ["python", "-m", "awslambdaric"]

# Handler function (main.py has the Mangum-wrapped FastAPI app)
CMD ["main.handler"]

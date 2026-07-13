# QA Tool Docker Image

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libssl-dev \
    libffi-dev \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser (engine pins chromium)
RUN python -m playwright install --with-deps chromium

# Create output directory
RUN mkdir -p qa-results

# Set entrypoint (v2 engine: explore | act | flow | sweep | report)
ENTRYPOINT ["python", "-m", "engine.cli"]

# Default command
CMD ["--help"]

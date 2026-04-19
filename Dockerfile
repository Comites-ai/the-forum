# Use official Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies.
#
# Node.js + uv are required for stdio-transport MCP servers:
#   - `npx` runs npm-packaged MCP servers (e.g. @modelcontextprotocol/server-github)
#   - `uvx` runs PyPI-packaged MCP servers (e.g. mcp-server-time)
#
# These are only invoked when an MCP server in Firestore has
# transport="stdio" and a command in the allowlist ({npx, uvx}).
# See app/models/agent.py::ALLOWED_STDIO_COMMANDS.
RUN apt-get update && apt-get install -y \
        gcc \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && pip install --no-cache-dir uv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=3.0)" || exit 1

# Run with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

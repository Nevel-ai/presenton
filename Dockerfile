FROM python:3.11-slim-bookworm

# Install Node.js and npm
RUN apt-get update && apt-get install -y \
    nginx \
    curl \
    zstd \
    libreoffice \
    fontconfig \
    chromium


# Install Node.js 20 using NodeSource repository
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs


# Create a working directory
WORKDIR /app  

# Set environment variables
ENV APP_DATA_DIRECTORY=/app_data
ENV TEMP_DIRECTORY=/tmp/presenton
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium


# Install ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Install dependencies for FastAPI
COPY servers/fastapi/requirements-s3.txt /tmp/requirements-s3.txt
RUN pip install -r /tmp/requirements-s3.txt -c /tmp/requirements-s3.txt \
    aiohttp aiomysql aiosqlite asyncpg fastapi[standard] \
    pathvalidate pdfplumber chromadb sqlmodel \
    anthropic google-genai openai fastmcp dirtyjson
RUN pip install -c /tmp/requirements-s3.txt docling --extra-index-url https://download.pytorch.org/whl/cpu

# Install dependencies for Next.js
WORKDIR /app/servers/nextjs
COPY servers/nextjs/package.json servers/nextjs/package-lock.json ./
RUN npm install


# Copy Next.js app
COPY servers/nextjs/ /app/servers/nextjs/

# Build the Next.js app
WORKDIR /app/servers/nextjs
RUN npm run build

WORKDIR /app

# Copy FastAPI
COPY servers/fastapi/ ./servers/fastapi/
COPY start.js LICENSE NOTICE ./

# Copy user config generator script
COPY scripts/generate-userconfig.sh /app/scripts/generate-userconfig.sh
RUN chmod +x /app/scripts/generate-userconfig.sh

# Copy nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Expose the port
EXPOSE 80

# Start the servers (generate user config first)
CMD ["sh", "-lc", "/app/scripts/generate-userconfig.sh && exec node /app/start.js"]

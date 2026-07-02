FROM python:3.12-slim

# Install only wget and curl (for debugging) – Playwright will install its own deps
RUN apt-get update && apt-get install -y wget curl && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Let Playwright install all required system libraries AND Chromium browser
RUN python -m playwright install-deps chromium
RUN python -m playwright install chromium

# Copy your bot code
COPY . .

# Start the bot
CMD ["python", "bot.py"]

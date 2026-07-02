FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your bot code
COPY . .

# Start the bot
CMD ["python", "bot.py"]

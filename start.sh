#!/bin/bash
# Install Chromium browser and system dependencies for Playwright
playwright install chromium
playwright install-deps

# Start the bot
python bot.py

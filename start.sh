#!/bin/bash
echo "Installing Playwright browsers..."
playwright install chromium --with-deps
echo "Starting bot..."
python bot.py

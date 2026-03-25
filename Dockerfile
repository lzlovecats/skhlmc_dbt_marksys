FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

# Copy only the bot source files
COPY tg_bot.py tg_notifications.py tg_scheduler.py ./

# -u = unbuffered stdout/stderr so logs appear in fly logs immediately
CMD ["python", "-u", "tg_bot.py"]

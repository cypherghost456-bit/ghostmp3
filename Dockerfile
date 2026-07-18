FROM python:3.11-slim

# Install FFmpeg (Required for yt-dlp to work)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app code
COPY . .

# Start the server
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8080"]

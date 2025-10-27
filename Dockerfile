# Use lightweight Python 3.11 image
FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Copy cookies file for YouTube authentication
COPY www.youtube.com_cookies.txt .

# Create downloads directory
RUN mkdir -p downloads

# Expose port (Koyeb or any platform uses PORT env variable)
EXPOSE 8000

# Set environment variable for cookies file
ENV YOUTUBE_COOKIES=www.youtube.com_cookies.txt

# Start the application with Uvicorn
# Your fucking shitting me
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

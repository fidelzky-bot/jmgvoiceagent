# Use the official Python image.
FROM python:3.11-slim

# Set the working directory in the container.
WORKDIR /app

# Copy the requirements file and install dependencies.
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copy the rest of your application code.
COPY . .

# Expose port 8080 for Fly.io
EXPOSE 8080

# Run your server
CMD ["python", "server.py"] 
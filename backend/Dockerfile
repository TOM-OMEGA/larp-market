FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests gunicorn
COPY . .
EXPOSE 5002
CMD ["gunicorn", "--bind", "0.0.0.0:5002", "--workers", "2", "--timeout", "120", "app:app"]

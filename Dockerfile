FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY app.py .
# Secrets come from the host's environment, never baked into the image.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]

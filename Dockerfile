FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cascade.py .

EXPOSE 8319

CMD ["python", "cascade.py"]

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cascade.py .
COPY cascade_lib/ cascade_lib/

EXPOSE 8319

CMD ["python", "cascade.py"]

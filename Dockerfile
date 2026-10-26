FROM python:3.12-slim

WORKDIR /srv/swirl
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
ENV SWIRL_DB=/data/swirl.db
VOLUME /data
EXPOSE 8080

# API process; the payout worker is the same image with: python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

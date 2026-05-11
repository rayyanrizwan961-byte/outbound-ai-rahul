FROM python:3.11-slim
RUN apt-get update && apt-get install -y libgomp1 libglib2.0-0 libsndfile1 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "agent.py", "start"]

FROM python:3.14-slim

WORKDIR /app

COPY . /app

EXPOSE 4173

CMD ["python", "server.py"]

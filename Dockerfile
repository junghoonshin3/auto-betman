FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

RUN mkdir -p storage && chown -R pwuser:pwuser /app

USER pwuser

EXPOSE 10000

CMD ["python", "-m", "src.main", "--schedule"]

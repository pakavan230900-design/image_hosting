FROM python:3.12-slim AS builder 
WORKDIR /app 
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt 
FROM python:3.12-slim 

WORKDIR /app 
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY app.py .
COPY static/ ./static/

RUN mkdir -p images logs 
EXPOSE 8000 
CMD ["python", "app.py"]
 
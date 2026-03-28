FROM python:3.11-slim

# dstack-sdk temporarily disabled (KMS commented out)
RUN pip install --no-cache-dir web3 eth-account pycryptodome

WORKDIR /app
COPY monitor.py .

CMD ["python3", "monitor.py"]

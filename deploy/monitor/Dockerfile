FROM python:3.11-slim

RUN pip install --no-cache-dir web3 eth-account pycryptodome dstack-sdk

WORKDIR /app
COPY monitor.py .

CMD ["python3", "monitor.py"]

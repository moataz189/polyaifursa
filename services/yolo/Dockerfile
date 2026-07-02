FROM python:3.12-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY torch-requirements.txt .
COPY requirements.txt .
RUN pip install --no-cache-dir -r torch-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade "wheel==0.46.2" "pip>=26.1.2" "setuptools>=78.1.1"
COPY . .

EXPOSE 8080

CMD ["python", "app.py"]

#bonus
# FROM python:3.11-slim AS builder

# RUN python -m venv /opt/venv
# ENV PATH="/opt/venv/bin:$PATH"

# WORKDIR /app

# COPY torch-requirements.txt .
# COPY requirements.txt .

# RUN pip install --no-cache-dir -r torch-requirements.txt
# RUN pip install --no-cache-dir -r requirements.txt


# FROM python:3.11-slim

# RUN apt-get update && apt-get install -y \
#     libglib2.0-0 \
#     libgl1 \
#     libxcb1 \
#     && rm -rf /var/lib/apt/lists/*

# WORKDIR /app

# COPY --from=builder /opt/venv /opt/venv
# ENV PATH="/opt/venv/bin:$PATH"

# COPY . .

# EXPOSE 8080

# CMD ["python", "app.py"]
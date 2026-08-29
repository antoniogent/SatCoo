FROM python:3.11-slim-bullseye

# Installazione mdbtools + strumenti di compilazione
RUN apt-get update && apt-get install -y \
    mdbtools \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade di pip e installazione dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia del codice sorgente
COPY . .

# Mantiene il container sempre in ascolto/attivo
CMD ["tail", "-f", "/dev/null"]FROM python:3.11-slim-bullseye

# Installazione mdbtools + strumenti di compilazione C/C++ (gcc, g++, libpq-dev)
RUN apt-get update && apt-get install -y \
    mdbtools \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade di pip e installazione dei requisiti
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia del codice sorgente
COPY . .

CMD ["python", "main.py"]

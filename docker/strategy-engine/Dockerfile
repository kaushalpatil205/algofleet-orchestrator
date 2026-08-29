# Stage 1: Builder - compile TA-Lib C library
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    build-essential wget gcc make automake \
    && rm -rf /var/lib/apt/lists/*

# Install TA-Lib C library from source
WORKDIR /tmp
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cp /usr/share/automake-*/config.guess ta-lib/ \
    && cp /usr/share/automake-*/config.sub ta-lib/ \
    && cd ta-lib && ./configure --prefix=/usr && make && make install

WORKDIR /app
COPY strategy-engine/requirements.txt .
# Install python dependencies including TA-Lib which compiles against the C library
RUN pip install --no-cache-dir -r requirements.txt TA-Lib psycopg2-binary

# Stage 2: Final runtime image (smaller, no build tools)
FROM python:3.11-slim

# Copy the compiled TA-Lib C library
COPY --from=builder /usr/lib/libta_lib* /usr/lib/
# Copy python packages
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

WORKDIR /app
# Copy the entire strategy engine repository
COPY strategy-engine/ /app/
# Copy our custom entrypoint
COPY algofleet/docker/strategy-engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# STRATEGY_SCRIPT is injected per-pod (e.g. "Live/Strategy 17/s17_m3m2_v1_btcusd_sell.py")
# ENGINE_CONFIG_JSON is injected from AWS Secrets Manager at runtime
ENV STRATEGY_SCRIPT=""
ENV ENGINE_CONFIG_JSON=""

ENTRYPOINT ["/app/entrypoint.sh"]

FROM python:3.12-slim

# Playwright + bw CLI butuh beberapa paket sistem.
# PENTING: libatomic1 wajib ada SEBELUM playwright dipanggil — driver Node-nya
# (playwright/driver/node) men-dependensikan libatomic, dan tanpa itu
# `playwright install` ikut gagal (dulu tertutup oleh `|| echo WARN`).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip tzdata \
        libatomic1 libnss3 libnspr4 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Zona waktu agar jadwal "pagi" sesuai WIB.
ENV TZ=Asia/Jakarta

# Bitwarden CLI (opsional — dipakai tool bw_get_password)
RUN curl -fsSL "https://bitwarden.com/download/?app=cli&platform=linux" \
      -o /tmp/bw.zip \
    && unzip -o /tmp/bw.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/bw \
    && rm /tmp/bw.zip \
    || echo "WARN: bw CLI gagal di-install (opsional)"

WORKDIR /app

# 1) Dependensi Python — layer stabil (cache hangus hanya bila requirements berubah)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 2) Browser Chromium — layer paling mahal, ditaruh SEBELUM copy source
#    agar tidak ter-download ulang saat kode berubah.
#    Jangan disembunyikan: kalau gagal, build harus gagal.
RUN python -m playwright install --with-deps chromium

# 3) Source code (paling sering berubah) — paling atas urutan copy
COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 10000

ENTRYPOINT ["/entrypoint.sh"]

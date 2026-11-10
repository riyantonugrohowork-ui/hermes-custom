#!/bin/sh
set -e

echo "🚀 Hermes Agent — entrypoint"

# 1. Vault Bitwarden (opsi A: unlock otomatis).
#    Prioritas: Docker secret file -> BW_MASTERPASS env.
MP=""
if [ -f /run/secrets/bw_masterpass ]; then
  MP="$(tr -d '\r\n' < /run/secrets/bw_masterpass)"
fi
if [ -z "$MP" ] && [ -n "$BW_MASTERPASS" ]; then
  MP="$BW_MASTERPASS"
fi

if [ -n "$MP" ]; then
  # Cek status akun bw: belum login perlu `bw login` dulu.
  # Semua perintah diberi timeout agar tidak menggantung & memblokir web UI.
  STATUS=$(timeout 15 bw status --nointeraction 2>/dev/null | grep -o '"status":"[^"]*"' | cut -d'"' -f4)

  if [ "$STATUS" = "unauthenticated" ] || [ -z "$STATUS" ]; then
    if [ -n "$BW_EMAIL" ]; then
      echo "🔑 Login Bitwarden sebagai $BW_EMAIL..."
      if timeout 25 env BW_PASSWORD="$MP" bw login "$BW_EMAIL" --passwordenv BW_PASSWORD --raw >/dev/null 2>&1; then
        echo "✅ Login Bitwarden berhasil."
      else
        echo "⚠️  Login Bitwarden gagal/timeout (cek email/password/2FA). Lanjut tanpa vault."
      fi
    else
      echo "⚠️  Bitwarden belum login & BW_EMAIL kosong — isi BW_EMAIL di .env."
    fi
  fi

  echo "🔐 Membuka vault Bitwarden otomatis..."
  SESSION=$(timeout 30 env BW_PASSWORD="$MP" bw unlock --passwordenv BW_PASSWORD --raw 2>/dev/null)
  if [ -n "$SESSION" ]; then
    export BW_SESSION="$SESSION"
    echo "✅ Vault terbuka (session baru dihasilkan)."
  else
    echo "⚠️  bw unlock gagal/timeout — cek master password. Vault tetap terkunci."
  fi
elif [ -n "$BW_SESSION" ]; then
  echo "✅ BW_SESSION terdeteksi dari environment."
else
  echo "⚠️  Vault Bitwarden terkunci — tulis master password ke secrets/bw_masterpass bila perlu."
fi

# 2. Pastikan folder runtime ada.
mkdir -p /app/data /app/output /app/logs

# 3. Start daemon otonom di background (jalankan recipe sesuai jadwalnya).
#    HERMES_AUTOPILOT menang; ECUTI_AUTOPILOT dipertahankan agar .env lama tetap berlaku.
AUTOPILOT="${HERMES_AUTOPILOT:-${ECUTI_AUTOPILOT:-1}}"
if [ "$AUTOPILOT" = "1" ]; then
  echo "🤖 Autopilot aktif — recipe berjalan mengikuti jadwalnya."
  python -m hermes autopilot &
else
  echo "ℹ️  Autopilot nonaktif — recipe hanya jalan manual."
fi

# 4. Start web UI (foreground, port 10000).
echo "🌐 Menghidupkan web UI di port ${HERMES_WEB_PORT:-10000}..."
exec python -m hermes web

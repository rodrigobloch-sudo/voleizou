#!/bin/bash
cd "$(dirname "$0")"

# Instala dependências se necessário
pip3 install -r requirements.txt -q

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "localhost")

echo "🏐 Iniciando Voleizou..."
echo ""
echo "   → Painel:     https://localhost:8000"
echo "   → Rede local: https://$IP:8000"
echo ""
echo "   Abrindo navegador..."
sleep 2

open "https://localhost:8000"

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem

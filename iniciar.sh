#!/bin/bash
cd "$(dirname "$0")"

# Instala dependências se necessário
pip3 install -r requirements.txt -q

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "localhost")
HOSTNAME=$(scutil --get LocalHostName 2>/dev/null || echo "$IP")

echo ""
echo "🏐 Voleizou rodando em:"
echo "   → Seu computador: https://localhost:8000"
echo "   → Rede local:     https://$HOSTNAME.local:8000"
echo ""
echo "   Pressione Ctrl+C para encerrar."
echo ""

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem --reload

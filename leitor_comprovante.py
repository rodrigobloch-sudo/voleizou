"""
leitor_comprovante.py — Extrai dados de comprovantes Pix em PDF ou imagem (PNG/JPG).
Suporta: Nubank (e outros bancos com formato similar).
Retorna: { nome, valor, data_iso, data_original, banco }
"""

import re
from io import BytesIO
from pdfminer.high_level import extract_text
import pytesseract
from PIL import Image

MESES_PT = {
    'JAN': 1, 'FEV': 2, 'MAR': 3, 'ABR': 4, 'MAI': 5, 'JUN': 6,
    'JUL': 7, 'AGO': 8, 'SET': 9, 'OUT': 10, 'NOV': 11, 'DEZ': 12,
}


def parse_data(texto: str):
    """Extrai a data do comprovante. Retorna (data_iso, data_original)."""

    # Formato Nubank: "03 MAI 2026 - 14:53:49"
    m = re.search(r'(\d{2})\s+([A-Z]{3})\s+(\d{4})\s*[-–]\s*(\d{2}:\d{2}:\d{2})', texto, re.IGNORECASE)
    if m:
        dia, mes_abr, ano, hora = m.group(1), m.group(2).upper(), m.group(3), m.group(4)
        mes = MESES_PT.get(mes_abr)
        if mes:
            data_iso = f"{ano}-{mes:02d}-{dia}"
            data_orig = f"{dia}/{mes:02d}/{ano} {hora}"
            return data_iso, data_orig

    # Formato alternativo: "03/05/2026"
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', texto)
    if m:
        dia, mes, ano = m.group(1), m.group(2), m.group(3)
        data_iso = f"{ano}-{mes}-{dia}"
        data_orig = f"{dia}/{mes}/{ano}"
        return data_iso, data_orig

    return None, None


def parse_valor(texto: str):
    """Extrai o valor do comprovante."""
    # "Valor R$ 1,00" ou "Valor\nR$ 1.200,00"
    m = re.search(r'Valor\s+R\$\s*([\d.,]+)', texto, re.IGNORECASE)
    if m:
        v = m.group(1).replace('.', '').replace(',', '.')
        try:
            return float(v)
        except ValueError:
            pass

    # Fallback: qualquer "R$ X,XX" próximo ao topo
    matches = re.findall(r'R\$\s*([\d.,]+)', texto)
    for match in matches:
        v = match.replace('.', '').replace(',', '.')
        try:
            val = float(v)
            if val > 0:
                return val
        except ValueError:
            continue

    return None


def parse_nome_origem(texto: str):
    """Extrai o nome de quem enviou (seção Origem)."""
    # Encontra a seção "Origem" e pega o Nome dentro dela
    m = re.search(r'Origem\s+Nome\s+(.+?)(?:\n|Institui)', texto, re.IGNORECASE | re.DOTALL)
    if m:
        nome = m.group(1).strip().split('\n')[0].strip()
        if nome:
            return nome

    # Fallback: última ocorrência de "Nome [valor]" no texto
    nomes = re.findall(r'Nome\s+([A-ZÀ-Ú][a-zA-ZÀ-ú\s]+?)(?:\n|Institui)', texto)
    if len(nomes) >= 2:
        return nomes[-1].strip()  # O último Nome é geralmente o pagador (Origem)

    return None


def parse_banco(texto: str) -> str:
    """Tenta identificar o banco do comprovante."""
    texto_lower = texto.lower()
    if 'nubank' in texto_lower or 'nu pagamentos' in texto_lower:
        return 'Nubank'
    if 'itaú' in texto_lower or 'itau' in texto_lower:
        return 'Itaú'
    if 'bradesco' in texto_lower:
        return 'Bradesco'
    if 'santander' in texto_lower:
        return 'Santander'
    if 'caixa' in texto_lower:
        return 'Caixa Econômica'
    if 'banco do brasil' in texto_lower or 'bb' in texto_lower:
        return 'Banco do Brasil'
    if 'inter' in texto_lower:
        return 'Banco Inter'
    if 'sicoob' in texto_lower:
        return 'Sicoob'
    return 'Não identificado'


def extrair_texto_imagem(img_bytes: bytes) -> str:
    """Extrai texto de imagem PNG/JPG usando OCR (pytesseract)."""
    img = Image.open(BytesIO(img_bytes))
    # Converte para escala de cinza para melhorar OCR
    img = img.convert('L')
    texto = pytesseract.image_to_string(img, lang='por')
    return texto


def ler_comprovante(file_bytes: bytes, filename: str = "") -> dict:
    """
    Lê um comprovante Pix em PDF ou imagem (PNG/JPG) e retorna os dados extraídos.

    Retorna dict com:
      - nome: str | None
      - valor: float | None
      - data_iso: str | None  (formato YYYY-MM-DD)
      - data_original: str | None
      - banco: str
      - texto_bruto: str  (para debug)
      - erro: str | None
    """
    try:
        ext = filename.lower().split('.')[-1] if filename else 'pdf'
        if ext in ('png', 'jpg', 'jpeg', 'webp'):
            texto = extrair_texto_imagem(file_bytes)
        else:
            texto = extract_text(BytesIO(file_bytes))

        # Normaliza espaços múltiplos mas preserva quebras de linha
        texto_limpo = re.sub(r'[ \t]+', ' ', texto)

        nome = parse_nome_origem(texto_limpo)
        valor = parse_valor(texto_limpo)
        data_iso, data_orig = parse_data(texto_limpo)
        banco = parse_banco(texto_limpo)

        resultado = {
            "nome": nome,
            "valor": valor,
            "data_iso": data_iso,
            "data_original": data_orig,
            "banco": banco,
            "texto_bruto": texto_limpo,
            "erro": None,
        }

        # Indica campos não encontrados
        campos_faltando = [k for k in ("nome", "valor", "data_iso") if not resultado[k]]
        if campos_faltando:
            resultado["aviso"] = f"Não foi possível extrair: {', '.join(campos_faltando)}"

        return resultado

    except Exception as e:
        return {
            "nome": None, "valor": None, "data_iso": None,
            "data_original": None, "banco": "—",
            "texto_bruto": "", "erro": str(e),
        }

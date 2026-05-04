from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import extract
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime
import models
from leitor_comprovante import ler_comprovante
from database import engine, get_db
import os, socket, subprocess

models.Base.metadata.create_all(bind=engine)

# ── Migrações automáticas (adiciona colunas novas sem recriar tabelas) ────────
def _migrar():
    from database import engine as _engine, is_sqlite
    with _engine.connect() as conn:
        from sqlalchemy import text as _text
        migrações = [
            ("jogadores", "posicao",         "VARCHAR"),
            ("jogadores", "numero_camisa",    "INTEGER"),
            ("jogadores", "data_nascimento",  "DATE"),
            ("jogadores", "cpf",             "VARCHAR"),
            ("jogadores", "rg",              "VARCHAR"),
            ("jogos",     "categoria",        "VARCHAR"),
        ]
        for tabela, coluna, tipo in migrações:
            try:
                if is_sqlite:
                    conn.execute(_text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"))
                else:
                    conn.execute(_text(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {coluna} {tipo}"))
                conn.commit()
            except Exception:
                try: conn.rollback()
                except: pass

        # Remove o índice único de data em jogos (para permitir múltiplos eventos por dia)
        if is_sqlite:
            # SQLite: verifica se a tabela tem unique constraint em data e recria sem ela
            try:
                row = conn.execute(_text("SELECT sql FROM sqlite_master WHERE type='table' AND name='jogos'")).fetchone()
                if row and 'UNIQUE' in (row[0] or '').upper():
                    conn.execute(_text("PRAGMA foreign_keys=OFF"))
                    conn.execute(_text("""
                        CREATE TABLE jogos_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            data DATE NOT NULL,
                            categoria VARCHAR,
                            observacao VARCHAR,
                            criado_em DATETIME DEFAULT (CURRENT_TIMESTAMP)
                        )
                    """))
                    conn.execute(_text("INSERT INTO jogos_new (id,data,categoria,observacao,criado_em) SELECT id,data,categoria,observacao,criado_em FROM jogos"))
                    conn.execute(_text("DROP TABLE jogos"))
                    conn.execute(_text("ALTER TABLE jogos_new RENAME TO jogos"))
                    conn.execute(_text("PRAGMA foreign_keys=ON"))
                    conn.commit()
            except Exception:
                try: conn.rollback()
                except: pass
        else:
            for idx_name in ("ix_jogos_data", "uq_jogos_data", "jogos_data_key"):
                try:
                    conn.execute(_text(f"DROP INDEX IF EXISTS {idx_name}"))
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except: pass

_migrar()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

app = FastAPI(title="Volei App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")


# ── Schemas ───────────────────────────────────────────────────────────────────

class JogadorCreate(BaseModel):
    nome: str
    tipo: str
    telefone: Optional[str] = None
    posicao: Optional[str] = None
    numero_camisa: Optional[int] = None
    data_nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None

class JogadorUpdate(BaseModel):
    nome: Optional[str] = None
    tipo: Optional[str] = None
    telefone: Optional[str] = None
    posicao: Optional[str] = None
    numero_camisa: Optional[int] = None
    data_nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None
    ativo: Optional[bool] = None

class JogoCreate(BaseModel):
    data: date
    categoria: Optional[str] = None
    observacao: Optional[str] = None

class ParticipacaoCreate(BaseModel):
    jogador_id: int

class PagamentoCreate(BaseModel):
    jogador_id: int
    valor: float
    data_pagamento: date
    referencia: Optional[str] = None
    tipo: str
    observacao: Optional[str] = None

class SaidaCreate(BaseModel):
    descricao: str
    valor: float
    data: date
    categoria: Optional[str] = None
    observacao: Optional[str] = None


# ── Jogadores ─────────────────────────────────────────────────────────────────

@app.get("/api/jogadores")
def listar_jogadores(tipo: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.Jogador)
    if tipo:
        q = q.filter(models.Jogador.tipo == tipo)
    jogadores = q.order_by(models.Jogador.nome).all()
    return [
        {
            "id": j.id, "nome": j.nome, "tipo": j.tipo,
            "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao,
            "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": j.cpf,
            "rg": j.rg,
            "criado_em": j.criado_em.isoformat() if j.criado_em else None,
        }
        for j in jogadores
    ]

@app.post("/api/jogadores", status_code=201)
def criar_jogador(data: JogadorCreate, db: Session = Depends(get_db)):
    if data.tipo not in ("mensalista", "avulso"):
        raise HTTPException(400, "tipo deve ser 'mensalista' ou 'avulso'")
    j = models.Jogador(**data.model_dump())
    db.add(j)
    db.commit()
    db.refresh(j)
    return {"id": j.id, "nome": j.nome, "tipo": j.tipo, "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao, "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": j.cpf, "rg": j.rg}

@app.put("/api/jogadores/{jogador_id}")
def atualizar_jogador(jogador_id: int, data: JogadorUpdate, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(j, field, value)
    db.commit()
    return {"id": j.id, "nome": j.nome, "tipo": j.tipo, "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao, "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": j.cpf, "rg": j.rg}

@app.delete("/api/jogadores/{jogador_id}")
def desativar_jogador(jogador_id: int, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    j.ativo = False
    db.commit()
    return {"ok": True}


# ── Jogos ─────────────────────────────────────────────────────────────────────

@app.get("/api/jogos")
def listar_jogos(db: Session = Depends(get_db)):
    jogos = db.query(models.Jogo).order_by(models.Jogo.data.desc()).all()
    result = []
    for jogo in jogos:
        avulsos = [
            {
                "id": p.jogador.id,
                "nome": p.jogador.nome,
                "participacao_id": p.id,
            }
            for p in jogo.participacoes
        ]
        result.append({
            "id": jogo.id,
            "data": jogo.data.isoformat(),
            "categoria": jogo.categoria,
            "observacao": jogo.observacao,
            "avulsos": avulsos,
            "total_avulsos": len(avulsos),
        })
    return result

@app.post("/api/jogos", status_code=201)
def criar_jogo(data: JogoCreate, db: Session = Depends(get_db)):
    # Permite múltiplos eventos na mesma data (ex: jogo semanal + amistoso)
    jogo = models.Jogo(**data.model_dump())
    db.add(jogo)
    db.commit()
    db.refresh(jogo)
    return {"id": jogo.id, "data": jogo.data.isoformat(), "categoria": jogo.categoria, "observacao": jogo.observacao}

@app.get("/api/jogos/{jogo_id}")
def obter_jogo(jogo_id: int, db: Session = Depends(get_db)):
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    avulsos = [
        {"id": p.jogador.id, "nome": p.jogador.nome, "participacao_id": p.id}
        for p in jogo.participacoes
    ]
    return {"id": jogo.id, "data": jogo.data.isoformat(), "categoria": jogo.categoria, "observacao": jogo.observacao, "avulsos": avulsos}

@app.post("/api/jogos/{jogo_id}/participacoes", status_code=201)
def adicionar_avulso(jogo_id: int, data: ParticipacaoCreate, db: Session = Depends(get_db)):
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    jogador = db.query(models.Jogador).filter(models.Jogador.id == data.jogador_id).first()
    if not jogador:
        raise HTTPException(404, "Jogador não encontrado")
    if jogador.tipo != "avulso":
        raise HTTPException(400, "Apenas avulsos são adicionados a jogos")
    existente = db.query(models.ParticipacaoAvulso).filter(
        models.ParticipacaoAvulso.jogo_id == jogo_id,
        models.ParticipacaoAvulso.jogador_id == data.jogador_id
    ).first()
    if existente:
        raise HTTPException(400, "Jogador já registrado neste jogo")
    p = models.ParticipacaoAvulso(jogo_id=jogo_id, jogador_id=data.jogador_id)
    db.add(p)
    db.commit()
    return {"ok": True}

@app.delete("/api/jogos/{jogo_id}/participacoes/{jogador_id}")
def remover_avulso(jogo_id: int, jogador_id: int, db: Session = Depends(get_db)):
    p = db.query(models.ParticipacaoAvulso).filter(
        models.ParticipacaoAvulso.jogo_id == jogo_id,
        models.ParticipacaoAvulso.jogador_id == jogador_id
    ).first()
    if not p:
        raise HTTPException(404, "Participação não encontrada")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ── Pagamentos ────────────────────────────────────────────────────────────────

@app.get("/api/pagamentos")
def listar_pagamentos(
    jogador_id: Optional[int] = None,
    mes: Optional[int] = None,
    ano: Optional[int] = None,
    db: Session = Depends(get_db)
):
    q = db.query(models.Pagamento)
    if jogador_id:
        q = q.filter(models.Pagamento.jogador_id == jogador_id)
    if mes:
        q = q.filter(extract("month", models.Pagamento.data_pagamento) == mes)
    if ano:
        q = q.filter(extract("year", models.Pagamento.data_pagamento) == ano)
    pagamentos = q.order_by(models.Pagamento.data_pagamento.desc()).all()
    return [
        {
            "id": p.id,
            "jogador_id": p.jogador_id,
            "jogador_nome": p.jogador.nome,
            "valor": p.valor,
            "data_pagamento": p.data_pagamento.isoformat(),
            "referencia": p.referencia,
            "tipo": p.tipo,
            "observacao": p.observacao,
        }
        for p in pagamentos
    ]

@app.post("/api/pagamentos", status_code=201)
def criar_pagamento(data: PagamentoCreate, db: Session = Depends(get_db)):
    if data.tipo not in ("mensalidade", "avulso"):
        raise HTTPException(400, "tipo deve ser 'mensalidade' ou 'avulso'")
    jogador = db.query(models.Jogador).filter(models.Jogador.id == data.jogador_id).first()
    if not jogador:
        raise HTTPException(404, "Jogador não encontrado")
    p = models.Pagamento(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id, "jogador_nome": jogador.nome, "valor": p.valor, "data_pagamento": p.data_pagamento.isoformat()}

@app.delete("/api/pagamentos/{pagamento_id}")
def deletar_pagamento(pagamento_id: int, db: Session = Depends(get_db)):
    p = db.query(models.Pagamento).filter(models.Pagamento.id == pagamento_id).first()
    if not p:
        raise HTTPException(404, "Pagamento não encontrado")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ── Saídas ────────────────────────────────────────────────────────────────────

@app.get("/api/saidas")
def listar_saidas(mes: Optional[int] = None, ano: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(models.Saida)
    if mes:
        q = q.filter(extract("month", models.Saida.data) == mes)
    if ano:
        q = q.filter(extract("year", models.Saida.data) == ano)
    saidas = q.order_by(models.Saida.data.desc()).all()
    return [
        {
            "id": s.id, "descricao": s.descricao, "valor": s.valor,
            "data": s.data.isoformat(), "categoria": s.categoria, "observacao": s.observacao,
        }
        for s in saidas
    ]

@app.post("/api/saidas", status_code=201)
def criar_saida(data: SaidaCreate, db: Session = Depends(get_db)):
    s = models.Saida(**data.model_dump())
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "descricao": s.descricao, "valor": s.valor, "data": s.data.isoformat()}

@app.delete("/api/saidas/{saida_id}")
def deletar_saida(saida_id: int, db: Session = Depends(get_db)):
    s = db.query(models.Saida).filter(models.Saida.id == saida_id).first()
    if not s:
        raise HTTPException(404, "Saída não encontrada")
    db.delete(s)
    db.commit()
    return {"ok": True}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def dashboard(mes: Optional[int] = None, ano: Optional[int] = None, db: Session = Depends(get_db)):
    hoje = date.today()
    mes = mes or hoje.month
    ano = ano or hoje.year

    MESES_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

    # Mensalistas
    mensalistas = db.query(models.Jogador).filter(
        models.Jogador.tipo == "mensalista",
        models.Jogador.ativo == True
    ).order_by(models.Jogador.nome).all()

    pagamentos_mes = db.query(models.Pagamento).filter(
        models.Pagamento.tipo == "mensalidade",
        extract("month", models.Pagamento.data_pagamento) == mes,
        extract("year", models.Pagamento.data_pagamento) == ano,
    ).all()

    mensalistas_lista = []
    for m in mensalistas:
        pagamentos_m = [
            p for p in pagamentos_mes
            if p.jogador_id == m.id and not (p.observacao or "").startswith("PENDENTE|")
        ]
        valor_pago = sum(p.valor for p in pagamentos_m)
        ultimo_pag = max(pagamentos_m, key=lambda p: p.data_pagamento) if pagamentos_m else None

        if valor_pago >= 120:
            status = "quitado"
        elif valor_pago > 0:
            status = "parcial"
        else:
            status = "em_aberto"

        mensalistas_lista.append({
            "id": m.id,
            "nome": m.nome,
            "status": status,
            "valor_pago": valor_pago,
            "valor_devido": 120.0,
            "valor_falta": max(0, 120.0 - valor_pago),
            "data_pagamento": ultimo_pag.data_pagamento.isoformat() if ultimo_pag else None,
        })

    # Avulsos do mês
    jogos_mes = db.query(models.Jogo).filter(
        extract("month", models.Jogo.data) == mes,
        extract("year", models.Jogo.data) == ano,
    ).all()

    avulsos = db.query(models.Jogador).filter(
        models.Jogador.tipo == "avulso",
        models.Jogador.ativo == True
    ).order_by(models.Jogador.nome).all()

    pagamentos_avulso_mes = db.query(models.Pagamento).filter(
        models.Pagamento.tipo == "avulso",
        extract("month", models.Pagamento.data_pagamento) == mes,
        extract("year", models.Pagamento.data_pagamento) == ano,
    ).all()

    avulsos_resumo = []
    for av in avulsos:
        jogos_count = sum(
            1 for jogo in jogos_mes
            if any(p.jogador_id == av.id for p in jogo.participacoes)
        )
        valor_pago = sum(p.valor for p in pagamentos_avulso_mes if p.jogador_id == av.id)
        valor_devido = jogos_count * 35
        if jogos_count > 0 or valor_pago > 0:
            avulsos_resumo.append({
                "id": av.id,
                "nome": av.nome,
                "jogos": jogos_count,
                "valor_devido": valor_devido,
                "valor_pago": valor_pago,
                "pendente": max(0, valor_devido - valor_pago),
            })

    # Financeiro
    total_entradas_mensalidade = sum(
        p.valor for p in pagamentos_mes
        if not (p.observacao or "").startswith("PENDENTE|")
    )
    total_entradas_avulso = sum(
        p.valor for p in pagamentos_avulso_mes
        if not (p.observacao or "").startswith("PENDENTE|")
    )
    total_entradas = total_entradas_mensalidade + total_entradas_avulso

    saidas_mes = db.query(models.Saida).filter(
        extract("month", models.Saida.data) == mes,
        extract("year", models.Saida.data) == ano,
    ).all()
    total_saidas = sum(s.valor for s in saidas_mes)

    # Aniversariantes do mês
    todos_jogadores_ativos = db.query(models.Jogador).filter(
        models.Jogador.ativo == True,
        models.Jogador.data_nascimento != None
    ).all()
    aniversariantes = []
    for j in todos_jogadores_ativos:
        if j.data_nascimento and j.data_nascimento.month == mes:
            aniversariantes.append({
                "id": j.id,
                "nome": j.nome,
                "tipo": j.tipo,
                "data_nascimento": j.data_nascimento.isoformat(),
                "dia": j.data_nascimento.day,
            })
    aniversariantes.sort(key=lambda x: x["dia"])

    # Todos os jogos do mês para o calendário
    todos_jogos_mes = db.query(models.Jogo).filter(
        extract("month", models.Jogo.data) == mes,
        extract("year", models.Jogo.data) == ano,
    ).order_by(models.Jogo.data).all()

    return {
        "mes": mes,
        "ano": ano,
        "mes_nome": f"{MESES_PT[mes]} {ano}",
        "mensalistas_lista": mensalistas_lista,
        "avulsos_resumo": avulsos_resumo,
        "jogos_mes": [{"id": j.id, "data": j.data.isoformat(), "categoria": j.categoria, "observacao": j.observacao} for j in todos_jogos_mes],
        "aniversariantes": aniversariantes,
        "total_entradas": total_entradas,
        "total_saidas": total_saidas,
        "saldo": total_entradas - total_saidas,
    }


# ── Página pública do jogador ────────────────────────────────────────────────

@app.get("/pagar/{jogador_id}")
def pagina_pagar(jogador_id: int):
    return FileResponse("static/pagar.html")

@app.get("/api/jogadores/{jogador_id}/info-pagamento")
def info_pagamento_jogador(jogador_id: int, db: Session = Depends(get_db)):
    """Retorna info do jogador para a página de pagamento."""
    j = db.query(models.Jogador).filter(
        models.Jogador.id == jogador_id,
        models.Jogador.ativo == True
    ).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")

    hoje = date.today()
    mes, ano = hoje.month, hoje.year
    valor_devido = 120.0 if j.tipo == "mensalista" else 35.0

    return {
        "id": j.id,
        "nome": j.nome,
        "tipo": j.tipo,
        "mes": mes,
        "ano": ano,
        "valor_devido": valor_devido,
    }

@app.post("/api/comprovante/enviar")
async def enviar_comprovante_jogador(
    file: UploadFile = File(...),
    jogador_id: int = Form(...),
    nome_comprovante: str = Form(default=""),
    valor: str = Form(default=""),
    data_iso: str = Form(default=""),
    db: Session = Depends(get_db)
):
    """Recebe comprovante enviado pelo jogador e cria pendência para aprovação."""
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")

    content = await file.read()

    # Se não veio dados lidos, tenta ler agora
    valor_float = None
    data_pagamento = None
    if valor:
        try: valor_float = float(valor)
        except: pass
    if data_iso:
        try: data_pagamento = date.fromisoformat(data_iso)
        except: pass

    if not valor_float or not data_pagamento:
        lido = ler_comprovante(content, file.filename)
        valor_float = valor_float or lido.get("valor")
        data_pagamento = data_pagamento or (
            date.fromisoformat(lido["data_iso"]) if lido.get("data_iso") else date.today()
        )
        nome_comprovante = nome_comprovante or lido.get("nome") or ""

    # Salva arquivo no banco
    ext = file.filename.split('.')[-1].lower() if '.' in file.filename else 'pdf'
    mimetypes_map = {'pdf': 'application/pdf', 'png': 'image/png',
                     'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}
    mimetype = mimetypes_map.get(ext, 'application/octet-stream')

    arquivo = models.ArquivoComprovante(
        nome_original=file.filename,
        conteudo=content,
        mimetype=mimetype,
    )
    db.add(arquivo)
    db.flush()  # gera o arquivo.id sem fechar a transação

    # Cria pagamento pendente
    hoje = date.today()
    MESES_PT = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    referencia = f"{MESES_PT[hoje.month]} {hoje.year}" if j.tipo == "mensalista" else str(date.today())

    p = models.Pagamento(
        jogador_id=jogador_id,
        valor=valor_float or 0,
        data_pagamento=data_pagamento or date.today(),
        referencia=referencia,
        tipo=j.tipo if j.tipo == "avulso" else "mensalidade",
        observacao=f"PENDENTE|comprovante_id:{arquivo.id}|nome:{nome_comprovante}",
    )
    db.add(p)
    db.commit()
    return {"ok": True}

@app.get("/api/pendentes")
def listar_pendentes(db: Session = Depends(get_db)):
    """Lista pagamentos pendentes de aprovação (enviados pelos jogadores)."""
    pendentes = db.query(models.Pagamento).filter(
        models.Pagamento.observacao.like("PENDENTE|%")
    ).order_by(models.Pagamento.criado_em.desc()).all()

    result = []
    for p in pendentes:
        # Extrai info do campo observacao
        partes = {k: v for k, v in (x.split(':', 1) for x in p.observacao.split('|') if ':' in x)}
        comprovante_id = partes.get('comprovante_id', '')
        result.append({
            "id": p.id,
            "jogador_id": p.jogador_id,
            "jogador_nome": p.jogador.nome,
            "valor": p.valor,
            "data_pagamento": p.data_pagamento.isoformat(),
            "referencia": p.referencia,
            "tipo": p.tipo,
            "nome_comprovante": partes.get('nome', ''),
            "comprovante_url": f"/api/comprovante/arquivo/{comprovante_id}" if comprovante_id else None,
            "criado_em": p.criado_em.isoformat() if p.criado_em else None,
        })
    return result

@app.post("/api/pendentes/{pagamento_id}/aprovar")
def aprovar_pendente(pagamento_id: int, db: Session = Depends(get_db)):
    """Aprova um pagamento pendente."""
    p = db.query(models.Pagamento).filter(models.Pagamento.id == pagamento_id).first()
    if not p:
        raise HTTPException(404, "Pagamento não encontrado")
    p.observacao = p.observacao.replace("PENDENTE|", "")
    db.commit()
    return {"ok": True}

@app.delete("/api/pendentes/{pagamento_id}/rejeitar")
def rejeitar_pendente(pagamento_id: int, db: Session = Depends(get_db)):
    """Rejeita e remove um pagamento pendente."""
    p = db.query(models.Pagamento).filter(models.Pagamento.id == pagamento_id).first()
    if not p:
        raise HTTPException(404, "Pagamento não encontrado")
    # Remove o arquivo do banco
    partes = {k: v for k, v in (x.split(':', 1) for x in p.observacao.split('|') if ':' in x)}
    comprovante_id = partes.get('comprovante_id')
    if comprovante_id:
        arq = db.query(models.ArquivoComprovante).filter(
            models.ArquivoComprovante.id == int(comprovante_id)
        ).first()
        if arq:
            db.delete(arq)
    db.delete(p)
    db.commit()
    return {"ok": True}

@app.get("/api/comprovante/arquivo/{arquivo_id}")
def servir_comprovante(arquivo_id: int, db: Session = Depends(get_db)):
    arq = db.query(models.ArquivoComprovante).filter(
        models.ArquivoComprovante.id == arquivo_id
    ).first()
    if not arq:
        raise HTTPException(404, "Arquivo não encontrado")
    return Response(content=arq.conteudo, media_type=arq.mimetype)

@app.get("/api/config/ip")
def get_ip():
    # Se houver uma URL pública configurada (produção), usa ela
    public_url = os.getenv("PUBLIC_URL")
    if public_url:
        return {"ip": None, "porta": None, "hostname": None, "scheme": "https", "public_url": public_url}

    # Ambiente local: detecta IP e hostname
    try:
        hostname = subprocess.check_output(
            ["scutil", "--get", "LocalHostName"], text=True
        ).strip()
        local_hostname = f"{hostname}.local"
    except:
        local_hostname = None
    return {"ip": get_local_ip(), "porta": 8000, "hostname": local_hostname, "scheme": "https", "public_url": None}

@app.get("/instalar-certificado")
def pagina_certificado():
    return FileResponse("static/instalar_cert.html")

@app.get("/voleizou.crt")
def baixar_certificado():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "voleizou.crt")
    if not os.path.exists(path):
        raise HTTPException(404, "Certificado não encontrado")
    return FileResponse(path, filename="voleizou.crt", media_type="application/x-x509-ca-cert")

# ── Comprovante ───────────────────────────────────────────────────────────────

@app.post("/api/comprovante/parse")
async def parse_comprovante(file: UploadFile = File(...), db: Session = Depends(get_db)):
    EXTENSOES_VALIDAS = ('.pdf', '.png', '.jpg', '.jpeg', '.webp')
    if not any(file.filename.lower().endswith(ext) for ext in EXTENSOES_VALIDAS):
        raise HTTPException(400, "Envie um arquivo PDF ou imagem (PNG, JPG)")

    file_bytes = await file.read()
    dados = ler_comprovante(file_bytes, file.filename)

    if dados.get("erro"):
        raise HTTPException(422, f"Erro ao ler PDF: {dados['erro']}")

    # Tenta encontrar o jogador pelo nome
    jogador_sugerido = None
    if dados.get("nome"):
        nome_lower = dados["nome"].lower()
        jogadores = db.query(models.Jogador).filter(models.Jogador.ativo == True).all()
        # Busca por correspondência parcial no nome
        for j in jogadores:
            partes = j.nome.lower().split()
            if any(p in nome_lower for p in partes if len(p) > 2):
                jogador_sugerido = {"id": j.id, "nome": j.nome, "tipo": j.tipo}
                break

    return {
        "nome_comprovante": dados.get("nome"),
        "valor": dados.get("valor"),
        "data_iso": dados.get("data_iso"),
        "data_original": dados.get("data_original"),
        "banco": dados.get("banco"),
        "jogador_sugerido": jogador_sugerido,
        "aviso": dados.get("aviso"),
    }


@app.get("/api/caixa")
def caixa_geral(db: Session = Depends(get_db)):
    """Resumo financeiro geral — acumulado de todos os meses."""

    # Todas as entradas aprovadas (observacao NULL ou sem prefixo PENDENTE)
    todos_pagamentos = db.query(models.Pagamento).filter(
        (models.Pagamento.observacao == None) |
        (~models.Pagamento.observacao.like("PENDENTE|%"))
    ).all()
    total_entradas = sum(p.valor for p in todos_pagamentos)

    # Todas as saídas
    todas_saidas = db.query(models.Saida).all()
    total_saidas = sum(s.valor for s in todas_saidas)

    # Resumo por mês (últimos 12 meses com movimento)
    from collections import defaultdict
    por_mes = defaultdict(lambda: {"entradas": 0.0, "saidas": 0.0})
    for p in todos_pagamentos:
        chave = f"{p.data_pagamento.year}-{p.data_pagamento.month:02d}"
        por_mes[chave]["entradas"] += p.valor
    for s in todas_saidas:
        chave = f"{s.data.year}-{s.data.month:02d}"
        por_mes[chave]["saidas"] += s.valor

    MESES_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

    historico = []
    for chave in sorted(por_mes.keys(), reverse=True)[:12]:
        ano, mes = int(chave.split("-")[0]), int(chave.split("-")[1])
        e = por_mes[chave]["entradas"]
        s = por_mes[chave]["saidas"]
        historico.append({
            "chave": chave,
            "mes_nome": f"{MESES_PT[mes]} {ano}",
            "entradas": e,
            "saidas": s,
            "saldo": e - s,
        })

    return {
        "total_entradas": total_entradas,
        "total_saidas": total_saidas,
        "saldo_caixa": total_entradas - total_saidas,
        "historico": historico,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

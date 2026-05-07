from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import extract
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime
import models, hmac, hashlib, bcrypt, re, smtplib
from email.mime.text import MIMEText
from cryptography.fernet import Fernet, InvalidToken
from leitor_comprovante import ler_comprovante
from database import engine, get_db, is_sqlite, SessionLocal
import os, socket, subprocess

models.Base.metadata.create_all(bind=engine)

# ── Autenticação ──────────────────────────────────────────────────────────────

SECRET_KEY      = os.getenv("SECRET_KEY",      "volei-dev-secret-2024")
ADMIN_USER      = os.getenv("ADMIN_USER",      "admin")
ADMIN_PASS      = os.getenv("ADMIN_PASSWORD",  "volei123")
ENCRYPTION_KEY  = os.getenv("ENCRYPTION_KEY",  "")

# Menus do sistema — adicione aqui sempre que criar uma nova seção
MENUS_SLUGS = [
    "dashboard", "jogadores", "jogos", "calendario", "pagamentos",
    "saidas", "entradas", "caixa", "pendencias", "pendentes", "config",
    "config-valores",
]

# Chaves de configuração com seus defaults
CONFIG_DEFAULTS = {
    "valor_mensalidade": "120.0",
    "valor_avulso":      "35.0",
}
TIPOS_USUARIO = ["admin", "mensalista", "avulso"]

# ── Helpers de criptografia (CPF / RG) ───────────────────────────────────────

def _fernet() -> Fernet | None:
    if not ENCRYPTION_KEY:
        return None
    try:
        return Fernet(ENCRYPTION_KEY.encode())
    except Exception:
        return None

def _encrypt(value: str | None) -> str | None:
    """Criptografa um valor antes de salvar no banco. Retorna None se value for None."""
    if not value:
        return value
    f = _fernet()
    if not f:
        return value  # sem chave configurada: salva em claro (ambiente local)
    return f.encrypt(value.encode()).decode()

def _decrypt(value: str | None) -> str | None:
    """Descriptografa um valor lido do banco. Retorna o valor original se não estiver criptografado."""
    if not value:
        return value
    f = _fernet()
    if not f:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        return value  # já estava em texto puro (dados antigos)

def _criar_token(usuario: str) -> str:
    payload = f"{usuario}:{date.today().toordinal()}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verificar_token(token: str, max_dias: int = 30) -> bool:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        ordinal = int(payload.rsplit(":", 1)[1])
        return 0 <= (date.today().toordinal() - ordinal) <= max_dias
    except Exception:
        return False

_PUBLICOS_EXATOS   = {"/", "/api/login", "/api/logout", "/api/me",
                      "/api/comprovante/enviar", "/instalar-certificado", "/voleizou.crt",
                      "/cadastro", "/definir-senha",
                      "/recuperar-senha", "/redefinir-senha",
                      "/api/cadastro", "/api/definir-senha-convite", "/api/verificar-convite",
                      "/api/convite/link", "/cadastro-enviado",
                      "/api/recuperar-senha", "/api/verificar-reset", "/api/redefinir-senha"}
_PUBLICOS_PREFIXO  = ("/static/", "/pagar/")
_PUBLICOS_SUFIXO   = ("/info-pagamento",)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (path in _PUBLICOS_EXATOS
                or any(path.startswith(p) for p in _PUBLICOS_PREFIXO)
                or any(path.endswith(s) for s in _PUBLICOS_SUFIXO)):
            return await call_next(request)
        token = request.cookies.get("volei_sessao", "")
        if not _verificar_token(token):
            return JSONResponse({"detail": "Não autorizado"}, status_code=401)
        return await call_next(request)

# ── Migrações automáticas (adiciona colunas novas sem recriar tabelas) ────────
def _migrar():
    from database import engine as _engine, is_sqlite
    with _engine.connect() as conn:
        from sqlalchemy import text as _text
        migrações = [
            ("entradas",  "id",              "INTEGER"),  # força criação via models
            ("jogadores", "posicao",         "VARCHAR"),
            ("jogadores", "numero_camisa",    "INTEGER"),
            ("jogadores", "data_nascimento",  "DATE"),
            ("jogadores", "cpf",             "VARCHAR"),
            ("jogadores", "rg",              "VARCHAR"),
            ("jogadores", "email",           "VARCHAR"),
            ("jogos",     "categoria",             "VARCHAR"),
            ("jogos",     "mensalistas_ausentes",  "VARCHAR"),
            ("jogos",     "valor",                 "REAL"),
            ("jogos",     "status",                "VARCHAR"),
            ("jogos",     "endereco",              "VARCHAR"),
            ("usuarios",  "tipo",                  "VARCHAR"),
            ("usuarios",  "jogador_id",            "INTEGER"),
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

def _seed_admin():
    """Cria o usuário admin padrão se a tabela de usuários estiver vazia."""
    db = SessionLocal()
    try:
        if db.query(models.Usuario).count() == 0:
            senha_hash = bcrypt.hashpw(ADMIN_PASS.encode(), bcrypt.gensalt()).decode()
            u = models.Usuario(nome="Admin", usuario=ADMIN_USER, senha_hash=senha_hash)
            db.add(u)
            db.commit()
    except Exception:
        pass
    finally:
        db.close()

_seed_admin()

def _seed_permissoes():
    """Cria entradas de permissão para qualquer menu/tipo que ainda não exista.
    Novos menus nascem com admin=True e demais=False."""
    db = SessionLocal()
    try:
        # Garante tipo='admin' nos usuários que não têm o campo preenchido
        db.execute(__import__('sqlalchemy').text(
            "UPDATE usuarios SET tipo='admin' WHERE tipo IS NULL OR tipo=''"
        ))
        db.commit()
        for tipo in TIPOS_USUARIO:
            for slug in MENUS_SLUGS:
                existe = db.query(models.Permissao).filter_by(
                    tipo_usuario=tipo, menu_slug=slug
                ).first()
                if not existe:
                    db.add(models.Permissao(
                        tipo_usuario=tipo,
                        menu_slug=slug,
                        permitido=(tipo == "admin"),
                    ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_seed_permissoes()

def _seed_config():
    """Garante que todas as chaves de CONFIG_DEFAULTS existam no banco."""
    db = SessionLocal()
    try:
        for chave, valor in CONFIG_DEFAULTS.items():
            if not db.query(models.Configuracao).filter_by(chave=chave).first():
                db.add(models.Configuracao(chave=chave, valor=valor))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_seed_config()

def _reset_admin_pass():
    """Se RESET_ADMIN_PASS estiver definido, redefine a senha do admin e remove a var."""
    nova_senha = os.getenv("RESET_ADMIN_PASS", "")
    if not nova_senha:
        return
    db = SessionLocal()
    try:
        u = db.query(models.Usuario).filter(models.Usuario.tipo == "admin").first()
        if u:
            u.senha_hash = bcrypt.hashpw(nova_senha.encode(), bcrypt.gensalt()).decode()
            db.commit()
            print(f"[RESET] Senha do admin '{u.usuario}' redefinida via RESET_ADMIN_PASS.")
        else:
            print("[RESET] Nenhum usuário admin encontrado.")
    finally:
        db.close()

_reset_admin_pass()

def _corrigir_tipo_jogadores_admin():
    """Vincula admins sem jogador_id a jogadores pelo nome, e corrige tipo='admin' para 'mensalista'."""
    db = SessionLocal()
    try:
        admins = db.query(models.Usuario).filter(models.Usuario.tipo == "admin").all()
        for u in admins:
            # Auto-vínculo por nome exato (se ainda não vinculado)
            if not u.jogador_id:
                j = db.query(models.Jogador).filter(models.Jogador.nome == u.nome).first()
                if j:
                    u.jogador_id = j.id
                    print(f"[INIT] Admin '{u.usuario}' vinculado automaticamente ao jogador '{j.nome}'.")
            # Corrige tipo do jogador vinculado
            if u.jogador_id:
                j = db.query(models.Jogador).filter(models.Jogador.id == u.jogador_id).first()
                if j and j.tipo not in ("mensalista", "avulso"):
                    j.tipo = "mensalista"
                    print(f"[INIT] Jogador '{j.nome}' corrigido para mensalista.")
        db.commit()
    except Exception as e:
        print(f"[INIT] Erro ao corrigir tipos: {e}")
    finally:
        db.close()

_corrigir_tipo_jogadores_admin()

def _corrigir_usernames():
    """Renomeia usuários cujo username não segue o padrão nome.sobrenome."""
    import unicodedata
    def _normalizar(s: str) -> str:
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r'[^a-z0-9]', '', s.lower())

    db = SessionLocal()
    try:
        usuarios = db.query(models.Usuario).all()
        for u in usuarios:
            if not u.nome or u.usuario == ADMIN_USER:
                continue
            partes = u.nome.strip().split()
            primeiro = _normalizar(partes[0])
            ultimo   = _normalizar(partes[-1]) if len(partes) > 1 else ""
            esperado = f"{primeiro}.{ultimo}" if ultimo and ultimo != primeiro else primeiro
            if not esperado:
                continue
            # Só corrige se o username atual NÃO começa com o padrão esperado
            if not u.usuario.startswith(esperado.split('.')[0] + '.') and u.usuario != esperado:
                # Gera username único
                candidato, i = esperado, 2
                while db.query(models.Usuario).filter(
                    models.Usuario.usuario == candidato,
                    models.Usuario.id != u.id
                ).first():
                    candidato = f"{esperado}{i}"; i += 1
                print(f"[INIT] Username '{u.usuario}' → '{candidato}'")
                u.usuario = candidato
        db.commit()
    except Exception as e:
        print(f"[INIT] Erro ao corrigir usernames: {e}")
        db.rollback()
    finally:
        db.close()

_corrigir_usernames()

def _seed_pendencias_eventos():
    """Na inicialização, gera pendências faltantes para jogos confirmados/realizados não-semanais."""
    db = SessionLocal()
    try:
        _regenerar_pendencias_todos_eventos(db)
        print("[INIT] Pendências de eventos verificadas/geradas.")
    except Exception as e:
        print(f"[INIT] Erro ao gerar pendências: {e}")
    finally:
        db.close()

_seed_pendencias_eventos()

def _get_config(db: Session) -> dict:
    """Retorna dict com os valores de configuração convertidos para float."""
    rows = db.query(models.Configuracao).all()
    cfg = {r.chave: float(r.valor) for r in rows}
    # garante defaults caso seed ainda não tenha rodado
    for chave, valor in CONFIG_DEFAULTS.items():
        cfg.setdefault(chave, float(valor))
    return cfg

def _migrar_criptografia():
    """Criptografa CPF/RG existentes que ainda estão em texto puro."""
    f = _fernet()
    if not f:
        return  # chave não configurada — nada a fazer
    db = SessionLocal()
    try:
        jogadores = db.query(models.Jogador).filter(
            (models.Jogador.cpf != None) | (models.Jogador.rg != None)
        ).all()
        for j in jogadores:
            if j.cpf and not j.cpf.startswith("gA"):   # "gA" = prefixo de token Fernet
                j.cpf = f.encrypt(j.cpf.encode()).decode()
            if j.rg and not j.rg.startswith("gA"):
                j.rg = f.encrypt(j.rg.encode()).decode()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

_migrar_criptografia()

# ── Helpers de convite / setup de senha ──────────────────────────────────────

def _gerar_token_setup(usuario_id: int) -> str:
    payload = f"setup:{usuario_id}:{date.today().toordinal()}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verificar_token_setup(token: str) -> int | None:
    """Retorna usuario_id se token válido e dentro do prazo, None caso contrário."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        parts = payload.split(":")   # ["setup", user_id, ordinal]
        if len(parts) != 3 or parts[0] != "setup":
            return None
        ordinal = int(parts[2])
        if abs(date.today().toordinal() - ordinal) > 7:   # expira em 7 dias
            return None
        return int(parts[1])
    except Exception:
        return None

def _gerar_token_reset(usuario_id: int) -> str:
    payload = f"reset:{usuario_id}:{date.today().toordinal()}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verificar_token_reset(token: str) -> int | None:
    """Retorna usuario_id se token válido e dentro de 1 dia, None caso contrário."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        parts = payload.split(":")
        if len(parts) != 3 or parts[0] != "reset":
            return None
        ordinal = int(parts[2])
        if abs(date.today().toordinal() - ordinal) > 1:
            return None
        return int(parts[1])
    except Exception:
        return None

def _gerar_usuario(email: str, db: Session, nome: str = "") -> str:
    """Gera username no formato nome.sobrenome a partir do nome completo."""
    import unicodedata
    def _normalizar(s: str) -> str:
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r'[^a-z0-9]', '', s.lower())

    if nome and nome.strip():
        partes = nome.strip().split()
        primeiro = _normalizar(partes[0])
        ultimo   = _normalizar(partes[-1]) if len(partes) > 1 else ""
        base = f"{primeiro}.{ultimo}" if ultimo and ultimo != primeiro else primeiro
    else:
        base = re.sub(r'[^a-z0-9._-]', '', email.split("@")[0].lower()) or "jogador"

    base = base or "jogador"
    username, i = base, 2
    while db.query(models.Usuario).filter_by(usuario=username).first():
        username = f"{base}{i}"; i += 1
    return username

def _enviar_email(to: str, subject: str, body: str):
    """Envia e-mail via Brevo API (HTTP) ou imprime no log se não configurado."""
    import urllib.request as _urlreq, json as _json

    brevo_key = os.getenv("BREVO_API_KEY", "")
    from_email = os.getenv("SMTP_FROM", "noreply@voleizou.com.br")

    if not brevo_key:
        print(f"\n[EMAIL PARA: {to}]\nAssunto: {subject}\n{body}\n")
        return

    payload = _json.dumps({
        "sender":      {"name": "Voleizou", "email": from_email},
        "to":          [{"email": to}],
        "subject":     subject,
        "textContent": body,
    }).encode()
    req = _urlreq.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={
            "api-key":      brevo_key,
            "Content-Type": "application/json",
            "User-Agent":   "Voleizou/1.0",
        },
    )
    try:
        with _urlreq.urlopen(req, timeout=20) as resp:
            print(f"[EMAIL] Enviado via Brevo para {to}: {resp.status}")
    except Exception as exc:
        print(f"[EMAIL] Falha Brevo para {to}: {exc}")
        raise

def _checar_email_telefone(db: Session, email: str | None, telefone: str | None,
                            excluir_id: int = None):
    q_base = db.query(models.Jogador)
    if excluir_id:
        q_base = q_base.filter(models.Jogador.id != excluir_id)
    if email:
        if q_base.filter(models.Jogador.email == email).first():
            raise HTTPException(400, "Este e-mail já está cadastrado para outro jogador")
    if telefone:
        if q_base.filter(models.Jogador.telefone == telefone).first():
            raise HTTPException(400, "Este telefone já está cadastrado para outro jogador")

def _status_efetivo(data_jogo, status_raw: str | None) -> str:
    """Retorna o status real do jogo, aplicando regra de auto-Realizado para datas passadas."""
    s = status_raw or "Planejado"
    if s in ("Planejado", "Confirmado") and data_jogo < date.today():
        return "Realizado"
    return s

def _gerar_pendencias_jogo(db: Session, jogo: models.Jogo):
    """Cria/atualiza Pendencia para cada participante de um jogo não-semanal com valor."""
    cat = (jogo.categoria or "").strip()
    # Jogo Semanal não gera pendências individuais (coberto pela mensalidade)
    if not jogo.valor or cat == "Jogo Semanal":
        return
    ausentes = [int(x) for x in (jogo.mensalistas_ausentes or "").split(",") if x.strip()]
    from sqlalchemy import or_ as _or2
    admin_ids = {row[0] for row in db.query(models.Usuario.jogador_id).filter(
        models.Usuario.tipo == "admin", models.Usuario.jogador_id.isnot(None)
    ).all()}
    mensalistas = db.query(models.Jogador).filter(
        _or2(models.Jogador.tipo == "mensalista", models.Jogador.id.in_(admin_ids)),
        models.Jogador.ativo == True
    ).all()
    presentes   = [m for m in mensalistas if m.id not in ausentes]
    avulsos     = [p.jogador for p in jogo.participacoes]
    participantes = presentes + avulsos
    n = len(participantes)
    if n == 0:
        return
    valor_pessoa = round(jogo.valor / n, 2)
    descricao = f"{cat or 'Evento'} — {jogo.data.strftime('%d/%m/%Y')}"
    from sqlalchemy.exc import IntegrityError
    for j in participantes:
        existe = db.query(models.Pendencia).filter_by(jogador_id=j.id, jogo_id=jogo.id).first()
        if existe:
            existe.valor = valor_pessoa   # atualiza se participantes mudaram
            existe.descricao = descricao
            continue
        p = models.Pendencia(
            jogador_id=j.id, jogo_id=jogo.id, tipo="evento",
            descricao=descricao,
            valor=valor_pessoa,
            referencia=jogo.data.isoformat(),
        )
        db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()


def _regenerar_pendencias_todos_eventos(db: Session):
    """Retroativamente gera pendências para jogos confirmados/realizados que ainda não as têm."""
    jogos = db.query(models.Jogo).filter(
        models.Jogo.status.in_(["Confirmado", "Realizado"]),
        models.Jogo.valor.isnot(None),
        models.Jogo.valor > 0,
    ).all()
    for jogo in jogos:
        cat = (jogo.categoria or "").strip()
        if cat == "Jogo Semanal":
            continue
        _gerar_pendencias_jogo(db, jogo)
    db.commit()

def _usuario_da_sessao(request: Request, db: Session) -> models.Usuario | None:
    token = request.cookies.get("volei_sessao", "")
    if not _verificar_token(token):
        return None
    try:
        usuario_str = token.rsplit(".", 1)[0].rsplit(":", 1)[0]
        return db.query(models.Usuario).filter_by(usuario=usuario_str).first()
    except Exception:
        return None

def _exigir_admin(request: Request, db: Session):
    """Lança 403 se o usuário logado não for admin."""
    u = _usuario_da_sessao(request, db)
    if not u or u.tipo != "admin":
        raise HTTPException(403, "Acesso restrito a administradores")

def _migrar_participacoes_tipo(db: Session, jogador: models.Jogador, tipo_novo: str):
    """Ao mudar o tipo do jogador, ajusta presenças em jogos futuros confirmados
    e recalcula todas as pendências de jogos confirmados com valor."""
    hoje = date.today()
    jogos_futuros = db.query(models.Jogo).filter(
        models.Jogo.status == "Confirmado",
        models.Jogo.data >= hoje,
    ).all()

    if tipo_novo == "avulso":
        # Era mensalista → agora avulso
        # Nos jogos futuros onde estava presente (não ausente): migra para ParticipacaoAvulso
        for jogo in jogos_futuros:
            ausentes = [int(x) for x in (jogo.mensalistas_ausentes or "").split(",") if x.strip()]
            if jogador.id not in ausentes:
                # Estava presente como mensalista → adiciona na lista de ausentes
                ausentes.append(jogador.id)
                jogo.mensalistas_ausentes = ','.join(str(i) for i in ausentes)
                # Cria participação avulso se não existir
                existe = db.query(models.ParticipacaoAvulso).filter_by(
                    jogo_id=jogo.id, jogador_id=jogador.id
                ).first()
                if not existe:
                    db.add(models.ParticipacaoAvulso(jogo_id=jogo.id, jogador_id=jogador.id))
    else:
        # Era avulso → agora mensalista
        # Nos jogos futuros: remove ParticipacaoAvulso (aparecerá como mensalista automaticamente)
        for jogo in jogos_futuros:
            p = db.query(models.ParticipacaoAvulso).filter_by(
                jogo_id=jogo.id, jogador_id=jogador.id
            ).first()
            if p:
                db.delete(p)
            # Remove do ausentes se estiver (limpeza)
            ausentes = [int(x) for x in (jogo.mensalistas_ausentes or "").split(",") if x.strip()]
            if jogador.id in ausentes:
                ausentes.remove(jogador.id)
                jogo.mensalistas_ausentes = ','.join(str(i) for i in ausentes) if ausentes else None

    db.flush()

    # Recalcula pendências de todos os jogos confirmados/realizados com valor
    todos_confirmados = db.query(models.Jogo).filter(
        models.Jogo.status.in_(["Confirmado", "Realizado"])
    ).all()
    for jogo in todos_confirmados:
        _gerar_pendencias_jogo(db, jogo)

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

app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/cadastro")
def pagina_cadastro():
    return FileResponse("static/cadastro.html")

@app.get("/definir-senha")
def pagina_definir_senha():
    return FileResponse("static/definir-senha.html")


# ── Login / Logout / Me ───────────────────────────────────────────────────────

class LoginData(BaseModel):
    usuario: str
    senha: str

@app.post("/api/login")
def login(data: LoginData, response: Response, db: Session = Depends(get_db)):
    u = db.query(models.Usuario).filter(models.Usuario.usuario == data.usuario).first()
    if not u or not bcrypt.checkpw(data.senha.encode(), u.senha_hash.encode()):
        raise HTTPException(401, "Usuário ou senha incorretos")
    token = _criar_token(data.usuario)
    response.set_cookie(
        key="volei_sessao",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        secure=not is_sqlite,
    )
    return {"ok": True, "nome": u.nome}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("volei_sessao", samesite="lax")
    return {"ok": True}

@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("volei_sessao", "")
    if not _verificar_token(token):
        raise HTTPException(401, "Não autenticado")
    # Extrai login do token (formato: "usuario:ordinal.sig")
    try:
        login = token.rsplit(".", 1)[0].rsplit(":", 1)[0]
    except Exception:
        login = ""
    u = db.query(models.Usuario).filter(models.Usuario.usuario == login).first()
    tipo = (u.tipo or "admin") if u else "admin"
    perms = db.query(models.Permissao).filter(models.Permissao.tipo_usuario == tipo).all()
    jogador_id = u.jogador_id if u else None
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    return {
        "ok": True,
        "tipo": tipo,
        "jogador_id": jogador_id,
        "permissoes": {p.menu_slug: p.permitido for p in perms},
        "public_url": public_url or None,
    }


# ── Gerenciamento de usuários ─────────────────────────────────────────────────

class UsuarioCreate(BaseModel):
    nome: str
    usuario: str
    senha: str
    tipo: str = "admin"

class SenhaUpdate(BaseModel):
    senha_nova: str

class TipoUpdate(BaseModel):
    tipo: str

class PermissaoUpdate(BaseModel):
    permitido: bool

@app.get("/api/usuarios")
def listar_usuarios(db: Session = Depends(get_db)):
    result = []
    for u in db.query(models.Usuario).order_by(models.Usuario.nome).all():
        jogador_nome = None
        if u.jogador_id:
            j = db.query(models.Jogador).filter(models.Jogador.id == u.jogador_id).first()
            jogador_nome = j.nome if j else None
        result.append({
            "id": u.id, "nome": u.nome, "usuario": u.usuario,
            "tipo": u.tipo or "admin",
            "jogador_id": u.jogador_id,
            "jogador_nome": jogador_nome,
            "criado_em": u.criado_em.isoformat() if u.criado_em else None,
        })
    return result

class VincularJogadorData(BaseModel):
    jogador_id: Optional[int] = None

@app.put("/api/usuarios/{usuario_id}/jogador")
def vincular_jogador(usuario_id: int, data: VincularJogadorData, request: Request, db: Session = Depends(get_db)):
    """Vincula (ou desvincula) um usuário a um jogador."""
    _exigir_admin(request, db)
    u = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    if data.jogador_id:
        j = db.query(models.Jogador).filter(models.Jogador.id == data.jogador_id).first()
        if not j:
            raise HTTPException(404, "Jogador não encontrado")
        u.jogador_id = data.jogador_id
        # Garante que o tipo do jogador está correto
        tipo_jogador = "mensalista" if (u.tipo or "admin") == "admin" else (u.tipo or "mensalista")
        if j.tipo != tipo_jogador:
            j.tipo = tipo_jogador
    else:
        u.jogador_id = None
    db.commit()
    return {"ok": True}

@app.post("/api/usuarios", status_code=201)
def criar_usuario(data: UsuarioCreate, db: Session = Depends(get_db)):
    if db.query(models.Usuario).filter(models.Usuario.usuario == data.usuario).first():
        raise HTTPException(400, f"Usuário '{data.usuario}' já existe")
    if len(data.senha) < 4:
        raise HTTPException(400, "A senha deve ter pelo menos 4 caracteres")
    if data.tipo not in TIPOS_USUARIO:
        raise HTTPException(400, f"Tipo inválido. Use: {', '.join(TIPOS_USUARIO)}")
    senha_hash = bcrypt.hashpw(data.senha.encode(), bcrypt.gensalt()).decode()
    u = models.Usuario(nome=data.nome, usuario=data.usuario, senha_hash=senha_hash, tipo=data.tipo)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id, "nome": u.nome, "usuario": u.usuario, "tipo": u.tipo}

@app.put("/api/usuarios/{usuario_id}/senha")
def alterar_senha(usuario_id: int, data: SenhaUpdate, db: Session = Depends(get_db)):
    u = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    if len(data.senha_nova) < 4:
        raise HTTPException(400, "A senha deve ter pelo menos 4 caracteres")
    u.senha_hash = bcrypt.hashpw(data.senha_nova.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}

@app.delete("/api/usuarios/{usuario_id}")
def deletar_usuario(usuario_id: int, db: Session = Depends(get_db)):
    u = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    if db.query(models.Usuario).count() <= 1:
        raise HTTPException(400, "Não é possível remover o único usuário do sistema")
    db.delete(u)
    db.commit()
    return {"ok": True}

@app.put("/api/usuarios/{usuario_id}/tipo")
def alterar_tipo(usuario_id: int, data: TipoUpdate, db: Session = Depends(get_db)):
    if data.tipo not in TIPOS_USUARIO:
        raise HTTPException(400, f"Tipo inválido. Use: {', '.join(TIPOS_USUARIO)}")
    u = db.query(models.Usuario).filter(models.Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    tipo_anterior = u.tipo
    u.tipo = data.tipo
    # Admins são tratados como mensalistas no contexto de jogador
    tipo_jogador = "mensalista" if data.tipo == "admin" else data.tipo
    tipo_jogador_anterior = "mensalista" if (tipo_anterior or "admin") == "admin" else tipo_anterior
    # Sincroniza o Jogador vinculado e ajusta participações em jogos
    if u.jogador_id:
        j = db.query(models.Jogador).filter(models.Jogador.id == u.jogador_id).first()
        if j and tipo_jogador_anterior != tipo_jogador:
            j.tipo = tipo_jogador
            _migrar_participacoes_tipo(db, j, tipo_jogador)
        elif j:
            j.tipo = tipo_jogador  # corrige caso esteja "admin"
    db.commit()
    return {"ok": True}


# ── Permissões ────────────────────────────────────────────────────────────────

@app.get("/api/permissoes")
def listar_permissoes(db: Session = Depends(get_db)):
    perms = db.query(models.Permissao).all()
    result: dict = {t: {} for t in TIPOS_USUARIO}
    for p in perms:
        if p.tipo_usuario in result:
            result[p.tipo_usuario][p.menu_slug] = p.permitido
    return result

@app.put("/api/permissoes/{tipo_usuario}/{menu_slug}")
def atualizar_permissao(tipo_usuario: str, menu_slug: str, data: PermissaoUpdate,
                        db: Session = Depends(get_db)):
    if tipo_usuario not in TIPOS_USUARIO:
        raise HTTPException(400, "Tipo de usuário inválido")
    if tipo_usuario == "admin" and menu_slug == "config" and not data.permitido:
        raise HTTPException(400, "Não é possível remover o acesso a Configurações do Admin")
    p = db.query(models.Permissao).filter_by(
        tipo_usuario=tipo_usuario, menu_slug=menu_slug
    ).first()
    if not p:
        raise HTTPException(404, "Permissão não encontrada")
    p.permitido = data.permitido
    db.commit()
    return {"ok": True, "permitido": p.permitido}


# ── Configurações de valores ───────────────────────────────────────────────────

class ValoresUpdate(BaseModel):
    valor_mensalidade: float
    valor_avulso: float

@app.get("/api/config/valores")
def get_valores(db: Session = Depends(get_db)):
    return _get_config(db)

@app.put("/api/config/valores")
def put_valores(data: ValoresUpdate, db: Session = Depends(get_db)):
    for chave, valor in [("valor_mensalidade", data.valor_mensalidade),
                         ("valor_avulso", data.valor_avulso)]:
        row = db.query(models.Configuracao).filter_by(chave=chave).first()
        if row:
            row.valor = str(valor)
        else:
            db.add(models.Configuracao(chave=chave, valor=str(valor)))
    db.commit()
    return _get_config(db)


# ── Schemas ───────────────────────────────────────────────────────────────────

class JogadorCreate(BaseModel):
    nome: str
    tipo: str
    email: Optional[str] = None
    telefone: Optional[str] = None
    posicao: Optional[str] = None
    numero_camisa: Optional[int] = None
    data_nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None

class JogadorUpdate(BaseModel):
    nome: Optional[str] = None
    # tipo não pode ser alterado via PUT — usar PATCH /api/jogadores/{id}/tipo (admin only)
    email: Optional[str] = None
    telefone: Optional[str] = None
    posicao: Optional[str] = None
    numero_camisa: Optional[int] = None
    data_nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None
    ativo: Optional[bool] = None

class CadastroPublico(BaseModel):
    nome: str
    email: str
    telefone: str
    data_nascimento: date
    posicao: str
    rg: str
    tipo: str   # mensalista | avulso
    cpf: Optional[str] = None
    numero_camisa: Optional[int] = None

class DefinirSenhaConvite(BaseModel):
    token: str
    senha: str

class TipoUpdate(BaseModel):
    tipo: str

class JogoCreate(BaseModel):
    data: date
    categoria: Optional[str] = None
    observacao: Optional[str] = None
    valor: Optional[float] = None
    status: Optional[str] = "Planejado"
    endereco: Optional[str] = None

class JogoUpdate(BaseModel):
    data: Optional[date] = None
    categoria: Optional[str] = None
    observacao: Optional[str] = None
    endereco: Optional[str] = None
    valor: Optional[float] = None
    status: Optional[str] = None

class ParticipacaoCreate(BaseModel):
    jogador_id: int

class PagamentoCreate(BaseModel):
    jogador_id: int
    valor: float
    data_pagamento: date
    referencia: Optional[str] = None
    tipo: str
    observacao: Optional[str] = None

class PresencasUpdate(BaseModel):
    ausentes_ids: List[int]

class SaidaCreate(BaseModel):
    descricao: str
    valor: float
    data: date
    categoria: Optional[str] = None
    observacao: Optional[str] = None


class EntradaCreate(BaseModel):
    descricao: str
    valor: float
    data: date
    categoria: Optional[str] = None
    observacao: Optional[str] = None


# ── Jogadores ─────────────────────────────────────────────────────────────────

@app.get("/api/jogadores")
def listar_jogadores(tipo: Optional[str] = None, db: Session = Depends(get_db)):
    from sqlalchemy import or_ as _or
    q = db.query(models.Jogador)
    if tipo == "mensalista":
        # Admins são tratados como mensalistas: inclui jogadores vinculados a admins
        admin_ids = {row[0] for row in db.query(models.Usuario.jogador_id).filter(
            models.Usuario.tipo == "admin", models.Usuario.jogador_id.isnot(None)
        ).all()}
        q = q.filter(_or(models.Jogador.tipo == "mensalista", models.Jogador.id.in_(admin_ids)))
    elif tipo:
        q = q.filter(models.Jogador.tipo == tipo)
    jogadores = q.order_by(models.Jogador.nome).all()
    return [
        {
            "id": j.id, "nome": j.nome, "tipo": j.tipo,
            "email": j.email,
            "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao,
            "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": _decrypt(j.cpf),
            "rg": _decrypt(j.rg),
            "criado_em": j.criado_em.isoformat() if j.criado_em else None,
        }
        for j in jogadores
    ]

def _checar_camisa(db, numero: int, excluir_id: int = None):
    """Lança 400 se a camisa já está em uso por outro jogador ativo."""
    if numero is None:
        return
    q = db.query(models.Jogador).filter(
        models.Jogador.numero_camisa == numero,
        models.Jogador.ativo == True,
    )
    if excluir_id:
        q = q.filter(models.Jogador.id != excluir_id)
    dono = q.first()
    if dono:
        raise HTTPException(400, f"Camisa #{numero} já está em uso por {dono.nome}")

@app.post("/api/jogadores", status_code=201)
def criar_jogador(data: JogadorCreate, db: Session = Depends(get_db)):
    if data.tipo not in ("mensalista", "avulso"):
        raise HTTPException(400, "tipo deve ser 'mensalista' ou 'avulso'")
    _checar_camisa(db, data.numero_camisa)
    _checar_email_telefone(db, data.email, data.telefone)
    dados = data.model_dump()
    dados["cpf"] = _encrypt(dados.get("cpf"))
    dados["rg"]  = _encrypt(dados.get("rg"))
    j = models.Jogador(**dados)
    db.add(j)
    db.commit()
    db.refresh(j)
    return {"id": j.id, "nome": j.nome, "tipo": j.tipo, "email": j.email,
            "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao, "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": _decrypt(j.cpf), "rg": _decrypt(j.rg)}

@app.put("/api/jogadores/{jogador_id}")
def atualizar_jogador(jogador_id: int, data: JogadorUpdate, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    _checar_camisa(db, data.numero_camisa, excluir_id=jogador_id)
    _checar_email_telefone(db, data.email, data.telefone, excluir_id=jogador_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        if field in ("cpf", "rg"):
            value = _encrypt(value)
        setattr(j, field, value)
    db.commit()
    return {"id": j.id, "nome": j.nome, "tipo": j.tipo, "email": j.email,
            "telefone": j.telefone, "ativo": j.ativo,
            "posicao": j.posicao, "numero_camisa": j.numero_camisa,
            "data_nascimento": j.data_nascimento.isoformat() if j.data_nascimento else None,
            "cpf": _decrypt(j.cpf), "rg": _decrypt(j.rg)}

@app.patch("/api/jogadores/{jogador_id}/tipo")
def alterar_tipo_jogador(jogador_id: int, data: TipoUpdate,
                         request: Request, db: Session = Depends(get_db)):
    u = _usuario_da_sessao(request, db)
    if not u or u.tipo != "admin":
        raise HTTPException(403, "Apenas administradores podem alterar a modalidade")
    if data.tipo not in ("mensalista", "avulso"):
        raise HTTPException(400, "Modalidade inválida")
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    tipo_anterior = j.tipo
    j.tipo = data.tipo
    # Atualiza também o Usuario vinculado, se houver
    usuario_vinc = db.query(models.Usuario).filter_by(jogador_id=jogador_id).first()
    if usuario_vinc:
        usuario_vinc.tipo = data.tipo
    # Migra participações em jogos futuros e recalcula pendências
    if tipo_anterior != data.tipo:
        _migrar_participacoes_tipo(db, j, data.tipo)
    db.commit()
    return {"ok": True, "tipo": j.tipo}

@app.delete("/api/jogadores/{jogador_id}")
def desativar_jogador(jogador_id: int, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    j.ativo = False
    db.commit()
    return {"ok": True}

@app.patch("/api/jogadores/{jogador_id}/reativar")
def reativar_jogador(jogador_id: int, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    j.ativo = True
    db.commit()
    return {"ok": True}

@app.delete("/api/jogadores/{jogador_id}/permanente")
def deletar_jogador_permanente(jogador_id: int, db: Session = Depends(get_db)):
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")
    db.delete(j)   # cascade apaga pagamentos e participações
    db.commit()
    return {"ok": True}

# ── Convite / Auto-cadastro ───────────────────────────────────────────────────

@app.get("/api/convite/link")
def get_convite_link(request: Request):
    public_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    return {"link": f"{public_url}/cadastro"}


@app.get("/api/admin/test-email")
def test_email(para: str = ""):
    """Testa envio de e-mail e retorna resultado detalhado."""
    host     = os.getenv("SMTP_HOST", "")
    port     = int(os.getenv("SMTP_PORT", "465"))
    user     = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    destino  = para or user
    resend = os.getenv("RESEND_API_KEY", "")
    brevo  = os.getenv("BREVO_API_KEY", "")
    config = {"metodo": "Resend" if resend else ("Brevo" if brevo else "SMTP"),
               "RESEND_API_KEY": "***" if resend else "(não definido)",
               "BREVO_API_KEY":  "***" if brevo  else "(não definido)",
               "SMTP_HOST": host or "(não definido)", "SMTP_PORT": port,
               "SMTP_USER": user or "(não definido)",
               "destino": destino}
    if not resend and not brevo and not host:
        return {"ok": False, "erro": "Nenhum método de e-mail configurado", "config": config}
    try:
        _enviar_email(destino, "Teste Voleizou", "Este é um e-mail de teste do Voleizou.")
        return {"ok": True, "mensagem": f"E-mail enviado para {destino}", "config": config}
    except Exception as exc:
        return {"ok": False, "erro": str(exc), "config": config}

@app.post("/api/cadastro")
def cadastro_publico(data: CadastroPublico, db: Session = Depends(get_db)):
    """Cria uma solicitação de cadastro pendente — admin precisa aprovar."""
    if data.tipo not in ("mensalista", "avulso"):
        raise HTTPException(400, "Modalidade inválida")
    if not data.email or "@" not in data.email:
        raise HTTPException(400, "E-mail inválido")
    # Verifica duplicidade de e-mail/telefone em jogadores já ativos
    _checar_email_telefone(db, data.email, data.telefone)
    # Verifica duplicidade em solicitações pendentes
    existente = db.query(models.SolicitacaoCadastro).filter(
        models.SolicitacaoCadastro.email == data.email,
        models.SolicitacaoCadastro.status == "pendente"
    ).first()
    if existente:
        raise HTTPException(400, "Já existe uma solicitação pendente com este e-mail")

    sol = models.SolicitacaoCadastro(
        nome=data.nome, email=data.email, telefone=data.telefone,
        data_nascimento=data.data_nascimento, posicao=data.posicao,
        rg=_encrypt(data.rg), cpf=_encrypt(data.cpf) if data.cpf else None,
        tipo=data.tipo, numero_camisa=data.numero_camisa, status="pendente",
    )
    db.add(sol)
    db.commit()
    return {"ok": True, "mensagem": "Solicitação enviada! Aguarde aprovação do administrador."}


@app.get("/api/cadastros-pendentes")
def listar_cadastros_pendentes(db: Session = Depends(get_db)):
    sols = db.query(models.SolicitacaoCadastro).filter(
        models.SolicitacaoCadastro.status == "pendente"
    ).order_by(models.SolicitacaoCadastro.criado_em).all()
    return [{"id": s.id, "nome": s.nome, "email": s.email, "telefone": s.telefone,
             "tipo": s.tipo, "posicao": s.posicao,
             "data_nascimento": str(s.data_nascimento) if s.data_nascimento else None,
             "criado_em": str(s.criado_em)} for s in sols]


@app.post("/api/cadastros-pendentes/{sol_id}/aprovar")
def aprovar_cadastro(sol_id: int, request: Request, db: Session = Depends(get_db)):
    sol = db.query(models.SolicitacaoCadastro).filter(
        models.SolicitacaoCadastro.id == sol_id).first()
    if not sol:
        raise HTTPException(404, "Solicitação não encontrada")
    if sol.status != "pendente":
        raise HTTPException(400, "Solicitação já processada")

    # Verifica duplicidade antes de criar
    _checar_email_telefone(db, sol.email, sol.telefone)

    j = models.Jogador(
        nome=sol.nome, tipo=sol.tipo, email=sol.email,
        telefone=sol.telefone, data_nascimento=sol.data_nascimento,
        posicao=sol.posicao, rg=sol.rg, cpf=sol.cpf,
        numero_camisa=sol.numero_camisa, ativo=True,
    )
    db.add(j)
    db.flush()

    username = _gerar_usuario(sol.email, db, sol.nome)
    senha_temp = bcrypt.hashpw(os.urandom(32).hex().encode(), bcrypt.gensalt()).decode()
    u = models.Usuario(nome=sol.nome, usuario=username,
                       senha_hash=senha_temp, tipo=sol.tipo, jogador_id=j.id)
    db.add(u)
    db.flush()

    token = _gerar_token_setup(u.id)
    sol.status = "aprovado"
    db.commit()

    public_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    setup_url  = f"{public_url}/definir-senha?token={token}"
    email_ok = False
    try:
        _enviar_email(
            to=sol.email,
            subject="Bem-vindo ao Voleizou! Defina sua senha de acesso",
            body=(f"Olá {sol.nome}!\n\n"
                  f"Seu cadastro no Voleizou foi aprovado!\n"
                  f"Seu usuário de acesso é: {username}\n\n"
                  f"Clique no link abaixo para criar sua senha:\n{setup_url}\n\n"
                  f"O link é válido por 7 dias.\n\nAbraços,\nEquipe Voleizou"),
        )
        email_ok = True
    except Exception:
        pass

    resp = {"ok": True, "usuario": username, "email_enviado": email_ok}
    if not email_ok:
        resp["setup_url"] = setup_url
    return resp


@app.post("/api/cadastros-pendentes/{sol_id}/rejeitar")
def rejeitar_cadastro(sol_id: int, db: Session = Depends(get_db)):
    sol = db.query(models.SolicitacaoCadastro).filter(
        models.SolicitacaoCadastro.id == sol_id).first()
    if not sol:
        raise HTTPException(404, "Solicitação não encontrada")
    sol.status = "rejeitado"
    db.commit()
    return {"ok": True}

@app.get("/api/jogadores/{jogador_id}/link-setup")
def get_link_setup(jogador_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin: gera (ou regenera) o link de definição de senha para um jogador."""
    u = db.query(models.Usuario).filter(models.Usuario.jogador_id == jogador_id).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado para este jogador")
    token = _gerar_token_setup(u.id)
    public_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    return {"link": f"{public_url}/definir-senha?token={token}", "usuario": u.usuario}

@app.get("/api/verificar-convite")
def verificar_convite(token: str, db: Session = Depends(get_db)):
    uid = _verificar_token_setup(token)
    if not uid:
        raise HTTPException(400, "Link inválido ou expirado")
    u = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    return {"ok": True, "nome": u.nome, "usuario": u.usuario}

@app.post("/api/definir-senha-convite")
def definir_senha_convite(data: DefinirSenhaConvite, db: Session = Depends(get_db)):
    uid = _verificar_token_setup(data.token)
    if not uid:
        raise HTTPException(400, "Link inválido ou expirado")
    u = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not u:
        raise HTTPException(404, "Usuário não encontrado")
    if len(data.senha) < 6:
        raise HTTPException(400, "Senha deve ter pelo menos 6 caracteres")
    u.senha_hash = bcrypt.hashpw(data.senha.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}


# ── Recuperação de senha ───────────────────────────────────────────────────────

@app.get("/recuperar-senha")
def pagina_recuperar_senha():
    return FileResponse("static/recuperar-senha.html")

@app.get("/redefinir-senha")
def pagina_redefinir_senha():
    return FileResponse("static/redefinir-senha.html")

class RecuperarSenhaRequest(BaseModel):
    email: str

@app.post("/api/recuperar-senha")
def recuperar_senha(data: RecuperarSenhaRequest, request: Request, db: Session = Depends(get_db)):
    """Envia e-mail com link de redefinição de senha (válido 24h)."""
    jogador = db.query(models.Jogador).filter(
        models.Jogador.email == data.email.strip().lower(),
        models.Jogador.ativo == True
    ).first()
    # Responde OK mesmo sem encontrar para não revelar quais e-mails existem
    if not jogador:
        return {"ok": True}
    u = db.query(models.Usuario).filter_by(jogador_id=jogador.id).first()
    if not u:
        return {"ok": True}
    token = _gerar_token_reset(u.id)
    public_url = os.getenv("PUBLIC_URL", str(request.base_url).rstrip("/"))
    reset_url  = f"{public_url}/redefinir-senha?token={token}"
    try:
        _enviar_email(
            to=jogador.email,
            subject="Voleizou — Redefinição de senha",
            body=(f"Olá {jogador.nome}!\n\n"
                  f"Recebemos uma solicitação para redefinir a senha da conta '{u.usuario}'.\n\n"
                  f"Clique no link abaixo para criar uma nova senha:\n{reset_url}\n\n"
                  f"O link é válido por 24 horas.\n"
                  f"Se você não solicitou isso, ignore este e-mail.\n\n"
                  f"Abraços,\nEquipe Voleizou"),
        )
    except Exception as exc:
        print(f"[RESET] Falha ao enviar e-mail: {exc}")
    return {"ok": True}

@app.get("/api/verificar-reset")
def verificar_reset(token: str, db: Session = Depends(get_db)):
    uid = _verificar_token_reset(token)
    if not uid:
        raise HTTPException(400, "Link inválido ou expirado")
    u = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not u:
        raise HTTPException(400, "Usuário não encontrado")
    return {"ok": True, "nome": u.nome, "usuario": u.usuario}

class RedefinirSenhaRequest(BaseModel):
    token: str
    senha: str

@app.post("/api/redefinir-senha")
def redefinir_senha(data: RedefinirSenhaRequest, db: Session = Depends(get_db)):
    uid = _verificar_token_reset(data.token)
    if not uid:
        raise HTTPException(400, "Link inválido ou expirado")
    u = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not u:
        raise HTTPException(400, "Usuário não encontrado")
    if len(data.senha) < 6:
        raise HTTPException(400, "A senha deve ter pelo menos 6 caracteres")
    u.senha_hash = bcrypt.hashpw(data.senha.encode(), bcrypt.gensalt()).decode()
    db.commit()
    return {"ok": True}


# ── Jogos ─────────────────────────────────────────────────────────────────────

def _jogo_dict(jogo) -> dict:
    avulsos = [{"id": p.jogador.id, "nome": p.jogador.nome, "participacao_id": p.id}
               for p in jogo.participacoes]
    ausentes = [int(x) for x in (jogo.mensalistas_ausentes or '').split(',') if x.strip()]
    return {
        "id": jogo.id, "data": jogo.data.isoformat(),
        "categoria": jogo.categoria, "observacao": jogo.observacao,
        "valor": jogo.valor, "status": jogo.status or "Planejado",
        "status_efetivo": _status_efetivo(jogo.data, jogo.status),
        "avulsos": avulsos, "total_avulsos": len(avulsos),
        "mensalistas_ausentes": ausentes,
        "endereco": jogo.endereco,
    }

@app.get("/api/jogos")
def listar_jogos(db: Session = Depends(get_db)):
    jogos = db.query(models.Jogo).order_by(models.Jogo.data.asc()).all()
    return [_jogo_dict(j) for j in jogos]

@app.post("/api/jogos", status_code=201)
def criar_jogo(data: JogoCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    jogo = models.Jogo(**data.model_dump())
    db.add(jogo)
    db.flush()
    if jogo.status in ("Confirmado", "Realizado"):
        _gerar_pendencias_jogo(db, jogo)
    db.commit()
    db.refresh(jogo)
    return _jogo_dict(jogo)

@app.get("/api/jogos/{jogo_id}")
def obter_jogo(jogo_id: int, db: Session = Depends(get_db)):
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    return _jogo_dict(jogo)

@app.put("/api/jogos/{jogo_id}")
def atualizar_jogo(jogo_id: int, data: JogoUpdate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    status_anterior = jogo.status or "Planejado"
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(jogo, field, value)
    # Gera pendências ao confirmar ou realizar (apenas não-semanal com valor)
    if jogo.status in ("Confirmado", "Realizado") and status_anterior not in ("Confirmado", "Realizado"):
        _gerar_pendencias_jogo(db, jogo)
    elif jogo.status in ("Confirmado", "Realizado"):
        # Atualiza valores se valor ou categoria mudou
        _gerar_pendencias_jogo(db, jogo)
    db.commit()
    return _jogo_dict(jogo)

@app.put("/api/jogos/{jogo_id}/presencas")
def atualizar_presencas(jogo_id: int, data: PresencasUpdate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    jogo.mensalistas_ausentes = ','.join(str(i) for i in data.ausentes_ids) if data.ausentes_ids else None
    db.flush()
    # Recalcula pendências se o jogo já estiver confirmado (divisão do valor muda com presenças)
    if jogo.status in ("Confirmado", "Realizado"):
        _gerar_pendencias_jogo(db, jogo)
    db.commit()
    return {"ok": True}

@app.delete("/api/jogos/{jogo_id}")
def deletar_jogo(jogo_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    jogo = db.query(models.Jogo).filter(models.Jogo.id == jogo_id).first()
    if not jogo:
        raise HTTPException(404, "Jogo não encontrado")
    db.delete(jogo)
    db.commit()
    return {"ok": True}

@app.post("/api/jogos/{jogo_id}/participacoes", status_code=201)
def adicionar_avulso(jogo_id: int, data: ParticipacaoCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
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
    db.flush()
    if jogo.status in ("Confirmado", "Realizado"):
        _gerar_pendencias_jogo(db, jogo)
    db.commit()
    return {"ok": True}

@app.delete("/api/jogos/{jogo_id}/participacoes/{jogador_id}")
def remover_avulso(jogo_id: int, jogador_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    p = db.query(models.ParticipacaoAvulso).filter(
        models.ParticipacaoAvulso.jogo_id == jogo_id,
        models.ParticipacaoAvulso.jogador_id == jogador_id
    ).first()
    if not p:
        raise HTTPException(404, "Participação não encontrada")
    db.delete(p)
    db.flush()
    if jogo.status in ("Confirmado", "Realizado"):
        _gerar_pendencias_jogo(db, jogo)
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
def criar_pagamento(data: PagamentoCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
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
def deletar_pagamento(pagamento_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
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
def criar_saida(data: SaidaCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    s = models.Saida(**data.model_dump())
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "descricao": s.descricao, "valor": s.valor, "data": s.data.isoformat()}

@app.delete("/api/saidas/{saida_id}")
def deletar_saida(saida_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    s = db.query(models.Saida).filter(models.Saida.id == saida_id).first()
    if not s:
        raise HTTPException(404, "Saída não encontrada")
    db.delete(s)
    db.commit()
    return {"ok": True}


# ── Entradas gerais ───────────────────────────────────────────────────────────

@app.get("/api/entradas")
def listar_entradas(mes: Optional[int] = None, ano: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(models.Entrada)
    if mes:
        q = q.filter(extract("month", models.Entrada.data) == mes)
    if ano:
        q = q.filter(extract("year", models.Entrada.data) == ano)
    entradas = q.order_by(models.Entrada.data.desc()).all()
    return [
        {
            "id": e.id, "descricao": e.descricao, "valor": e.valor,
            "data": e.data.isoformat(), "categoria": e.categoria, "observacao": e.observacao,
        }
        for e in entradas
    ]

@app.post("/api/entradas", status_code=201)
def criar_entrada(data: EntradaCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    e = models.Entrada(**data.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id, "descricao": e.descricao, "valor": e.valor, "data": e.data.isoformat()}

@app.put("/api/entradas/{entrada_id}")
def editar_entrada(entrada_id: int, data: EntradaCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    e = db.query(models.Entrada).filter(models.Entrada.id == entrada_id).first()
    if not e:
        raise HTTPException(404, "Entrada não encontrada")
    for k, v in data.model_dump().items():
        setattr(e, k, v)
    db.commit()
    return {"ok": True}

@app.delete("/api/entradas/{entrada_id}")
def deletar_entrada(entrada_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    e = db.query(models.Entrada).filter(models.Entrada.id == entrada_id).first()
    if not e:
        raise HTTPException(404, "Entrada não encontrada")
    db.delete(e)
    db.commit()
    return {"ok": True}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def dashboard(mes: Optional[int] = None, ano: Optional[int] = None, db: Session = Depends(get_db)):
    hoje = date.today()
    mes = mes or hoje.month
    ano = ano or hoje.year
    cfg = _get_config(db)
    VALOR_MENSALIDADE = cfg["valor_mensalidade"]
    VALOR_AVULSO      = cfg["valor_avulso"]

    MESES_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

    # Mensalistas (admins são tratados como mensalistas)
    from sqlalchemy import or_ as _or4
    _dash_admin_ids = {row[0] for row in db.query(models.Usuario.jogador_id).filter(
        models.Usuario.tipo == "admin", models.Usuario.jogador_id.isnot(None)
    ).all()}
    mensalistas = db.query(models.Jogador).filter(
        _or4(models.Jogador.tipo == "mensalista", models.Jogador.id.in_(_dash_admin_ids)),
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

        if valor_pago >= VALOR_MENSALIDADE:
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
            "valor_devido": VALOR_MENSALIDADE,
            "valor_falta": max(0, VALOR_MENSALIDADE - valor_pago),
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
        valor_devido = jogos_count * VALOR_AVULSO
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
    entradas_gerais_mes = db.query(models.Entrada).filter(
        extract("month", models.Entrada.data) == mes,
        extract("year", models.Entrada.data) == ano,
    ).all()
    total_entradas_gerais = sum(e.valor for e in entradas_gerais_mes)
    total_entradas = total_entradas_mensalidade + total_entradas_avulso + total_entradas_gerais

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

    # Presenças do mês — apenas jogos passados
    jogos_passados = [j for j in todos_jogos_mes if j.data < hoje]
    total_jogos_passados = len(jogos_passados)

    todos_ativos = db.query(models.Jogador).filter(
        models.Jogador.ativo == True
    ).order_by(models.Jogador.nome).all()

    # IDs de admins com jogador vinculado (tratados como mensalistas)
    _dash_admin_jog_ids = {row[0] for row in db.query(models.Usuario.jogador_id).filter(
        models.Usuario.tipo == "admin", models.Usuario.jogador_id.isnot(None)
    ).all()}

    presencas_mes = []
    for jogador in todos_ativos:
        eh_mensalista = jogador.tipo == 'mensalista' or jogador.id in _dash_admin_jog_ids
        count = 0
        for jogo in jogos_passados:
            ausentes_ids = [int(x) for x in (jogo.mensalistas_ausentes or '').split(',') if x.strip()]
            if eh_mensalista:
                if jogador.id not in ausentes_ids:
                    count += 1
            else:
                if any(p.jogador_id == jogador.id for p in jogo.participacoes):
                    count += 1
        # Mensalistas (e admins): inclui sempre; avulsos: só se tiver ao menos 1 presença
        if eh_mensalista or count > 0:
            presencas_mes.append({
                "id": jogador.id,
                "nome": jogador.nome,
                "tipo": jogador.tipo,
                "presencas": count,
                "total": total_jogos_passados,
            })

    return {
        "mes": mes,
        "ano": ano,
        "mes_nome": f"{MESES_PT[mes]} {ano}",
        "mensalistas_lista": mensalistas_lista,
        "avulsos_resumo": avulsos_resumo,
        "jogos_mes": [{"id": j.id, "data": j.data.isoformat(), "categoria": j.categoria, "observacao": j.observacao, "valor": j.valor} for j in todos_jogos_mes],
        "aniversariantes": aniversariantes,
        "presencas_mes": presencas_mes,
        "total_jogos_passados": total_jogos_passados,
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
    """Retorna info do jogador para a página de pagamento, incluindo pendências abertas."""
    j = db.query(models.Jogador).filter(
        models.Jogador.id == jogador_id,
        models.Jogador.ativo == True
    ).first()
    if not j:
        raise HTTPException(404, "Jogador não encontrado")

    hoje = date.today()
    mes, ano = hoje.month, hoje.year
    cfg = _get_config(db)
    VALOR_MENSALIDADE = cfg["valor_mensalidade"]
    VALOR_AVULSO = cfg["valor_avulso"]

    pendencias_abertas = []

    if j.tipo == "mensalista":
        # Verifica últimos 3 meses
        for delta in range(2, -1, -1):
            m = mes - delta
            y = ano
            if m <= 0: m += 12; y -= 1
            pags = db.query(models.Pagamento).filter(
                models.Pagamento.jogador_id == j.id,
                models.Pagamento.tipo == "mensalidade",
                extract("month", models.Pagamento.data_pagamento) == m,
                extract("year",  models.Pagamento.data_pagamento) == y,
            ).all()
            pago = sum(p.valor for p in pags if not (p.observacao or "").startswith("PENDENTE|"))
            falta = round(VALOR_MENSALIDADE - pago, 2)
            if falta > 0:
                MESES_PT = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                            "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
                pendencias_abertas.append({
                    "id": None, "tipo": "mensalidade",
                    "descricao": f"Mensalidade {MESES_PT[m]} {y}",
                    "valor": falta, "referencia": f"{y}-{m:02d}",
                })
    else:
        # Avulso: verifica jogos do mês sem pagamento
        pass  # avulso usa fluxo simples de valor total

    # Pendências de evento abertas
    eventos = db.query(models.Pendencia).filter(
        models.Pendencia.jogador_id == j.id,
        models.Pendencia.tipo == "evento",
        models.Pendencia.quitado == False,
    ).order_by(models.Pendencia.criado_em).all()
    for e in eventos:
        pendencias_abertas.append({
            "id": e.id, "tipo": "evento",
            "descricao": e.descricao, "valor": e.valor,
            "referencia": e.referencia,
        })

    total_devido = sum(p["valor"] for p in pendencias_abertas)
    if not pendencias_abertas and j.tipo != "mensalista":
        total_devido = VALOR_AVULSO

    return {
        "id": j.id, "nome": j.nome, "tipo": j.tipo,
        "mes": mes, "ano": ano,
        "valor_devido": total_devido or (VALOR_MENSALIDADE if j.tipo == "mensalista" else VALOR_AVULSO),
        "pendencias": pendencias_abertas,
    }

@app.post("/api/comprovante/enviar")
async def enviar_comprovante_jogador(
    file: UploadFile = File(...),
    jogador_id: int = Form(...),
    nome_comprovante: str = Form(default=""),
    valor: str = Form(default=""),
    data_iso: str = Form(default=""),
    pendencias_ids: str = Form(default=""),   # IDs separados por vírgula
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
        observacao=f"PENDENTE|comprovante_id:{arquivo.id}|nome:{nome_comprovante}|pendencias_ids:{pendencias_ids}",
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
        ids_str = partes.get("pendencias_ids", "")
        pend_ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
        pend_info = []
        if pend_ids:
            pends = db.query(models.Pendencia).filter(models.Pendencia.id.in_(pend_ids)).all()
            pend_info = [{"id": pe.id, "descricao": pe.descricao, "valor": pe.valor} for pe in pends]
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
            "pendencias": pend_info,
        })
    return result

def _pendencias_jogador(jogador_id: int, db: Session, cfg: dict) -> list:
    """Retorna as pendências de um jogador específico (para uso próprio)."""
    hoje = date.today()
    mes, ano = hoje.month, hoje.year
    MESES_PT = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    VALOR_MENSALIDADE = cfg["valor_mensalidade"]
    j = db.query(models.Jogador).filter(models.Jogador.id == jogador_id, models.Jogador.ativo == True).first()
    if not j:
        return []
    itens = []
    if j.tipo == "mensalista":
        for delta in range(2, -1, -1):
            m = mes - delta; y = ano
            if m <= 0: m += 12; y -= 1
            if j.criado_em and date(y, m, 1) < j.criado_em.date().replace(day=1):
                continue
            pags = db.query(models.Pagamento).filter(
                models.Pagamento.jogador_id == j.id,
                models.Pagamento.tipo == "mensalidade",
                extract("month", models.Pagamento.data_pagamento) == m,
                extract("year",  models.Pagamento.data_pagamento) == y,
            ).all()
            pago  = sum(p.valor for p in pags if not (p.observacao or "").startswith("PENDENTE|"))
            falta = round(VALOR_MENSALIDADE - pago, 2)
            if falta > 0:
                itens.append({"id": None, "tipo": "mensalidade",
                              "descricao": f"Mensalidade {MESES_PT[m]} {y}",
                              "valor": falta, "referencia": f"{y}-{m:02d}"})
    eventos = db.query(models.Pendencia).filter(
        models.Pendencia.jogador_id == jogador_id,
        models.Pendencia.tipo == "evento",
        models.Pendencia.quitado == False,
    ).all()
    for e in eventos:
        itens.append({"id": e.id, "tipo": "evento",
                      "descricao": e.descricao, "valor": e.valor, "referencia": e.referencia})
    if not itens:
        return []
    total = round(sum(i["valor"] for i in itens), 2)
    return [{"jogador": {"id": j.id, "nome": j.nome, "tipo": j.tipo,
                         "telefone": j.telefone, "email": j.email},
             "itens": itens, "total": total}]


@app.get("/api/pendencias")
def listar_pendencias(request: Request, db: Session = Depends(get_db)):
    """Admin: retorna todos. Jogador: retorna só as próprias pendências."""
    cfg = _get_config(db)
    u = _usuario_da_sessao(request, db)
    if u and u.tipo != "admin" and u.jogador_id:
        return _pendencias_jogador(u.jogador_id, db, cfg)
    VALOR_MENSALIDADE = cfg["valor_mensalidade"]
    hoje = date.today()
    mes, ano = hoje.month, hoje.year
    MESES_PT = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

    from sqlalchemy import or_ as _or3
    _admin_ids = {row[0] for row in db.query(models.Usuario.jogador_id).filter(
        models.Usuario.tipo == "admin", models.Usuario.jogador_id.isnot(None)
    ).all()}
    mensalistas = db.query(models.Jogador).filter(
        _or3(models.Jogador.tipo == "mensalista", models.Jogador.id.in_(_admin_ids)),
        models.Jogador.ativo == True
    ).order_by(models.Jogador.nome).all()

    todos_ativos = db.query(models.Jogador).filter(
        models.Jogador.ativo == True
    ).order_by(models.Jogador.nome).all()

    result = {}

    # Mensalidade: verifica mês atual e anterior (mínimo maio/2026)
    DATA_INICIO_COBRANCA = date(2026, 5, 1)
    for j in mensalistas:
        for delta in range(1, -1, -1):
            m = mes - delta; y = ano
            if m <= 0: m += 12; y -= 1
            # Não cobrar antes de maio/2026
            if date(y, m, 1) < DATA_INICIO_COBRANCA:
                continue
            # Não cobrar meses antes da criação do jogador
            if j.criado_em and date(y, m, 1) < j.criado_em.date().replace(day=1):
                continue
            pags = db.query(models.Pagamento).filter(
                models.Pagamento.jogador_id == j.id,
                models.Pagamento.tipo == "mensalidade",
                extract("month", models.Pagamento.data_pagamento) == m,
                extract("year",  models.Pagamento.data_pagamento) == y,
            ).all()
            pago  = sum(p.valor for p in pags if not (p.observacao or "").startswith("PENDENTE|"))
            falta = round(VALOR_MENSALIDADE - pago, 2)
            if falta > 0:
                if j.id not in result:
                    result[j.id] = {"jogador": {"id": j.id, "nome": j.nome, "tipo": j.tipo,
                                                 "telefone": j.telefone, "email": j.email}, "itens": []}
                result[j.id]["itens"].append({
                    "id": None, "tipo": "mensalidade",
                    "descricao": f"Mensalidade {MESES_PT[m]} {y}",
                    "valor": falta, "referencia": f"{y}-{m:02d}",
                })

    # Pendências de evento abertas
    eventos = db.query(models.Pendencia).filter(
        models.Pendencia.tipo == "evento",
        models.Pendencia.quitado == False,
    ).order_by(models.Pendencia.criado_em).all()
    for e in eventos:
        j = e.jogador
        if not j or not j.ativo:
            continue
        if j.id not in result:
            result[j.id] = {"jogador": {"id": j.id, "nome": j.nome, "tipo": j.tipo,
                                         "telefone": j.telefone, "email": j.email}, "itens": []}
        result[j.id]["itens"].append({
            "id": e.id, "tipo": "evento",
            "descricao": e.descricao, "valor": e.valor, "referencia": e.referencia,
        })

    lista = sorted(result.values(), key=lambda x: x["jogador"]["nome"])
    for entry in lista:
        entry["total"] = round(sum(i["valor"] for i in entry["itens"]), 2)
    return lista

@app.delete("/api/pendencias/{pendencia_id}")
def excluir_pendencia(pendencia_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin: remove uma pendência de evento."""
    _exigir_admin(request, db)
    p = db.query(models.Pendencia).filter(models.Pendencia.id == pendencia_id).first()
    if not p:
        raise HTTPException(404, "Pendência não encontrada")
    db.delete(p)
    db.commit()
    return {"ok": True}

@app.post("/api/pendencias/regenerar")
def regenerar_pendencias(request: Request, db: Session = Depends(get_db)):
    """Admin: regenera pendências de eventos para todos os jogos confirmados/realizados."""
    _exigir_admin(request, db)
    _regenerar_pendencias_todos_eventos(db)
    return {"ok": True}

@app.post("/api/pendentes/{pagamento_id}/aprovar")
def aprovar_pendente(pagamento_id: int, db: Session = Depends(get_db)):
    """Aprova um pagamento pendente e quita as pendências vinculadas."""
    p = db.query(models.Pagamento).filter(models.Pagamento.id == pagamento_id).first()
    if not p:
        raise HTTPException(404, "Pagamento não encontrado")
    # Extrai pendencias_ids do observacao
    partes = {k: v for k, v in (x.split(':', 1) for x in (p.observacao or "").split('|') if ':' in x)}
    ids_str = partes.get("pendencias_ids", "")
    ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
    if ids:
        now = datetime.utcnow()
        db.query(models.Pendencia).filter(
            models.Pendencia.id.in_(ids),
            models.Pendencia.jogador_id == p.jogador_id,
        ).update({"quitado": True, "quitado_em": now}, synchronize_session=False)
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
    total_pagamentos = sum(p.valor for p in todos_pagamentos)

    # Entradas gerais (patrocínio, prêmios, doações, outros)
    todas_entradas_gerais = db.query(models.Entrada).all()
    total_entradas_gerais = sum(e.valor for e in todas_entradas_gerais)
    total_entradas = total_pagamentos + total_entradas_gerais

    # Todas as saídas
    todas_saidas = db.query(models.Saida).all()
    total_saidas = sum(s.valor for s in todas_saidas)

    # Resumo por mês (últimos 12 meses com movimento)
    from collections import defaultdict
    por_mes = defaultdict(lambda: {"entradas": 0.0, "saidas": 0.0})
    for p in todos_pagamentos:
        chave = f"{p.data_pagamento.year}-{p.data_pagamento.month:02d}"
        por_mes[chave]["entradas"] += p.valor
    for e in todas_entradas_gerais:
        chave = f"{e.data.year}-{e.data.month:02d}"
        por_mes[chave]["entradas"] += e.valor
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

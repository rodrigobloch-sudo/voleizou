from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, ForeignKey, UniqueConstraint, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Jogador(Base):
    __tablename__ = "jogadores"

    id              = Column(Integer, primary_key=True, index=True)
    nome            = Column(String, nullable=False)
    tipo            = Column(String, nullable=False)   # "mensalista" | "avulso"
    telefone        = Column(String, nullable=True)
    posicao         = Column(String, nullable=True)    # pode ser múltipla, ex: "Central,Ponteiro"
    numero_camisa   = Column(Integer, nullable=True)
    data_nascimento = Column(Date, nullable=True)
    cpf             = Column(String, nullable=True)
    rg              = Column(String, nullable=True)
    email           = Column(String, nullable=True, unique=True)
    ativo           = Column(Boolean, default=True)
    criado_em       = Column(DateTime, server_default=func.now())

    pagamentos            = relationship("Pagamento", back_populates="jogador", cascade="all, delete-orphan")
    participacoes         = relationship("ParticipacaoAvulso", back_populates="jogador", cascade="all, delete-orphan")
    pendencias_financeiras= relationship("Pendencia", back_populates="jogador", cascade="all, delete-orphan")


class Jogo(Base):
    __tablename__ = "jogos"

    id                   = Column(Integer, primary_key=True, index=True)
    data                 = Column(Date, nullable=False)
    categoria            = Column(String, nullable=True)
    observacao           = Column(String, nullable=True)
    valor                = Column(Float, nullable=True)   # valor total do evento (não usado em Jogo Semanal)
    mensalistas_ausentes = Column(String, nullable=True)  # IDs separados por vírgula
    status               = Column(String, nullable=True, default="Planejado")  # Planejado|Confirmado|Cancelado|Realizado
    criado_em            = Column(DateTime, server_default=func.now())

    participacoes = relationship("ParticipacaoAvulso", back_populates="jogo", cascade="all, delete-orphan")
    pendencias    = relationship("Pendencia", back_populates="jogo", cascade="all, delete-orphan")


class ParticipacaoAvulso(Base):
    __tablename__ = "participacoes_avulso"
    __table_args__ = (UniqueConstraint("jogo_id", "jogador_id"),)

    id         = Column(Integer, primary_key=True, index=True)
    jogo_id    = Column(Integer, ForeignKey("jogos.id"), nullable=False)
    jogador_id = Column(Integer, ForeignKey("jogadores.id"), nullable=False)
    criado_em  = Column(DateTime, server_default=func.now())

    jogo    = relationship("Jogo", back_populates="participacoes")
    jogador = relationship("Jogador", back_populates="participacoes")


class Pagamento(Base):
    __tablename__ = "pagamentos"

    id             = Column(Integer, primary_key=True, index=True)
    jogador_id     = Column(Integer, ForeignKey("jogadores.id"), nullable=False)
    valor          = Column(Float, nullable=False)
    data_pagamento = Column(Date, nullable=False)
    referencia     = Column(String, nullable=True)
    tipo           = Column(String, nullable=False)   # "mensalidade" | "avulso"
    observacao     = Column(String, nullable=True)
    criado_em      = Column(DateTime, server_default=func.now())

    jogador = relationship("Jogador", back_populates="pagamentos")


class Saida(Base):
    __tablename__ = "saidas"

    id         = Column(Integer, primary_key=True, index=True)
    descricao  = Column(String, nullable=False)
    valor      = Column(Float, nullable=False)
    data       = Column(Date, nullable=False)
    categoria  = Column(String, nullable=True)
    observacao = Column(String, nullable=True)
    criado_em  = Column(DateTime, server_default=func.now())


class ArquivoComprovante(Base):
    """Armazena o arquivo do comprovante diretamente no banco."""
    __tablename__ = "arquivos_comprovante"

    id            = Column(Integer, primary_key=True, index=True)
    nome_original = Column(String, nullable=False)
    conteudo      = Column(LargeBinary, nullable=False)
    mimetype      = Column(String, default="application/octet-stream")
    criado_em     = Column(DateTime, server_default=func.now())


class Usuario(Base):
    """Usuários administradores do sistema."""
    __tablename__ = "usuarios"

    id          = Column(Integer, primary_key=True, index=True)
    nome        = Column(String, nullable=False)
    usuario     = Column(String, nullable=False, unique=True)
    senha_hash  = Column(String, nullable=False)
    tipo        = Column(String, nullable=False, default="admin")  # admin | mensalista | avulso
    jogador_id  = Column(Integer, ForeignKey("jogadores.id"), nullable=True)
    criado_em   = Column(DateTime, server_default=func.now())


class Permissao(Base):
    """Permissões de acesso por tipo de usuário."""
    __tablename__ = "permissoes"
    __table_args__ = (UniqueConstraint("tipo_usuario", "menu_slug"),)

    id           = Column(Integer, primary_key=True, index=True)
    tipo_usuario = Column(String, nullable=False)   # admin | mensalista | avulso
    menu_slug    = Column(String, nullable=False)   # dashboard | jogadores | …
    permitido    = Column(Boolean, nullable=False, default=False)


class Pendencia(Base):
    """Pendências financeiras individuais por jogador (eventos confirmados + mensalidade)."""
    __tablename__ = "pendencias"
    __table_args__ = (UniqueConstraint("jogador_id", "jogo_id"),)  # evita duplicatas por jogo

    id          = Column(Integer, primary_key=True, index=True)
    jogador_id  = Column(Integer, ForeignKey("jogadores.id"), nullable=False)
    jogo_id     = Column(Integer, ForeignKey("jogos.id"), nullable=True)   # None = mensalidade
    tipo        = Column(String, nullable=False)   # "evento" | "mensalidade"
    descricao   = Column(String, nullable=False)
    valor       = Column(Float, nullable=False)
    referencia  = Column(String, nullable=True)    # "2026-05" (mensalidade) ou data do jogo
    quitado     = Column(Boolean, default=False, nullable=False)
    quitado_em  = Column(DateTime, nullable=True)
    criado_em   = Column(DateTime, server_default=func.now())

    jogador = relationship("Jogador", back_populates="pendencias_financeiras")
    jogo    = relationship("Jogo",    back_populates="pendencias")


class Configuracao(Base):
    """Configurações gerais do sistema (chave → valor)."""
    __tablename__ = "configuracoes"

    chave     = Column(String, primary_key=True)
    valor     = Column(String, nullable=False)
    criado_em = Column(DateTime, server_default=func.now())

from sqlalchemy import Column, Integer, String, Float, Boolean, Date, DateTime, ForeignKey, UniqueConstraint, LargeBinary, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Jogador(Base):
    __tablename__ = "jogadores"

    id              = Column(Integer, primary_key=True, index=True)
    nome            = Column(String, nullable=False)
    apelido         = Column(String, nullable=True)
    tipo            = Column(String, nullable=False)   # "mensalista" | "avulso"
    telefone        = Column(String, nullable=True)
    posicao         = Column(String, nullable=True)    # pode ser múltipla, ex: "Central,Ponteiro"
    posicao_principal = Column(String, nullable=True)  # uma das posições em `posicao`
    numero_camisa   = Column(Integer, nullable=True)
    data_nascimento = Column(Date, nullable=True)
    cpf             = Column(String, nullable=True)
    rg              = Column(String, nullable=True)
    email           = Column(String, nullable=True, unique=True)
    ativo           = Column(Boolean, default=True)
    foto            = Column(LargeBinary, nullable=True)
    foto_mimetype   = Column(String, nullable=True)
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
    endereco             = Column(String, nullable=True)
    local_nome           = Column(String, nullable=True)
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
    status     = Column(String, nullable=False, server_default="Confirmada")
    criado_em  = Column(DateTime, server_default=func.now())


class Entrada(Base):
    """Entradas financeiras gerais (patrocínio, prêmios, doações, etc.)."""
    __tablename__ = "entradas"

    id         = Column(Integer, primary_key=True, index=True)
    descricao  = Column(String, nullable=False)
    valor      = Column(Float, nullable=False)
    data       = Column(Date, nullable=False)
    categoria  = Column(String, nullable=True)   # Patrocínio | Prêmios | Doações | Outros
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
    cancelado_em = Column(DateTime, nullable=True)  # admin excluiu manualmente — não deve ser recriada em recálculos
    equipamento_reserva_id = Column(Integer, ForeignKey("reservas_equipamento.id"), nullable=True)
    criado_em   = Column(DateTime, server_default=func.now())

    jogador = relationship("Jogador", back_populates="pendencias_financeiras")
    jogo    = relationship("Jogo",    back_populates="pendencias")


class Equipamento(Base):
    """Lote de material do grupo (uniforme, etc.) disponível para encomenda."""
    __tablename__ = "equipamentos"

    id              = Column(Integer, primary_key=True, index=True)
    nome            = Column(String, nullable=False)
    ano             = Column(Integer, nullable=False)
    mes             = Column(Integer, nullable=False)   # 1-12
    valor_total     = Column(Float, nullable=False)
    entrada_minima  = Column(Float, nullable=False)
    data_limite_encomenda = Column(Date, nullable=False)
    permite_numero_nome   = Column(Boolean, default=False, nullable=False)
    tamanhos_disponiveis  = Column(String, nullable=False, default="PP,P,M,G,GG,XG")  # CSV, definido pelo admin conforme o fornecedor
    imagem_oficial            = Column(LargeBinary, nullable=True)
    imagem_oficial_mimetype   = Column(String, nullable=True)
    imagem_libero              = Column(LargeBinary, nullable=True)
    imagem_libero_mimetype     = Column(String, nullable=True)
    imagem_painel_medidas          = Column(LargeBinary, nullable=True)
    imagem_painel_medidas_mimetype = Column(String, nullable=True)
    ativo     = Column(Boolean, default=True, nullable=False)
    criado_em = Column(DateTime, server_default=func.now())

    reservas = relationship("ReservaEquipamento", back_populates="equipamento", cascade="all, delete-orphan")


class ReservaEquipamento(Base):
    """Encomenda de um jogador para um lote de Equipamento — sem unicidade por jogador,
    pois um mesmo jogador pode encomendar mais de um item no mesmo lote."""
    __tablename__ = "reservas_equipamento"

    id             = Column(Integer, primary_key=True, index=True)
    equipamento_id = Column(Integer, ForeignKey("equipamentos.id"), nullable=False)
    jogador_id     = Column(Integer, ForeignKey("jogadores.id"), nullable=False)  # requisitante
    nome_camiseta  = Column(String, nullable=True)
    numero_camiseta = Column(Integer, nullable=True)   # None = "sem número"
    tamanho        = Column(String, nullable=False)    # PP | P | M | G | GG | XG
    modelo         = Column(String, nullable=False)     # "oficial" | "libero"
    valor_total    = Column(Float, nullable=False)      # snapshot do equipamento no momento da encomenda
    entrada_minima = Column(Float, nullable=False)
    criado_em      = Column(DateTime, server_default=func.now())

    equipamento = relationship("Equipamento", back_populates="reservas")
    jogador     = relationship("Jogador")


class Categoria(Base):
    """Categorias configuráveis por tipo (jogo | saida | entrada)."""
    __tablename__ = "categorias"
    __table_args__ = (UniqueConstraint("tipo", "nome"),)

    id        = Column(Integer, primary_key=True, index=True)
    tipo      = Column(String, nullable=False)   # "jogo" | "saida" | "entrada"
    nome      = Column(String, nullable=False)
    criado_em = Column(DateTime, server_default=func.now())


class Local(Base):
    """Locais de jogo reutilizáveis."""
    __tablename__ = "locais"

    id        = Column(Integer, primary_key=True, index=True)
    nome      = Column(String, nullable=False, unique=True)
    endereco  = Column(String, nullable=True)
    criado_em = Column(DateTime, server_default=func.now())


class Configuracao(Base):
    """Configurações gerais do sistema (chave → valor)."""
    __tablename__ = "configuracoes"

    chave     = Column(String, primary_key=True)
    valor     = Column(String, nullable=False)
    criado_em = Column(DateTime, server_default=func.now())


class SolicitacaoCadastro(Base):
    """Solicitação de cadastro enviada pelo jogador — aguarda aprovação do admin."""
    __tablename__ = "solicitacoes_cadastro"

    id              = Column(Integer, primary_key=True, index=True)
    nome            = Column(String, nullable=False)
    email           = Column(String, nullable=False)
    telefone        = Column(String, nullable=True)
    data_nascimento = Column(Date, nullable=True)
    posicao         = Column(String, nullable=True)
    posicao_principal = Column(String, nullable=True)
    rg              = Column(String, nullable=True)   # encrypted
    cpf             = Column(String, nullable=True)   # encrypted
    tipo            = Column(String, nullable=False)  # mensalista | avulso
    numero_camisa   = Column(Integer, nullable=True)
    status          = Column(String, nullable=False, default="pendente")  # pendente | aprovado | rejeitado
    criado_em       = Column(DateTime, server_default=func.now())

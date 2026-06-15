# CLAUDE.md — Voleizou App

Contexto completo do projeto para uso por qualquer editor Claude. Leia este arquivo inteiro antes de fazer qualquer alteração.

---

## O que é este projeto

App web de **controle financeiro** do grupo de vôlei **Voleizou** (Porto Alegre). Jogos toda quarta-feira. Gerenciado por Rodrigo Bloch (rodrigobloch@gmail.com).

---

## URLs e repositório

- **Produção:** https://voleizou.com.br
- **Repositório:** https://github.com/rodrigobloch-sudo/voleizou
- **Código local (Rodrigo):** `~/Documents/Volei App/`
- **Iniciar local:** duplo clique em `Iniciar Volei App.command` ou `./iniciar.sh`

---

## Deploy

Git push para `main` → Render detecta e faz deploy automático. Nenhuma ação manual necessária.

```bash
cd ~/Documents/Volei\ App
git add .
git commit -m "mensagem"
git push origin main
```

**Build command no Render:**
```
apt-get install -y tesseract-ocr tesseract-ocr-por && pip install -r requirements.txt
```

**Start command no Render:**
```
python -m uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Backend | Python + FastAPI |
| Banco (produção) | PostgreSQL no Render (`DATABASE_URL`) |
| Banco (local) | SQLite (`volei.db`) |
| Frontend | SPA em `static/index.html` — HTML/CSS/JS + Bootstrap 5 |
| Hosting | Render (plano pago — sem sleep) |
| E-mail | Brevo (SMTP) |
| Leitura de comprovantes | pdfminer + tesseract |

---

## Arquivos principais

```
main.py                  # FastAPI — todas as rotas
models.py                # Modelos SQLAlchemy
database.py              # Conexão (SQLite local / PostgreSQL prod via DATABASE_URL)
leitor_comprovante.py    # Parser de comprovantes Pix (PDF/PNG)
static/index.html        # Frontend SPA completo
static/pagar.html        # Página pública: jogador vê pendências e envia comprovante
static/cadastro.html     # Página pública: auto-cadastro via link de convite
static/definir-senha.html
static/redefinir-senha.html
static/logo.png
```

---

## Variáveis de ambiente (Render)

| Variável | Uso |
|----------|-----|
| `DATABASE_URL` | PostgreSQL Render |
| `PUBLIC_URL` | https://voleizou.com.br |
| `SECRET_KEY` | HMAC para cookies e tokens |
| `ENCRYPTION_KEY` | Fernet para CPF/RG em repouso |
| `SMTP_HOST/PORT/USER/PASS/FROM` | E-mail via Brevo |

---

## Modelos de dados (models.py)

### Jogador
`id, nome, apelido (nullable), tipo (mensalista|avulso), telefone, posicao (CSV), numero_camisa, data_nascimento, cpf (encrypted), rg (encrypted), email (unique), ativo, foto (LargeBinary), foto_mimetype, criado_em`

### Jogo
`id, data, categoria, observacao, valor, mensalistas_ausentes (CSV de IDs), status (Planejado|Confirmado|Cancelado|Realizado), endereco, local_nome, criado_em`

### Local
`id, nome (unique), endereco, criado_em` — locais reutilizáveis para jogos

### ParticipacaoAvulso
`jogo_id, jogador_id` (unique pair)

### Pagamento
`id, jogador_id, valor, data_pagamento, referencia, tipo (mensalidade|avulso), observacao`

### Saida
`id, descricao, valor, data, categoria, observacao, status (Confirmada|Prevista), criado_em`

### Entrada
`id, descricao, valor, data, categoria (Patrocínio|Prêmios|Doações|Outros), observacao`

### Pendencia
`id, jogador_id, jogo_id (nullable=mensalidade), tipo (evento|mensalidade), descricao, valor, referencia, quitado, quitado_em` — UniqueConstraint(jogador_id, jogo_id)

### Configuracao
`chave (PK), valor` — defaults: `valor_mensalidade=120.0`, `valor_avulso=35.0`

### Categoria
`id, tipo (jogo|saida|entrada), nome` — UniqueConstraint(tipo, nome); seed automático no startup

### SolicitacaoCadastro
`id, nome, email, telefone, data_nascimento, posicao, rg, cpf, tipo, numero_camisa, status (pendente|aprovado|rejeitado)`

### Usuario
`id, nome, usuario (único, formato primeiro.ultimo), senha_hash (bcrypt), tipo (admin|mensalista|avulso), jogador_id (FK nullable)`

### ArquivoComprovante
`id, nome_original, conteudo (LargeBinary), mimetype`

---

## Migrações automáticas

**REGRA IMPORTANTE:** ao adicionar qualquer coluna nova em um model, adicionar também na lista `migrações` dentro de `_migrar()` em `main.py`.

```python
migrações = [
    ("tabela", "coluna", "TIPO_SQL"),
    ...
]
```

- SQLite: `ALTER TABLE ... ADD COLUMN` com try/except
- PostgreSQL: `ADD COLUMN IF NOT EXISTS`

---

## Autenticação e segurança

- Cookies HMAC-SHA256 (janela de 30 dias) — `_criar_token()` / `_verificar_token()`
- `BaseHTTPMiddleware` bloqueia todas as rotas não-públicas
- Rotas públicas definidas em `_PUBLICOS_EXATOS`, `_PUBLICOS_PREFIXO`, `_PUBLICOS_SUFIXO`
- Bcrypt para senhas de usuários
- Fernet para CPF/RG — `_encrypt()` / `_decrypt()` (tolera valores em claro para dados antigos)

---

## Permissões (MENUS_SLUGS)

`dashboard, jogadores, jogos, calendario, pagamentos, saidas, entradas, caixa, pendencias, pendentes, config, config-valores, config-categorias`

---

## Saídas — status Confirmada / Prevista

- Campo `status` em `Saida` (default `"Confirmada"`)
- `GET /api/saidas` retorna `status` em cada item
- `PUT /api/saidas/{id}` — edição completa (descrição, valor, data, categoria, status, observação)
- `POST /api/saidas` aceita `status` no body
- Saídas **Confirmadas** contam em todos os cálculos normalmente
- Saídas **Previstas** só aparecem nos campos `total_saidas_previstas` e `saldo_previsto` — visíveis apenas para admin na aba Caixa Geral

---

## Caixa Geral (`GET /api/caixa`)

Resposta:
```json
{
  "total_entradas": ...,
  "total_saidas": ...,           // só Confirmadas
  "total_saidas_previstas": ..., // só Previstas
  "saldo_caixa": ...,            // entradas - saídas confirmadas
  "saldo_previsto": ...,         // entradas - confirmadas - previstas
  "total_pendencias_abertas": ..., // soma de Pendencia onde quitado=False
  "historico": [...]
}
```

**Layout visual (aba Caixa Geral):**
- Linha 1 (todos os usuários): Total entradas · Total saídas · Saldo confirmado
- Linha 2 (só admin): Saídas previstas · Pendências em aberto · Saldo com previstos

---

## Pendências financeiras

- `DATA_INICIO_COBRANCA = date(2026, 5, 1)` — mensalidades cobradas a partir de maio/2026
- Janela de cobrança: mês atual + mês anterior
- Novo mensalista não entra em jogos Confirmados/Realizados nem em jogos com data < hoje
- `pagar.html`: checkboxes por pendência → envia `pendencias_ids` + `inclui_mensalidade`
- Aprovação: remove prefixo `PENDENTE|` do observacao e marca pendências como quitadas
- Admin pode quitar manualmente (`POST /api/pendencias/{id}/quitar`) ou excluir individualmente

---

## Chave Pix

**Chave:** `voleizoupoa@gmail.com`

Aparece em **todas** as mensagens de WhatsApp com o texto:
> Pague com a chave pix: voleizoupoa@gmail.com

Mensagens que incluem a chave:
1. Cobrança individual — avulso
2. Cobrança individual — mensalidade
3. Cobrança em massa
4. Cobrança via WhatsApp com seleção de pendências

Também aparece em `static/pagar.html` como card azul com botão "Copiar".

---

## Mensagens de WhatsApp

Padrão geral das mensagens:
```
Olá {nome}!

Aqui é da equipe do Voleizou!

{contexto do pagamento}

Pague com a chave pix: voleizoupoa@gmail.com

{link de pagamento}

Obrigado!
```

Cobrança com seleção de pendências adiciona itemização e nota "A mensalidade deve ser paga até o dia 10 de cada mês." quando há mensalidade.

**Telefone WPP:** sempre com prefixo `55` (verifica `tel.startsWith('55')` antes de adicionar).
**Sem emojis** nas mensagens (decisão do usuário).

---

## Frontend — padrões críticos

### Passar dados para onclick
**NUNCA** use `JSON.stringify()` inline em atributos `onclick` — quebra com aspas e caracteres especiais.

**Padrão correto:** salvar os dados em um map global e referenciar pelo id:
```javascript
window._saidasMap = {};
saidas.forEach(s => { window._saidasMap[s.id] = s; });
// no onclick:
onclick="editarSaida(${s.id})"
// na função:
function editarSaida(id) { const s = window._saidasMap[id]; ... }
```

Maps globais em uso: `window._saidasMap`, `window._pendenciasEntries`, `window._optsJogador`

### API calls
```javascript
api('GET/POST/PUT/DELETE', '/api/rota', body)
// SEMPRE usa credentials: 'include' automaticamente
```

### Links externos (WhatsApp)
```javascript
abrirUrl(url) // usa <a>.click() — mais confiável que window.open no mobile
```

### Flatpickr (campos de data)
Todos os campos de data devem ser registrados no array do DOMContentLoaded:
```javascript
['jogo-data', 'duplicar-jogo-data', 'pag-data', 'saida-data', 'entrada-data', 'duplicar-saida-data'].forEach(id => {
  const el = document.getElementById(id);
  if (el) flatpickr(el, opOpts);
});
```

### baseUrl
Lido de `GET /api/me` → campo `public_url`. Usado para links de pagamento: `${baseUrl}/pagar/${jogadorId}`.

---

## Importações SQLAlchemy — atenção

```python
from sqlalchemy import extract, func  # func é necessário para sum/coalesce
```

`func` precisa estar importado para usar `func.sum()`, `func.coalesce()` etc. nas queries.

---

## Fotos de jogadores

- Armazenadas como LargeBinary no banco (`foto` + `foto_mimetype` em `Jogador`)
- Upload via modal de edição — Pillow redimensiona para 400×400 JPEG
- `avatarHtml(jogadorId, temFoto, nome, size)` — helper JS reutilizável (foto ou inicial em círculo azul)
- Endpoints: `POST/DELETE/GET /api/jogadores/{id}/foto`

---

## Aniversários

- `_job_aniversario()` — APScheduler cron às 08:00 diariamente
- Envia e-mail personalizado para jogadores com aniversário no dia
- `POST /api/admin/aniversario/disparar` — disparo manual pelo admin
- Calendário exibe aniversários com 🎂 e cor rosa (`#be185d`)

---

## Status de jogos

`Planejado | Confirmado | Cancelado | Realizado`

- `_status_efetivo(data, status_raw)`: se data no passado e status é Planejado ou Confirmado → retorna "Realizado" (não altera DB)
- Novo mensalista só entra automaticamente em jogos com status Planejado E data >= hoje

---

## Admins como mensalistas

- Admins são tratados como mensalistas em todos os cálculos via OR query
- `_corrigir_tipo_jogadores_admin()` no startup: vincula Usuario admin ao Jogador de mesmo nome
- `_corrigir_usernames()` no startup: renomeia usernames para formato `primeiro.ultimo`

---

## Comprovante de pagamento

- Jogador envia em `pagar.html` — PDF ou imagem
- `leitor_comprovante.py` (pdfminer + tesseract) extrai valor e data
- Salvo como LargeBinary em `ArquivoComprovante`
- Pagamento criado com `observacao = "PENDENTE|comprovante_id:X|nome:Y|pendencias_ids:Z"`
- Admin aprova na aba "Aprovações": remove prefixo `PENDENTE|`, marca pendências como quitadas

---

## Locais de jogo

- Model `Local` — dropdown no modal de jogo, auto-preenche endereço
- Ao salvar jogo com `local_nome`, o local é salvo/atualizado automaticamente
- Gerenciamento via modal de lápis no dropdown

---

## Navegação SPA

- `navegarPara(page, pushHistory)` — `history.pushState`
- `localStorage` salva última página; restaurada no login seguinte
- `visibility:hidden` em `#main` até `aplicarPermissoes()` terminar

---

## Config valores

- `GET/PUT /api/config/valores` → `valor_mensalidade` e `valor_avulso` editáveis pelo admin
- `GET/POST/PUT/DELETE /api/categorias` → categorias de jogo, saída e entrada

---

## Regras gerais de desenvolvimento

1. **Sempre ler o arquivo atual antes de editar** — o app está em evolução contínua
2. **Migrações:** qualquer nova coluna deve entrar em `_migrar()`
3. **Mensagens WPP:** sem emojis; sempre incluir chave Pix
4. **onclick com dados:** usar `window._xyzMap[id]`, nunca JSON.stringify inline
5. **Deploy:** commit + push para `main` — Render cuida do resto
6. **SQLAlchemy:** importar `func` junto com `extract` quando precisar de agregações

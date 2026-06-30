# Bot Crypto — Expansão de Volume

Bot automatizado de trading para futuros na Binance, operando a estratégia **Expansão de Volume** (squeeze + breakout) no timeframe de 15 minutos.

## Funcionalidades

- Estratégia "Expansão de Volume" em BTCUSDT e BNBUSD (15m)
- Squeeze detection: ATR comprimido seguido de breakout com volume
- Suporte a múltiplos símbolos simultâneos
- Modo PAPER (simulação) e LIVE (ordens reais)
- Integração com Binance Testnet para desenvolvimento seguro
- Persistência em Supabase (PostgreSQL) com RLS
- Dashboard Streamlit com autenticação Supabase Auth
- Auto-refresh a cada 15s no dashboard
- Keepalive automático para evitar idle no Render free tier
- SL/TP dinâmico baseado em ATR (1.2× / 3.0×)
- Risco fixo de 1% do saldo por trade

## Estratégia — Expansão de Volume

A estratégia opera em candles de 15 minutos e segue 3 etapas:

### 1. Squeeze Detection
O ATR(14) é monitorado em uma janela de 20 candles. Um **squeeze** ocorre quando o ATR atual está dentro de 2% do menor ATR dos últimos 20 candles:

```
squeeze = atr <= 1.02 × min(atr[-20:])
```

Isso indica que a volatilidade está comprimida — o preço está "coletando energia" para uma explosão.

### 2. Breakout Entry
Uma vez que o squeeze é detectado, o tracker guarda o **high** e **low** da barra de squeeze. A partir daí, **cada candle seguinte** é verificado para breakout:

- **LONG**: close > squeeze_high **e** volume > 1.7 × Volume SMA(20)
- **SHORT**: close < squeeze_low **e** volume > 1.7 × Volume SMA(20)

O nível de squeeze permanece ativo até que um breakout ocorra. Se um novo squeeze surgir, o nível é atualizado.

### 3. Saída
- **Stop Loss**: ATR × 1.2 de distância do preço de entrada
- **Take Profit**: ATR × 3.0 de distância do preço de entrada
- Risco: 1% do saldo por trade

```
quantidade = (saldo × 0.01) ÷ distância_até_o_SL
```

## Arquitetura

```
bot_crypto/
├── dashboard/
│   ├── __init__.py
│   └── app.py              # Streamlit dashboard (login, controle, monitor)
├── deploy/
│   ├── bot-crypto.service   # Systemd unit para VPS
│   ├── Dockerfile           # Docker para deploy alternativo
│   └── setup-vps.sh         # Script de setup para VPS
├── src/
│   ├── __init__.py
│   ├── config.py            # Settings via python-dotenv + env vars
│   ├── database.py          # SupabaseDB — auth, bot_state, trades_log
│   ├── exchange.py          # BinanceFutures — API signed/unsigned com retry
│   ├── indicators.py        # ATR(14) Wilder RMA, Volume SMA(20)
│   ├── main.py              # TradingBot + SqueezeTracker (loop principal)
│   └── strategy.py          # detect_squeeze, check_entry, check_exit, sl/tp
├── .env.example
├── Procfile                 # Render start command
├── README.md
├── render.yaml              # Render Blueprint
├── requirements.txt
├── runtime.txt              # Python 3.11.7
├── supabase_schema.sql      # Schema PostgreSQL + RLS + triggers
└── test_integration.py      # Testes de sanidade (não versionado)
```

### Fluxo de Dados

```
Binance Futures API  ←→  exchange.py  ←→  main.py (TradingBot)
                                              ↕
                                         database.py
                                              ↕
                                      Supabase PostgreSQL
                                       ↕          ↕
                                bot_state    trades_log
                                       ↕
                              dashboard/app.py (Streamlit)
                                       ↕
                                  Usuário (browser)
```

### Módulos

| Arquivo | Responsabilidade |
|---|---|
| `src/indicators.py` | Funções puras pandas: `atr(14)`, `volume_sma(20)` |
| `src/strategy.py` | Lógica da estratégia: squeeze, entrada, saída, SL/TP |
| `src/main.py` | `SqueezeTracker` + `TradingBot` — loop principal com polling de 10s |
| `src/exchange.py` | `BinanceFutures` — cliente HTTP async com HMAC-SHA256, retry, erros tipados |
| `src/database.py` | `SupabaseDB` — Auth, CRUD `bot_state` e `trades_log` |
| `src/config.py` | `Settings` frozen dataclass via `python-dotenv` |
| `dashboard/app.py` | Streamlit SPA — login, status, preços, PnL, trades, auto-refresh |

## Configuração

### Variáveis de Ambiente (.env)

```env
# Supabase
SUPABASE_URL=https://seu-projeto.supabase.co
SUPABASE_ANON_KEY=sua_anon_key
SUPABASE_SERVICE_KEY=sua_service_role_key

# Binance
BINANCE_API_KEY=your_api_key
BINANCE_SECRET_KEY=your_secret_key
BINANCE_TESTNET=true

# Trading
TRADE_MODE=paper          # paper | live
SYMBOLS=BTCUSDT,BNBUSD
```

### Binance Testnet

1. Crie uma conta em https://testnet.binancefuture.com/
2. Gere uma API Key com permissão "Futures Trading"
3. Defina `BINANCE_API_KEY` e `BINANCE_SECRET_KEY` no .env
4. Defina `BINANCE_TESTNET=true` e `TRADE_MODE=paper`

## Deploy

### Render (atual)

- **Dashboard** (web service free): link definido no Render Dashboard
- **Bot**: roda em background thread junto com o dashboard
- **Keepalive**: ping a cada 5min na própria URL para evitar idle/sleep

### VPS (alternativa)

```bash
sudo bash deploy/setup-vps.sh
sudo systemctl start bot-crypto
```

### Docker

```bash
docker build -t bot-crypto -f deploy/Dockerfile .
docker run --env-file .env bot-crypto
```

## Dashboard

Após login, o dashboard exibe:

- Badge de status (RUNNING/STOPPED/ERROR) com timestamp
- Preços atuais dos símbolos (via API pública Binance)
- Saldo testnet (via API signed)
- Posição atual e níveis de squeeze
- Botão ▶ INICIAR / ⏹ PARAR
- Gráfico de PnL acumulado
- Resumo: total trades, wins, losses, win rate, PnL
- Trades abertos
- Histórico recente com PnL colorido
- Auto-refresh a cada 15 segundos

## Histórico de Desenvolvimento

| Etapa | Descrição |
|---|---|
| 1 | Lógica da estratégia em pandas (`indicators.py`, `strategy.py`) |
| 2 | Cliente Binance com retry e tratamento de erros (`exchange.py`) |
| 3 | Camada de banco Supabase com RLS (`database.py`, `supabase_schema.sql`) |
| 4 | Loop principal do bot com SqueezeTracker (`main.py`) |
| 5 | Dashboard Streamlit com login Supabase Auth (`dashboard/app.py`) |
| 6 | Deploy Render + GitHub + CI |
| 7 | Correção de bugs: SqueezeTracker (active reset), versão httpx, keepalive |
| 8 | Melhorias: gráfico PnL, preços ao vivo, balance testnet, auto-refresh |
| 9 | Teste de sanidade com dados reais (15 sinais em 5 dias BTCUSDT) |

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/>
  <img src="https://img.shields.io/badge/docker-socket-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker"/>
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite"/>
  <img src="https://img.shields.io/badge/license-Apache%202.0-red?style=for-the-badge" alt="License"/>
</p>

<h1 align="center">🛡️ Sentinel</h1>
<h3 align="center">Docker Autonomous Orchestrator & Monitor</h3>

<p align="center">
  Daemon assíncrono e autônomo para operações de Infraestrutura e DevOps.<br/>
  Monitora métricas via Docker Socket, executa ações corretivas automáticas<br/>
  e previne crash loops com um Circuit Breaker integrado.
</p>

---

## 📋 Índice

- [Visão Geral](#-visão-geral)
- [Stack Tecnológico](#-stack-tecnológico)
- [Arquitetura](#-arquitetura)
- [Design Patterns](#-design-patterns)
- [Estrutura de Diretórios](#-estrutura-de-diretórios)
- [Instalação](#-instalação)
- [Configuração](#-configuração)
- [Uso](#-uso)
- [API de Observabilidade](#-api-de-observabilidade)
- [Testes](#-testes)
- [Deploy com Docker Compose](#-deploy-com-docker-compose)
- [Licença](#-licença)

---

## 🔭 Visão Geral

O **Sentinel** é um daemon enterprise-grade que opera de forma autônoma sobre a sua infraestrutura Docker. Ele:

- **Coleta métricas** (CPU, RAM, Health Status) de todos os containers em execução via Docker Socket — de forma totalmente assíncrona e non-blocking.
- **Avalia regras** definidas em YAML contra as métricas coletadas, com suporte a duração sustentada (a condição precisa persistir por N segundos antes de agir).
- **Executa ações corretivas** automáticas: restart, stop, scale (via `docker compose`).
- **Previne Crash Loop BackOff** com um Circuit Breaker apoiado em SQLite — se um container for reiniciado mais de N vezes em M minutos, a ação autônoma é suspensa e humanos são alertados.
- **Notifica** via múltiplos canais (Console, Discord, Slack) usando o padrão Strategy.
- **Expõe uma API interna** para observabilidade do seu próprio estado.

---

## 🧱 Stack Tecnológico

| Componente | Tecnologia | Propósito |
|---|---|---|
| **Linguagem** | Python 3.11+ | Tipagem forte via `mypy` strict |
| **Docker** | `aiodocker` | Cliente assíncrono para o Docker Daemon |
| **API** | FastAPI + Uvicorn | Servidor de observabilidade embutido |
| **Banco de Dados** | SQLite + `aiosqlite` | Histórico de intervenções e estado do Circuit Breaker |
| **Logging** | Loguru | Logs estruturados em JSON (Datadog/ELK-ready) |
| **Configuração** | Pydantic Settings | Validação rigorosa de `.env` e `rules.yaml` |
| **Notificações** | `aiohttp` | Webhooks assíncronos para Discord e Slack |
| **Lint / Type Check** | `ruff` + `mypy` | Qualidade de código |
| **Testes** | `pytest` + `pytest-asyncio` | 36 testes unitários |

---

## 🏗️ Arquitetura

O Sentinel segue os princípios de **Clean Architecture**, separando responsabilidades em módulos independentes e intercambiáveis.

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py (Orchestrator)                    │
│           Bootstraps + runs 3 concurrent asyncio tasks           │
├──────────┬──────────────────────────────┬────────────────────────┤
│          │                              │                        │
│  ┌───────▼───────┐   ┌─────────────────▼──────────┐   ┌────────▼────────┐
│  │  Collector     │   │     Rules Engine            │   │   FastAPI        │
│  │  (aiodocker)   │──▶│  condition eval             │   │   /health        │
│  │                │   │  sustained-duration tracker  │   │   /history       │
│  └────────────────┘   │  circuit breaker check       │   │   /circuit-...   │
│                       └──────┬──────────┬────────────┘   └─────────────────┘
│                              │          │
│                    ┌─────────▼──┐  ┌────▼──────────┐
│                    │  Actions    │  │  Notifiers     │
│                    │  (Strategy) │  │  (Strategy)    │
│                    │  • Restart  │  │  • Console     │
│                    │  • Stop     │  │  • Discord     │
│                    │  • Scale    │  │  • Slack        │
│                    └─────────┬──┘  └────────────────┘
│                              │
│                    ┌─────────▼──────────┐
│                    │  State Manager     │
│                    │  (SQLite)          │
│                    │  • History         │
│                    │  • Circuit Breaker │
│                    └────────────────────┘
└─────────────────────────────────────────────────────────────────┘
```

### Fluxo de Execução

1. **Collector** consulta o Docker Daemon e normaliza métricas (compatível com cgroup v1/v2, Linux/macOS/WSL).
2. **Rules Engine** cruza métricas com as regras configuradas.
3. Se a condição for satisfeita pelo tempo sustentado, o engine consulta o **State Manager**.
4. Se o **Circuit Breaker** estiver fechado, a **Action** é executada e uma **Notification** é enviada.
5. Se o **Circuit Breaker** estiver aberto (muitos restarts recentes), a ação é suspensa e um alerta CRITICAL é emitido para intervenção humana.

---

## 🎯 Design Patterns

### Strategy Pattern
Os módulos `actions/` e `notifiers/` implementam interfaces abstratas (`BaseAction`, `BaseNotifier`). O engine invoca polimorficamente sem saber qual implementação concreta está em uso.

```python
# O engine não sabe se é Restart, Stop ou Scale
await action.execute(container_id, container_name, timeout)

# O engine não sabe se é Console, Discord ou Slack
await notifier.send(title, message, severity, container_name)
```

### Observer Pattern
O Rules Engine observa o fluxo de métricas do Collector de forma assíncrona a cada ciclo de polling, reagindo a mudanças de estado.

### Circuit Breaker / State Pattern
O State Manager mantém um registro persistente (SQLite) de todas as intervenções. Antes de executar qualquer ação destrutiva:

```
"Eu já reiniciei esse container N vezes nos últimos M minutos?"
├── NÃO → Executa a ação normalmente
└── SIM → Circuit Breaker ABERTO → Suspende ação → Alerta humanos
```

### Fail Fast
A configuração (`.env` + `rules.yaml`) é validada rigorosamente via Pydantic **antes** do daemon inicializar. Regex inválido, métricas desconhecidas, ou campos obrigatórios ausentes impedem a inicialização.

---

## 📂 Estrutura de Diretórios

```
sentinel/
├── src/
│   ├── __init__.py
│   ├── main.py                     # Orquestrador asyncio
│   ├── core/
│   │   ├── config.py               # Pydantic Settings + YAML Schema
│   │   ├── logger.py               # Loguru JSON estruturado
│   │   └── exceptions.py           # Exceções customizadas
│   ├── collectors/
│   │   └── docker_async.py         # aiodocker + normalização cross-platform
│   ├── engine/
│   │   ├── rules.py                # Motor de regras + sustained-duration
│   │   └── state_manager.py        # SQLite + Circuit Breaker
│   ├── actions/
│   │   ├── base.py                 # Interface abstrata (Strategy)
│   │   ├── restart.py              # RestartAction + StopAction
│   │   └── scale.py                # ScaleComposeAction
│   ├── notifiers/
│   │   ├── base.py                 # Interface + ConsoleNotifier
│   │   ├── discord.py              # Rich embeds via webhook
│   │   └── slack.py                # Block Kit via webhook
│   └── api/
│       ├── server.py               # Uvicorn como asyncio task
│       └── routes.py               # Endpoints de observabilidade
├── tests/
│   ├── test_config.py              # Validação Pydantic (14 testes)
│   ├── test_state_manager.py       # SQLite + Circuit Breaker (7 testes)
│   ├── test_rules_engine.py        # Matching + Conditions (10 testes)
│   └── test_api.py                 # Endpoints FastAPI (5 testes)
├── db/                             # Banco SQLite (criado em runtime)
├── rules.yaml                      # Regras de monitoramento
├── docker-compose.yml              # Deploy com socket mount
├── Dockerfile                      # Multi-stage, non-root
├── pyproject.toml                  # pytest + mypy + ruff
├── requirements.txt                # Dependências
├── .env.example                    # Template de configuração
├── .gitignore
└── LICENSE                         # Apache License 2.0
```

---

## 🚀 Instalação

### Pré-requisitos

- Python 3.11+
- Docker Engine com socket acessível
- (Opcional) Docker Compose v2

### Setup local

```bash
# Clonar o repositório
git clone https://github.com/soneylegal/sentinel.git
cd sentinel

# Criar virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Instalar dependências
pip install -r requirements.txt

# Copiar e editar configuração
cp .env.example .env
```

---

## ⚙️ Configuração

### Variáveis de Ambiente (`.env`)

| Variável | Default | Descrição |
|---|---|---|
| `SENTINEL_DOCKER_URL` | `unix:///var/run/docker.sock` | URL do Docker Daemon |
| `SENTINEL_API_HOST` | `0.0.0.0` | Host da API de observabilidade |
| `SENTINEL_API_PORT` | `9120` | Porta da API |
| `SENTINEL_RULES_PATH` | `rules.yaml` | Caminho do arquivo de regras |
| `SENTINEL_DB_PATH` | `db/sentinel.db` | Caminho do banco SQLite |
| `SENTINEL_POLL_INTERVAL` | `15` | Intervalo de coleta em segundos |
| `SENTINEL_CIRCUIT_BREAKER_THRESHOLD` | `3` | Restarts antes de desarmar o disjuntor |
| `SENTINEL_CIRCUIT_BREAKER_WINDOW_MINUTES` | `5` | Janela de tempo do disjuntor |
| `SENTINEL_LOG_LEVEL` | `INFO` | Nível de log |
| `SENTINEL_LOG_FORMAT` | `json` | Formato: `json` ou `pretty` |
| `SENTINEL_DISCORD_WEBHOOK_URL` | — | Webhook do Discord |
| `SENTINEL_SLACK_WEBHOOK_URL` | — | Webhook do Slack |

### Regras de Monitoramento (`rules.yaml`)

Cada regra define:

```yaml
rules:
  - name: "Nome da Regra"
    description: "Descrição"
    enabled: true
    match:
      container_name_pattern: ".*"     # Regex: quais containers monitorar
      exclude_patterns:
        - "^sentinel$"                 # Regex: quais excluir
    condition:
      metric: cpu_percent              # cpu_percent | memory_percent | memory_usage_mb | health_status
      operator: ">"                    # > | < | >= | <= | ==
      threshold: 90.0                  # Valor limite
      sustained_seconds: 60           # Duração mínima da violação
    action:
      type: restart                    # restart | stop | scale | exec
      timeout: 30                      # Timeout para ação graceful
    notify:
      channels:
        - console                      # console | discord | slack
      severity: critical               # info | warning | critical
```

#### Regras pré-configuradas

| Regra | Condição | Ação |
|---|---|---|
| High CPU Auto-Restart | CPU > 90% por 60s | Restart |
| Memory Leak Detection | RAM > 85% por 120s | Restart |
| Unhealthy Container Watchdog | health_status == unhealthy por 30s | Restart |

---

## ▶️ Uso

### Execução local

```bash
# Ativar venv
source .venv/bin/activate

# Iniciar o daemon
python -m src.main
```

O Sentinel irá:
1. Validar toda a configuração (Fail Fast).
2. Conectar-se ao Docker Daemon.
3. Inicializar o banco SQLite.
4. Iniciar a API de observabilidade na porta `9120`.
5. Entrar no loop de monitoramento.

### Parar o daemon

```bash
# Ctrl+C (SIGINT) ou
kill -SIGTERM <pid>
```

O Sentinel faz shutdown graceful, fechando conexões e banco de dados.

---

## 📡 API de Observabilidade

A API roda embutida no mesmo event loop do daemon (zero overhead de IPC).

| Método | Endpoint | Descrição |
|---|---|---|
| `GET` | `/health` | Status + conexão Docker + uptime |
| `GET` | `/history` | Últimas 50 intervenções autônomas |
| `GET` | `/circuit-breakers` | Estado de todos os disjuntores |
| `POST` | `/circuit-breakers/{name}/reset` | Reset manual de um disjuntor |
| `GET` | `/docs` | Swagger UI interativo |
| `GET` | `/redoc` | Documentação ReDoc |

### Exemplos

```bash
# Verificar saúde do daemon
curl http://localhost:9120/health
# {"status":"ok","docker_connected":true,"uptime_seconds":3421.5,"version":"1.0.0",...}

# Ver histórico de ações
curl http://localhost:9120/history
# {"count":3,"records":[{"container_name":"webapp","action_type":"restart",...}]}

# Resetar disjuntor manualmente
curl -X POST http://localhost:9120/circuit-breakers/webapp/reset
# {"status":"ok","container_name":"webapp","message":"Circuit breaker reset..."}
```

---

## 🧪 Testes

```bash
# Rodar todos os testes
python -m pytest tests/ -v

# Resultado esperado:
# tests/test_api.py              5 passed
# tests/test_config.py          14 passed
# tests/test_rules_engine.py    10 passed
# tests/test_state_manager.py    7 passed
# ===================== 36 passed =====================
```

### Cobertura de testes

| Módulo | Testes | O que valida |
|---|---|---|
| `test_config.py` | 14 | Pydantic settings, regex, YAML parsing, Fail Fast |
| `test_state_manager.py` | 7 | SQLite CRUD, Circuit Breaker trip/reset, isolamento por container |
| `test_rules_engine.py` | 10 | Pattern matching, operadores, sustained-duration, exclusões |
| `test_api.py` | 5 | Health OK/degraded, history, circuit breaker endpoints |

---

## 🐳 Deploy com Docker Compose

```bash
# Build e start em background
docker compose up -d --build

# Ver logs em tempo real
docker compose logs -f sentinel

# Verificar saúde
curl http://localhost:9120/health

# Parar
docker compose down
```

### O que o `docker-compose.yml` configura:

- **Socket mount** (`/var/run/docker.sock`) em modo read-only.
- **Volume persistente** para o banco SQLite.
- **Healthcheck** contra o endpoint `/health`.
- **Log rotation** (max 10MB, 3 arquivos).
- **Non-root user** no container.
- **Restart policy** `unless-stopped`.

---

## 📄 Licença

Este projeto está licenciado sob a **Apache License 2.0** — veja o arquivo [LICENSE](LICENSE) para detalhes.

```
Copyright 2026 Davi Laurindo

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
```

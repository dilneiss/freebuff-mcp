# Instalação

Ponte MCP que expõe o CLI do Freebuff como ferramentas MCP (`freebuff_status`,
`freebuff_run`, `freebuff_chats`, `freebuff_transcript`) para o OMP ou qualquer
cliente MCP.

## Pré-requisitos

- **Windows** (o bridge depende de ConPTY; não há suporte a Linux/macOS).
- **Python 3.11**.
- **pywinpty** (driver de PTY usado para dirigir a TUI).
- **Freebuff CLI** instalado e **logado** na máquina. O caminho procurado é
  `%USERPROFILE%\.config\manicode\freebuff.exe`, com fallback para um
  `freebuff` no `PATH`; o login pode ser conferido no próprio TUI.
- **OMP** instalado, para registrar o servidor (item abaixo).

## 1. Criar o venv e instalar dependências

Na raiz do projeto (`C:\freebuff-mcp`):

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install pywinpty
```

O `server.py` usa apenas a biblioteca padrão; `pywinpty` é a única dependência
externa.

## 2. Registrar no OMP

Edite `~/.omp/agent/mcp.json` e adicione a entrada `freebuff` como servidor
**stdio**:

```json
{
  "mcpServers": {
    "freebuff": {
      "type": "stdio",
      "command": "C:/freebuff-mcp/.venv/Scripts/python.exe",
      "args": ["C:/freebuff-mcp/server.py"],
      "timeout": 0
    }
  }
}
```

`timeout` é o timeout da requisição MCP no lado do cliente, em milissegundos
(`0` desliga o timeout do cliente). Use `0`: uma execução do Freebuff dura
minutos, e o próprio `freebuff_run` já limita a execução pelo seu
`timeout_seconds` (padrão 900). Com `60000`, qualquer run acima de 60 s é
abortado pelo OMP antes de o Freebuff terminar.

## 3. Verificar

Com o OMP configurado, peça o status ao servidor:

```
omp -p --no-session --auto-approve --no-title "Call freebuff_status and print version and logged_in"
```

A resposta deve trazer a versão do CLI e `logged_in: true`. Se o comando
reclamar que a ferramenta não existe, o `mcp.json` não foi lido: confira o
caminho do `python.exe`/`server.py` e reinicie o OMP.

## Modelos

Aponte o modelo de três formas:

| Forma | Chamada | Efeito |
|---|---|---|
| Ver catálogo | `freebuff_models` | lista as 12 opções com `key`, esforços, contexto e a atual |
| Trocar permanente | `freebuff_set_model {"model":"flash fast","effort":"low"}` | grava `freebuffModel`/`freebuffModelKey`/`freebuffReasoningEfforts` no `settings.json`; vale do próximo run em diante |
| Trocar só um run | `freebuff_run {"model":"SOLAR PRO", "prompt":"..."}` | faz snapshot do `settings.json`, aplica o override, executa e restaura os bytes originais |

A `key` manda; o rótulo pode estar defasado (o valor de fábrica chama
`m-8db1ccbba6` de `mimo/mimo-v2.5`, mas é *DeepSeek V4.1 Flash*). Se um run for
morto no meio, o snapshot `freebuff-mcp-settings-snapshot.json` permite ao
servidor restaurar o `settings.json` no próximo start.

## Variáveis de ambiente

| Variável | Padrão | Efeito |
|---|---|---|
| `FREEBUFF_EXE` | `%USERPROFILE%\.config\manicode\freebuff.exe` | Caminho do binário do CLI; ignorado se o arquivo não existir. |
| `FREEBUFF_CONFIG_DIR` | `%USERPROFILE%\.config\manicode` | Diretório de configuração/sessões consultado para status, leases e chats. |

Ambas são lidas no start do processo MCP — depois de alterá-las, reinicie o OMP
para que o servidor seja relançado.

## Comportamento de sessão

Antes de cada execução, o servidor faz um *preflight* dos arquivos
`freebuff-live-*.json` em `FREEBUFF_CONFIG_DIR`:

- Registros cujo processo dono (`ownerPid`) já morreu são **removidos** — leases
  mortos não bloqueiam a execução, e o que foi apagado aparece em
  `removed_stale` na resposta.
- Registros de um processo **vivo** são mantidos mesmo com lease expirado (ele
  ainda pode renová-los), mas ficam listados em `ignored_stale`.
- O resultado define `preflight_action`:
  - `reuse` — há sessão live válida; a execução reaproveita essa instância
    (`reused: true`).
  - `new` — nenhuma sessão utilizável; o bridge inicia uma nova.

Ainda assim, só há uma execução por processo do servidor: um segundo
`freebuff_run` concorrente falha na hora em vez de disputar o CLI.

## Troubleshooting

**“Already running elsewhere” / falha ao iniciar**
Outra instância do Freebuff está segurando a sessão live (TUI aberto em outra
janela, por exemplo). Feche essa instância e tente de novo; o bridge reporta a
mensagem do CLI em vez de tomar a sessão à força.

**O prompt chega achatado em uma linha**
É intencional: o composer da TUI submete ao receber Enter, então o servidor
converte quebras de linha em espaços. Escreva o prompt pensando em texto
corrido — listas com `\n` viram uma única frase.

**Repositórios com `.agents` (ou `mcp.json`) travam no pedido de confiança**
Passe `trust_agents: true` em `freebuff_run` para que o CLI carregue os
`.agents` do repositório sem perguntar. Sem isso, o run fica parado no prompt de
trust. A contrapartida é que o Freebuff executa essas instruções locais —
revise-as antes de habilitar em repositórios de terceiros.

**`freebuff_status` mostra `logged_in: false`**
Faça login no CLI (`freebuff` no terminal) e chame o status de novo.

**Credits / sessão esgotada**
O modo free é limitado por sessão (a TUI mostra `N hours remaining`). O bridge
devolve a tela do próprio CLI quando a sessão não pode ser iniciada.

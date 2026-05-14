# Agent Linux MVP

Agent em Python para o fluxo inicial do projeto:

- registra a maquina no backend;
- usa `machine_id` e `agent_id` proprios para identificar o cadastro, sem depender do hostname;
- envia heartbeat com telemetria rapida;
- identifica a distribuicao Linux real via `os-release`;
- detecta Carbonio e servicos comuns em refresh lento com cache local;
- consulta execucoes pendentes;
- mantem tunel persistente WebSocket para PTY remoto em tempo real;
- executa comandos recebidos da API;
- devolve resultado para o painel.

## Uso

1. Copie `agent-linux/config.example.json` para `agent-linux/config.json`.
2. Ajuste `api_base_url` e `enrollment_token`.
3. Instale as dependencias Python do agent:

```bash
pip install -r requirements.txt
```

Exemplo em producao:

```json
{
  "api_base_url": "https://api.seudominio.com",
  "enrollment_token": "token-forte-de-enrollment",
  "inventory_refresh_interval_sec": 300,
  "terminal_output_batch_ms": 16,
  "terminal_working_directory": "/root"
}
```

4. Registre o agent:

```bash
python agent.py register
```

Se o `register` for executado como `root` em Linux com `systemd`, o serviço `agentlx` é instalado e iniciado automaticamente ao final do cadastro.

5. Para iniciar em background manualmente:

```bash
python agent.py run
```

O comando acima nao prende mais o terminal. Ele cria um processo em background e grava logs em `agent-linux/agent.log`.

Comandos uteis:

```bash
python agent.py status
python agent.py stop
python agent.py run-foreground
```

## Coleta otimizada

- CPU e lida via `/proc/stat`, sem chamar `top`.
- Disco e lido via `os.statvfs()`, sem chamar `df`.
- Memoria e uptime continuam vindo de `/proc`.
- Inventario lento e servicos ficam em cache local e so sao recalculados no intervalo configurado.
- O agent evita `shell=True` na coleta local e usa shell apenas quando precisa executar comandos remotos com pipes, redirecionamentos ou outros recursos de shell.
- O terminal remoto pode abrir direto em um diretorio fixo com `terminal_working_directory`; quando vazio, o agent usa `/root` para root ou a `HOME` do usuario do processo.

## Rodando como servico no boot com auto-restart

Em producao, prefira instalar como servico `systemd`:

```bash
sudo python agent.py install-service
```

Esse comando:

- cria `agentlx.service` em `/etc/systemd/system/`;
- inicia automaticamente no boot;
- reinicia sozinho se o processo cair;
- executa o agent sem depender de terminal aberto.

Para remover:

```bash
sudo python agent.py uninstall-service
```

## Seguranca do MVP

- o cadastro inicial exige `x-agent-enrollment-token`;
- depois do cadastro, o agent usa `Authorization: Bearer <agent_token>`;
- o backend so enfileira templates conhecidos;
- o agent executa apenas o comando retornado pela API para o template liberado.
- quando o tunel persistente estiver online, a API envia um aviso imediato via WebSocket para antecipar o proximo poll e reduzir a latencia das execucoes.

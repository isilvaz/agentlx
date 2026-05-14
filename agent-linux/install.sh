#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_SOURCE_BASE_URL="https://raw.githubusercontent.com/isilvaz/agentlx/main/agent-linux"
DEFAULT_INSTALL_DIR="/opt/agentlx"

API_BASE_URL=""
ENROLLMENT_TOKEN=""
LOCATION=""
AGENT_NAME=""
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
SOURCE_BASE_URL="${AGENTLX_SOURCE_BASE_URL:-$DEFAULT_SOURCE_BASE_URL}"
POLL_INTERVAL_SEC="30"
INVENTORY_REFRESH_INTERVAL_SEC="300"
TERMINAL_OUTPUT_BATCH_MS="16"
TERMINAL_WORKING_DIRECTORY=""
AGENT_VERSION="agentlx-linux-mvp"

log() {
  printf '[agentlx-install] %s\n' "$*"
}

fail() {
  printf '[agentlx-install] erro: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Uso:
  bash install.sh \
    --api-base-url https://api.seudominio.com \
    --enrollment-token TOKEN_FORTE \
    --location DC-SP-01

Parametros obrigatorios:
  --api-base-url URL
  --enrollment-token TOKEN

Parametros opcionais:
  --location NOME
  --agent-name NOME
  --install-dir CAMINHO
  --source-base-url URL
  --poll-interval-sec NUM
  --inventory-refresh-interval-sec NUM
  --terminal-output-batch-ms NUM
  --terminal-working-directory CAMINHO
  --agent-version VALOR
  --help
EOF
}

require_root() {
  if [ "${EUID}" -ne 0 ]; then
    fail "execute este instalador com sudo ou como root."
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --api-base-url)
        API_BASE_URL="${2:-}"
        shift 2
        ;;
      --enrollment-token)
        ENROLLMENT_TOKEN="${2:-}"
        shift 2
        ;;
      --location)
        LOCATION="${2:-}"
        shift 2
        ;;
      --agent-name)
        AGENT_NAME="${2:-}"
        shift 2
        ;;
      --install-dir)
        INSTALL_DIR="${2:-}"
        shift 2
        ;;
      --source-base-url)
        SOURCE_BASE_URL="${2:-}"
        shift 2
        ;;
      --poll-interval-sec)
        POLL_INTERVAL_SEC="${2:-}"
        shift 2
        ;;
      --inventory-refresh-interval-sec)
        INVENTORY_REFRESH_INTERVAL_SEC="${2:-}"
        shift 2
        ;;
      --terminal-output-batch-ms)
        TERMINAL_OUTPUT_BATCH_MS="${2:-}"
        shift 2
        ;;
      --terminal-working-directory)
        TERMINAL_WORKING_DIRECTORY="${2:-}"
        shift 2
        ;;
      --agent-version)
        AGENT_VERSION="${2:-}"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        fail "parametro desconhecido: $1"
        ;;
    esac
  done
}

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "dnf"
    return
  fi
  if command -v yum >/dev/null 2>&1; then
    echo "yum"
    return
  fi
  if command -v zypper >/dev/null 2>&1; then
    echo "zypper"
    return
  fi
  if command -v pacman >/dev/null 2>&1; then
    echo "pacman"
    return
  fi
  if command -v apk >/dev/null 2>&1; then
    echo "apk"
    return
  fi
  fail "nenhum gerenciador de pacotes suportado foi encontrado."
}

install_system_packages() {
  local manager
  manager="$(detect_package_manager)"
  log "instalando dependencias do sistema com ${manager}..."

  case "${manager}" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y python3 python3-pip python3-venv ca-certificates curl
      ;;
    dnf)
      dnf install -y python3 python3-pip ca-certificates curl
      ;;
    yum)
      yum install -y python3 python3-pip ca-certificates curl
      ;;
    zypper)
      zypper --non-interactive install python3 python3-pip ca-certificates curl
      ;;
    pacman)
      pacman -Sy --noconfirm python python-pip ca-certificates curl
      ;;
    apk)
      apk add --no-cache python3 py3-pip ca-certificates curl bash
      ;;
  esac
}

resolve_local_source_dir() {
  local script_source script_dir
  script_source="${BASH_SOURCE[0]:-}"
  if [ -z "${script_source}" ] || [ ! -f "${script_source}" ]; then
    return 1
  fi

  script_dir="$(cd "$(dirname "${script_source}")" && pwd)"
  if [ -f "${script_dir}/agent.py" ] && [ -f "${script_dir}/requirements.txt" ] && [ -f "${script_dir}/config.example.json" ]; then
    printf '%s\n' "${script_dir}"
    return 0
  fi

  return 1
}

download_file() {
  local url="$1"
  local output="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "${url}" -o "${output}"
    return
  fi

  fail "curl nao encontrado para baixar ${url}."
}

fetch_agent_files() {
  local source_dir file
  mkdir -p "${INSTALL_DIR}"

  if source_dir="$(resolve_local_source_dir)"; then
    log "copiando arquivos locais do agent para ${INSTALL_DIR}..."
    for file in agent.py requirements.txt config.example.json; do
      cp "${source_dir}/${file}" "${INSTALL_DIR}/${file}"
    done
    return
  fi

  if [ -z "${SOURCE_BASE_URL}" ]; then
    fail "nao foi possivel localizar os arquivos locais do agent e --source-base-url nao foi informado. Defina DEFAULT_SOURCE_BASE_URL antes de publicar o script ou passe --source-base-url."
  fi

  log "baixando arquivos do agent de ${SOURCE_BASE_URL}..."
  download_file "${SOURCE_BASE_URL%/}/agent.py" "${INSTALL_DIR}/agent.py"
  download_file "${SOURCE_BASE_URL%/}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
  download_file "${SOURCE_BASE_URL%/}/config.example.json" "${INSTALL_DIR}/config.example.json"
}

write_config() {
  local config_file example_file hostname_value
  config_file="${INSTALL_DIR}/config.json"
  example_file="${INSTALL_DIR}/config.example.json"
  hostname_value="$(hostname -s 2>/dev/null || hostname || echo agentlx-host)"

  if [ -z "${AGENT_NAME}" ]; then
    AGENT_NAME="${hostname_value}"
  fi

  python3 - "$config_file" "$example_file" "$API_BASE_URL" "$ENROLLMENT_TOKEN" "$AGENT_NAME" "$LOCATION" "$POLL_INTERVAL_SEC" "$INVENTORY_REFRESH_INTERVAL_SEC" "$TERMINAL_OUTPUT_BATCH_MS" "$TERMINAL_WORKING_DIRECTORY" "$AGENT_VERSION" <<'PY'
import json
import os
import sys

(
    config_path,
    example_path,
    api_base_url,
    enrollment_token,
    agent_name,
    location,
    poll_interval_sec,
    inventory_refresh_interval_sec,
    terminal_output_batch_ms,
    terminal_working_directory,
    agent_version,
) = sys.argv[1:]

data = {}

if os.path.exists(example_path):
    with open(example_path, "r", encoding="utf-8") as handle:
        data.update(json.load(handle))

if os.path.exists(config_path):
    with open(config_path, "r", encoding="utf-8") as handle:
        data.update(json.load(handle))

data["api_base_url"] = api_base_url.rstrip("/")
data["enrollment_token"] = enrollment_token
data["agent_name"] = agent_name
data["location"] = location
data["poll_interval_sec"] = int(poll_interval_sec)
data["inventory_refresh_interval_sec"] = int(inventory_refresh_interval_sec)
data["terminal_output_batch_ms"] = int(terminal_output_batch_ms)
data["terminal_working_directory"] = terminal_working_directory
data["agent_version"] = agent_version
data["agent_token"] = str(data.get("agent_token") or "")
data["machine_id"] = str(data.get("machine_id") or "")
data["agent_id"] = str(data.get("agent_id") or "")

with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=True)
    handle.write("\n")
PY
}

create_virtualenv() {
  local venv_dir="${INSTALL_DIR}/.venv"
  if [ ! -x "${venv_dir}/bin/python" ]; then
    log "criando virtualenv em ${venv_dir}..."
    python3 -m venv "${venv_dir}"
  fi

  log "instalando dependencias Python do agent..."
  "${venv_dir}/bin/python" -m pip install --upgrade pip setuptools wheel
  "${venv_dir}/bin/python" -m pip install -r "${INSTALL_DIR}/requirements.txt"
}

register_agent() {
  local python_bin="${INSTALL_DIR}/.venv/bin/python"
  log "registrando agent..."
  (
    cd "${INSTALL_DIR}"
    "${python_bin}" agent.py register
  )
}

ensure_service_active() {
  local python_bin="${INSTALL_DIR}/.venv/bin/python"

  if ! command -v systemctl >/dev/null 2>&1; then
    fail "systemctl nao encontrado. Este instalador exige systemd para validar o servico."
  fi

  if ! systemctl is-active --quiet agentlx; then
    log "servico agentlx ainda nao esta ativo; tentando instalar/iniciar explicitamente..."
    (
      cd "${INSTALL_DIR}"
      "${python_bin}" agent.py install-service
    )
  fi

  if ! systemctl is-active --quiet agentlx; then
    systemctl --no-pager status agentlx || true
    fail "o servico agentlx nao ficou ativo apos a instalacao."
  fi

  log "servico agentlx validado com sucesso."
  systemctl --no-pager --full status agentlx | sed -n '1,12p'
}

main() {
  require_root
  parse_args "$@"

  [ -n "${API_BASE_URL}" ] || fail "--api-base-url e obrigatorio."
  [ -n "${ENROLLMENT_TOKEN}" ] || fail "--enrollment-token e obrigatorio."

  install_system_packages
  fetch_agent_files
  write_config
  create_virtualenv
  register_agent
  ensure_service_active

  log "instalacao concluida."
  log "diretorio: ${INSTALL_DIR}"
  log "configuracao: ${INSTALL_DIR}/config.json"
}

main "$@"

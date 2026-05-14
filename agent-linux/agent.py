#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import platform
import pty
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    import websockets
except ModuleNotFoundError:
    websockets = None

WebSocketConnection = Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG_PATH = ROOT / "config.example.json"
PID_PATH = ROOT / "agent.pid"
LOG_PATH = ROOT / "agent.log"
SYSTEMD_SERVICE_NAME = "agentlx"
SYSTEMD_SERVICE_PATH = Path("/etc/systemd/system") / f"{SYSTEMD_SERVICE_NAME}.service"


def iso_now(timestamp: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp or time.time()))


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        if not DEFAULT_CONFIG_PATH.exists():
            raise SystemExit("Arquivo de configuracao nao encontrado.")
        return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=True), encoding="utf-8")


def resolve_terminal_working_directory(config: dict[str, Any]) -> str:
    configured_path = str(config.get("terminal_working_directory") or "").strip()
    candidates: list[str] = []

    if configured_path:
        candidates.append(configured_path)

    home_dir = os.path.expanduser("~")
    if os.geteuid() == 0:
        candidates.append("/root")
    candidates.append(home_dir)
    candidates.append(str(ROOT))

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate

    return str(ROOT)


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def remove_pid_file() -> None:
    try:
        if PID_PATH.exists():
            PID_PATH.unlink()
    except OSError:
        pass


def ensure_single_instance() -> None:
    existing_pid = read_pid()
    if existing_pid and process_is_running(existing_pid):
        raise SystemExit(f"Agent ja esta em execucao com PID {existing_pid}.")
    if existing_pid:
        remove_pid_file()
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def build_systemd_unit() -> str:
    python_bin = Path(sys.executable).resolve()
    agent_script = (ROOT / "agent.py").resolve()
    working_dir = ROOT.resolve()
    return f"""[Unit]
Description=AgentLX Linux Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={python_bin} {agent_script} run-foreground
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


def run_process(
    args: list[str],
    timeout: int = 15,
    capture_output: bool = True,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    stdout_pipe: int | None = subprocess.PIPE if capture_output else None
    stderr_pipe: int | None = subprocess.PIPE if capture_output else None
    stdin_pipe: int | None = subprocess.PIPE if input_text is not None else None

    process = subprocess.Popen(
        args,
        stdin=stdin_pipe,
        stdout=stdout_pipe,
        stderr=stderr_pipe,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            stdout, stderr = process.communicate()
        return 124, (stdout or "").strip(), f"Command timed out after {timeout}s"
    except Exception as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait(timeout=1)
        return 1, "", str(exc)

    return process.returncode, (stdout or "").strip(), (stderr or "").strip()


def run_shell_command(command: str, timeout: int = 15) -> tuple[int, str, str]:
    shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return run_process([shell, "-lc", command], timeout=timeout)


def run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    if not shutil.which("systemctl"):
        raise SystemExit("systemctl nao encontrado. Este recurso exige systemd.")
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def install_systemd_service() -> None:
    if not is_linux():
        raise SystemExit("A instalacao do servico e suportada apenas em Linux.")
    if os.geteuid() != 0:
        raise SystemExit("Execute 'python agent.py install-service' com sudo ou como root.")

    SYSTEMD_SERVICE_PATH.write_text(build_systemd_unit(), encoding="utf-8")

    for args in (
        ("daemon-reload",),
        ("enable", SYSTEMD_SERVICE_NAME),
        ("restart", SYSTEMD_SERVICE_NAME),
    ):
        result = run_systemctl(*args)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or result.stdout.strip() or "Falha no systemctl.")

    print(f"Servico {SYSTEMD_SERVICE_NAME} instalado em {SYSTEMD_SERVICE_PATH} e iniciado.")


def uninstall_systemd_service() -> None:
    if not is_linux():
        raise SystemExit("A remocao do servico e suportada apenas em Linux.")
    if os.geteuid() != 0:
        raise SystemExit("Execute 'python agent.py uninstall-service' com sudo ou como root.")

    if shutil.which("systemctl"):
        run_systemctl("disable", "--now", SYSTEMD_SERVICE_NAME)

    if SYSTEMD_SERVICE_PATH.exists():
        SYSTEMD_SERVICE_PATH.unlink()

    if shutil.which("systemctl"):
        run_systemctl("daemon-reload")

    print(f"Servico {SYSTEMD_SERVICE_NAME} removido.")


def print_status() -> None:
    pid = read_pid()
    if pid and process_is_running(pid):
        print(f"Agent em execucao com PID {pid}.")
    else:
        print("Agent nao esta em execucao em background.")

    if shutil.which("systemctl") and SYSTEMD_SERVICE_PATH.exists():
        result = run_systemctl("is-active", SYSTEMD_SERVICE_NAME)
        service_state = result.stdout.strip() or "unknown"
        print(f"Servico systemd: {service_state}")


def stop_background_agent() -> None:
    if shutil.which("systemctl") and SYSTEMD_SERVICE_PATH.exists():
        result = run_systemctl("stop", SYSTEMD_SERVICE_NAME)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or result.stdout.strip() or "Falha ao parar o servico.")
        print(f"Servico {SYSTEMD_SERVICE_NAME} parado.")
        return

    pid = read_pid()
    if not pid:
        raise SystemExit("Nenhum PID local encontrado para o agent.")
    if not process_is_running(pid):
        remove_pid_file()
        raise SystemExit("O PID salvo nao esta mais ativo.")

    os.kill(pid, 15)
    for _ in range(20):
        if not process_is_running(pid):
            remove_pid_file()
            print(f"Agent com PID {pid} finalizado.")
            return
        time.sleep(0.25)

    raise SystemExit(f"Nao foi possivel finalizar o agent com PID {pid}.")


def start_background_agent() -> None:
    if websockets is None:
        raise SystemExit(
            "Dependencia ausente: instale o pacote 'websockets' com "
            "'pip3 install -r requirements.txt' antes de iniciar o agent."
        )

    if shutil.which("systemctl") and SYSTEMD_SERVICE_PATH.exists():
        result = run_systemctl("restart", SYSTEMD_SERVICE_NAME)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or result.stdout.strip() or "Falha ao iniciar o servico.")
        print(f"Servico {SYSTEMD_SERVICE_NAME} iniciado/reiniciado em background.")
        return

    existing_pid = read_pid()
    if existing_pid and process_is_running(existing_pid):
        raise SystemExit(f"Agent ja esta em execucao com PID {existing_pid}.")
    if existing_pid:
        remove_pid_file()

    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, str((ROOT / "agent.py").resolve()), "run-foreground"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(f"Agent iniciado em background com PID {process.pid}. Logs: {LOG_PATH}")


def parse_os_release() -> dict[str, str]:
    candidates = [Path("/etc/os-release"), Path("/usr/lib/os-release")]
    for path in candidates:
        if not path.exists():
            continue
        data: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"').strip("'")
        if data:
            return data
    return {}


def normalize_distro_id(raw_id: str, raw_name: str, raw_pretty_name: str) -> str:
    base = (raw_id or "").strip().lower().replace("_", "-")
    name_blob = f"{raw_name} {raw_pretty_name}".lower()
    if base == "centos" and "stream" in name_blob:
        return "centos-stream"
    if base in {"redhat", "red-hat-enterprise-linux"}:
        return "rhel"
    if base == "rocky linux":
        return "rocky"
    if base == "alma linux":
        return "almalinux"
    return base or "linux"


def distro_family_for(distro_id: str, like_ids: list[str]) -> str:
    debian_like = {"debian", "ubuntu", "linuxmint", "raspbian", "pop", "neon", "elementary"}
    redhat_like = {
        "rhel",
        "redhat",
        "centos",
        "centos-stream",
        "fedora",
        "rocky",
        "almalinux",
        "cloudlinux",
        "ol",
        "amzn",
        "amazon",
    }
    gentoo_like = {"gentoo"}
    suse_like = {"opensuse", "opensuse-leap", "sles"}
    arch_like = {"arch", "manjaro"}
    alpine_like = {"alpine"}

    tokens = {distro_id, *like_ids}
    if tokens & debian_like:
        return "debian"
    if tokens & redhat_like:
        return "redhat"
    if tokens & gentoo_like:
        return "gentoo"
    if tokens & suse_like:
        return "suse"
    if tokens & arch_like:
        return "arch"
    if tokens & alpine_like:
        return "alpine"
    return "linux"


def read_distribution() -> dict[str, Any]:
    os_release = parse_os_release()
    pretty_name = os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Linux"
    name = os_release.get("NAME") or pretty_name
    raw_id = os_release.get("ID", "")
    like_ids = [
        item.strip().lower().replace("_", "-")
        for item in os_release.get("ID_LIKE", "").split()
        if item.strip()
    ]
    distro_id = normalize_distro_id(raw_id, name, pretty_name)
    return {
        "id": distro_id,
        "family": distro_family_for(distro_id, like_ids),
        "version": os_release.get("VERSION_ID", ""),
        "name": name,
        "prettyName": pretty_name,
        "like": like_ids,
    }


def get_ip_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        hostname = socket.gethostname()
        try:
            return socket.gethostbyname(hostname)
        except OSError:
            return "127.0.0.1"


def read_uptime_seconds() -> int:
    uptime_path = Path("/proc/uptime")
    if uptime_path.exists():
        raw = uptime_path.read_text(encoding="utf-8").split()[0]
        return int(float(raw))
    return 0


def read_memory() -> tuple[float, float]:
    meminfo_path = Path("/proc/meminfo")
    if meminfo_path.exists():
        data: dict[str, int] = {}
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            data[key.strip()] = int(value.strip().split()[0])
        total = data.get("MemTotal", 0) / 1024 / 1024
        available = data.get("MemAvailable", 0) / 1024 / 1024
        used = max(total - available, 0)
        return round(used, 2), round(total, 2)
    return 0.0, 0.0


def read_cpu_times() -> tuple[int, int] | None:
    stat_path = Path("/proc/stat")
    if not stat_path.exists():
        return None

    first_line = stat_path.read_text(encoding="utf-8").splitlines()[0]
    parts = first_line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None

    values = [int(value) for value in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def compute_cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    busy = total_delta - idle_delta
    return round(clamp((busy / total_delta) * 100, 0.0, 100.0), 2)


def read_disk_percent() -> float:
    try:
        stats = os.statvfs("/")
    except OSError:
        return 0.0

    total_blocks = stats.f_blocks
    available_blocks = stats.f_bavail
    if total_blocks <= 0:
        return 0.0

    used_blocks = total_blocks - available_blocks
    return round(clamp((used_blocks / total_blocks) * 100, 0.0, 100.0), 2)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def process_exit_code(args: list[str], timeout: int = 5) -> int:
    code, _, _ = run_process(args, timeout=timeout, capture_output=False)
    return code


def list_systemd_units() -> str:
    if not command_exists("systemctl"):
        return ""
    code, stdout, _ = run_process(
        ["systemctl", "list-units", "--type=service", "--plain", "--no-legend", "--no-pager"],
        timeout=5,
    )
    if code != 0:
        return ""
    return stdout.lower()


def detect_services() -> list[dict[str, str]]:
    services: dict[str, dict[str, str]] = {
        "system": {"slug": "system", "displayName": "System", "detectedBy": "agent"},
    }

    def add(slug: str, display_name: str) -> None:
        services[slug] = {"slug": slug, "displayName": display_name, "detectedBy": "agent"}

    systemctl_checks = {
        "nginx": "Nginx",
        "mariadb": "MariaDB",
        "redis": "Redis",
        "postfix": "Postfix",
    }
    if command_exists("systemctl"):
        for slug, display_name in systemctl_checks.items():
            if process_exit_code(["systemctl", "is-active", "--quiet", slug], timeout=5) == 0:
                add(slug, display_name)

    if Path("/opt/zextras").is_dir() or Path("/opt/carbonio").is_dir() or command_exists("zmcontrol"):
        add("carbonio", "Carbonio")
    else:
        units = list_systemd_units()
        if "carbonio" in units or "zextras" in units:
            add("carbonio", "Carbonio")

    return list(services.values())


class SnapshotCollector:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.hostname = socket.gethostname()
        self.kernel = platform.release()
        self.arch = platform.machine()
        self.location = config.get("location", "")
        self.inventory_refresh_interval_sec = max(
            60,
            int(config.get("inventory_refresh_interval_sec", config.get("service_refresh_interval_sec", 300))),
        )
        self._distribution = read_distribution()
        self._cpu_times: tuple[int, int] | None = None
        self._inventory_cache: dict[str, Any] | None = None
        self._inventory_refreshed_at = 0.0

    def _refresh_inventory(self) -> dict[str, Any]:
        inventory = {
            "distribution": read_distribution(),
            "services": detect_services(),
            "kernel": platform.release(),
            "arch": platform.machine(),
        }
        self._distribution = inventory["distribution"]
        self.kernel = inventory["kernel"]
        self.arch = inventory["arch"]
        self._inventory_cache = inventory
        self._inventory_refreshed_at = time.time()
        return inventory

    def _get_inventory(self, force_refresh: bool = False) -> tuple[dict[str, Any], bool]:
        now = time.time()
        if (
            force_refresh
            or self._inventory_cache is None
            or now - self._inventory_refreshed_at >= self.inventory_refresh_interval_sec
        ):
            return self._refresh_inventory(), True
        return self._inventory_cache, False

    def _read_cpu_percent(self) -> float:
        current = read_cpu_times()
        if current is None:
            return 0.0

        previous = self._cpu_times
        self._cpu_times = current
        if previous is None:
            time.sleep(0.1)
            second = read_cpu_times()
            if second is None:
                return 0.0
            self._cpu_times = second
            return compute_cpu_percent(current, second)

        return compute_cpu_percent(previous, current)

    def collect_snapshot(self, force_inventory_refresh: bool = False) -> tuple[dict[str, Any], bool]:
        inventory, inventory_refreshed = self._get_inventory(force_refresh=force_inventory_refresh)
        ram_used_gb, ram_total_gb = read_memory()
        distribution = inventory["distribution"]
        snapshot = {
            "hostname": self.hostname,
            "ip": get_ip_address(),
            "os": distribution["prettyName"],
            "distribution": distribution,
            "kernel": inventory["kernel"],
            "arch": inventory["arch"],
            "location": self.location,
            "uptimeSec": read_uptime_seconds(),
            "cpuPercent": self._read_cpu_percent(),
            "ramUsedGb": ram_used_gb,
            "ramTotalGb": ram_total_gb if ram_total_gb > 0 else 1.0,
            "diskPercent": read_disk_percent(),
            "services": inventory["services"],
            "collectedAt": iso_now(),
        }
        return snapshot, inventory_refreshed


def api_request(
    config: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    use_agent_token: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = config["api_base_url"].rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if use_agent_token and config.get("agent_token"):
        headers["Authorization"] = f"Bearer {config['agent_token']}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Erro de conexao com a API: {exc}") from exc


def build_ws_url(config: dict[str, Any], path: str) -> str:
    base_url = config["api_base_url"].rstrip("/")
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + path
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + path
    return base_url + path


class RealtimeTunnelClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        batch_ms = int(config.get("terminal_output_batch_ms", 16))
        self.output_batch_window_sec = max(0.005, batch_ms / 1000)

    def _require_websockets(self) -> None:
        if websockets is None:
            raise RuntimeError(
                "Dependencia ausente: instale o pacote 'websockets' com "
                "'pip3 install -r requirements.txt' antes de iniciar o loop do agent."
            )

    def start(self) -> None:
        self._require_websockets()
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="agentlx-realtime-tunnel", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: None)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            if not self.config.get("agent_token"):
                await asyncio.sleep(5)
                continue

            try:
                await self._connect_and_serve()
            except Exception as exc:
                print(f"[agent][tunnel] erro: {exc}", file=sys.stderr)
            await asyncio.sleep(3)

    async def _connect_and_serve(self) -> None:
        ws_url = build_ws_url(self.config, "/api/agent/tunnel")
        headers = {"Authorization": f"Bearer {self.config['agent_token']}"}
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
            compression=None,
        ) as websocket:
            print(f"[agent][tunnel] conectado em {ws_url}")
            async for raw_message in websocket:
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(websocket, payload)

    async def _handle_message(
        self,
        websocket: WebSocketConnection,
        payload: dict[str, Any],
    ) -> None:
        message_type = payload.get("type")
        session_id = payload.get("sessionId", "")

        if message_type == "agent.ready":
            return

        if message_type == "terminal.open":
            await self._open_terminal(
                websocket,
                session_id,
                int(payload.get("cols", 120)),
                int(payload.get("rows", 30)),
            )
            return

        if message_type == "terminal.input":
            await self._write_terminal(session_id, str(payload.get("data", "")))
            return

        if message_type == "terminal.resize":
            await self._resize_terminal(
                session_id,
                int(payload.get("cols", 120)),
                int(payload.get("rows", 30)),
            )
            return

        if message_type == "terminal.close":
            await self._close_terminal(websocket, session_id, notify=True)

    async def _send_json(
        self,
        websocket: WebSocketConnection,
        payload: dict[str, Any],
    ) -> None:
        await websocket.send(json.dumps(payload))

    def _set_terminal_size(self, master_fd: int, cols: int, rows: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    async def _flush_terminal_output(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return

        session["flush_handle"] = None
        pending_output = session["pending_output"]
        if not pending_output:
            return

        payload = "".join(pending_output)
        pending_output.clear()
        await self._send_json(
            session["websocket"],
            {"type": "terminal.output", "sessionId": session_id, "data": payload},
        )

    def _schedule_terminal_flush(self, session_id: str) -> None:
        if not self.loop:
            return
        session = self.sessions.get(session_id)
        if not session or session.get("flush_handle") is not None:
            return

        session["flush_handle"] = self.loop.call_later(
            self.output_batch_window_sec,
            lambda: asyncio.create_task(self._flush_terminal_output(session_id)),
        )

    async def _open_terminal(
        self,
        websocket: WebSocketConnection,
        session_id: str,
        cols: int,
        rows: int,
    ) -> None:
        if not session_id:
            return

        if session_id in self.sessions:
            await self._send_json(
                websocket,
                {"type": "terminal.error", "sessionId": session_id, "message": "Sessao ja aberta."},
            )
            return

        shell = shutil.which("bash") or os.environ.get("SHELL") or "/bin/sh"
        pid, master_fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            try:
                os.chdir(resolve_terminal_working_directory(self.config))
            except OSError:
                pass
            os.execv(shell, [shell, "-i"])

        os.set_blocking(master_fd, False)
        self._set_terminal_size(master_fd, cols, rows)
        loop = self.loop
        if not loop:
            return

        session = {
            "pid": pid,
            "master_fd": master_fd,
            "websocket": websocket,
            "cols": cols,
            "rows": rows,
            "closed": False,
            "pending_output": [],
            "flush_handle": None,
        }
        self.sessions[session_id] = session

        def handle_readable() -> None:
            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    data = b""
                else:
                    loop.create_task(
                        self._send_json(
                            websocket,
                            {
                                "type": "terminal.error",
                                "sessionId": session_id,
                                "message": str(exc),
                            },
                        )
                    )
                    return

            if not data:
                loop.create_task(self._close_terminal(websocket, session_id, notify=True))
                return

            text = data.decode("utf-8", errors="replace")
            session["pending_output"].append(text)
            self._schedule_terminal_flush(session_id)

        loop.add_reader(master_fd, handle_readable)
        await self._send_json(websocket, {"type": "terminal.opened", "sessionId": session_id})

    async def _write_terminal(self, session_id: str, data: str) -> None:
        session = self.sessions.get(session_id)
        if not session or session.get("closed") or not data:
            return
        os.write(session["master_fd"], data.encode("utf-8", errors="ignore"))

    async def _resize_terminal(self, session_id: str, cols: int, rows: int) -> None:
        session = self.sessions.get(session_id)
        if not session or session.get("closed"):
            return
        session["cols"] = cols
        session["rows"] = rows
        self._set_terminal_size(session["master_fd"], cols, rows)

    async def _close_terminal(
        self,
        websocket: WebSocketConnection,
        session_id: str,
        notify: bool,
    ) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return

        if session.get("closed"):
            self.sessions.pop(session_id, None)
            return

        session["closed"] = True
        flush_handle = session.get("flush_handle")
        if flush_handle is not None:
            flush_handle.cancel()
            session["flush_handle"] = None

        if self.loop:
            try:
                self.loop.remove_reader(session["master_fd"])
            except Exception:
                pass

        await self._flush_terminal_output(session_id)

        exit_code: int | None = None
        try:
            os.close(session["master_fd"])
        except OSError:
            pass

        pid = session["pid"]
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(pid, 15)
                await asyncio.sleep(0.1)
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(pid, 9)
                waited_pid, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                exit_code = 128 + os.WTERMSIG(status)
        except ChildProcessError:
            exit_code = 0
        except OSError:
            exit_code = None

        self.sessions.pop(session_id, None)
        if notify:
            await self._send_json(
                websocket,
                {"type": "terminal.closed", "sessionId": session_id, "exitCode": exit_code},
            )


def register_agent(config: dict[str, Any], collector: SnapshotCollector) -> None:
    snapshot, _ = collector.collect_snapshot(force_inventory_refresh=True)
    payload = {
        "agentId": config.get("agent_id") or None,
        "agentName": config.get("agent_name", collector.hostname),
        "machineId": config.get("machine_id") or None,
        "agentVersion": config.get("agent_version", "agentlx-linux-mvp"),
        "pollIntervalSec": config.get("poll_interval_sec", 30),
        "snapshot": snapshot,
    }
    response = api_request(
        config,
        "POST",
        "/api/agent/register",
        payload,
        extra_headers={"x-agent-enrollment-token": config["enrollment_token"]},
    )
    config["agent_token"] = response["agentToken"]
    config["machine_id"] = response["machineId"]
    config["agent_id"] = response["agentId"]
    config["poll_interval_sec"] = response["pollIntervalSec"]
    save_config(config)
    print(f"Agent registrado com machine_id={config['machine_id']} e agent_id={config['agent_id']}")


def send_heartbeat(config: dict[str, Any], collector: SnapshotCollector) -> dict[str, Any]:
    snapshot, inventory_refreshed = collector.collect_snapshot()
    payload = {
        "agentVersion": config.get("agent_version", "agentlx-linux-mvp"),
        "snapshot": snapshot,
        "lastHeartbeatAt": iso_now(),
        "includeInventory": inventory_refreshed,
    }
    return api_request(config, "POST", "/api/agent/heartbeat", payload, use_agent_token=True)


def poll_executions(config: dict[str, Any]) -> list[dict[str, Any]]:
    response = api_request(
        config,
        "POST",
        "/api/agent/poll",
        {"limit": 3},
        use_agent_token=True,
    )
    return response.get("executions", [])


def send_execution_result(config: dict[str, Any], execution: dict[str, Any], started_at: float) -> None:
    start_time = iso_now(started_at)
    code, stdout, stderr = run_shell_command(
        execution["command"],
        timeout=int(execution.get("timeoutSec", 120)),
    )
    finished_at = time.time()
    payload = {
        "executionId": execution["executionId"],
        "status": "success" if code == 0 else "failed",
        "output": stdout,
        "errorOutput": stderr,
        "exitCode": code,
        "durationMs": int((finished_at - started_at) * 1000),
        "startedAt": start_time,
        "finishedAt": iso_now(finished_at),
    }
    api_request(config, "POST", "/api/agent/executions/result", payload, use_agent_token=True)


def run_loop(config: dict[str, Any], collector: SnapshotCollector) -> None:
    if not config.get("agent_token"):
        raise SystemExit("Agent ainda nao registrado. Execute 'python agent.py register' primeiro.")

    interval = int(config.get("poll_interval_sec", 30))
    tunnel = RealtimeTunnelClient(config)
    tunnel.start()
    print(f"Loop iniciado com intervalo de {interval}s")
    try:
        while True:
            try:
                heartbeat = send_heartbeat(config, collector)
                print(
                    f"Heartbeat enviado: machine={heartbeat.get('machineId')} "
                    f"status={heartbeat.get('status')} pending={heartbeat.get('pendingExecutions')}"
                )
                executions = poll_executions(config)
                for execution in executions:
                    print(f"Executando template {execution['templateId']} em {execution['machineId']}")
                    send_execution_result(config, execution, time.time())
            except Exception as exc:
                print(f"[agent] erro: {exc}", file=sys.stderr)
            time.sleep(interval)
    finally:
        tunnel.stop()


def run_foreground(config: dict[str, Any]) -> None:
    ensure_single_instance()
    collector = SnapshotCollector(config)
    try:
        run_loop(config, collector)
    finally:
        remove_pid_file()


def main() -> None:
    config = load_config()
    collector = SnapshotCollector(config)
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "register":
        register_agent(config, collector)
        return
    if command == "once":
        print(json.dumps(send_heartbeat(config, collector), indent=2))
        return
    if command == "run":
        start_background_agent()
        return
    if command == "run-foreground":
        run_foreground(config)
        return
    if command == "stop":
        stop_background_agent()
        return
    if command == "status":
        print_status()
        return
    if command == "install-service":
        install_systemd_service()
        return
    if command == "uninstall-service":
        uninstall_systemd_service()
        return
    raise SystemExit(f"Comando invalido: {command}")


if __name__ == "__main__":
    main()

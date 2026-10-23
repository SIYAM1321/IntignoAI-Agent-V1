from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid

import psutil
from collections import deque
from pathlib import Path
from typing import Any, Callable


DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b", r"\brm\s+-fr\b", r"\bdel\s+/[a-z]*s\b",
    r"\bformat(?:\.com)?\b", r"\bshutdown(?:\.exe)?\b", r"\brestart-computer\b",
    r"\bstop-computer\b", r"\breboot\b", r"\bpoweroff\b", r"\bdiskpart(?:\s|$)",
    r"\bformat-volume\b", r"\bclear-disk\b", r"\binitialize-disk\b",
    r"\bgit\s+clean\s+-[^\n]*f", r"\bgit\s+reset\s+--hard\b",
    r"\bfind\s+[^\n]*-delete\b", r"\bremove-item\b[^\n]*(?:-recurse|-force)",
    r"\bchown\s+-R\b", r"\bchmod\s+-R\b",
]


def command_requires_confirmation(command: str) -> bool:
    text = str(command or "").strip()
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in DANGEROUS_PATTERNS)


def default_shell() -> str:
    configured = os.getenv("TERMINAL_SHELL", "auto").strip().lower()
    if configured != "auto":
        return configured
    if os.name == "nt":
        # Prefer PowerShell for rich Windows commands; pwsh 7 if installed.
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


def shell_info() -> dict[str, Any]:
    shell = default_shell()
    if shell in {"powershell", "pwsh"}:
        shell_path = shutil.which(shell) or shutil.which("powershell.exe")
    elif shell == "cmd":
        shell_path = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    else:
        shell_path = shutil.which(shell) or shell
    return {"platform": platform.system(), "shell": shell, "shell_path": shell_path}


def _build_argv(command: str) -> tuple[list[str], str]:
    shell = default_shell()
    if shell in {"powershell", "pwsh"}:
        executable = shutil.which(shell) or shutil.which("powershell.exe")
        if not executable:
            raise FileNotFoundError("PowerShell was not found on this computer.")
        argv = [
            executable, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", command,
        ]
        return argv, shell
    if shell == "cmd":
        executable = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not executable:
            raise FileNotFoundError("cmd.exe was not found on this computer.")
        return [executable, "/d", "/s", "/c", command], shell
    executable = shutil.which(shell) or shell
    return [executable, "-lc", command], shell


def check_port(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> dict[str, Any]:
    port = int(port)
    host = str(host or "127.0.0.1")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    started = time.perf_counter()
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(float(timeout))
        try:
            result = sock.connect_ex((host, port))
        except OSError as exc:
            return {"host": host, "port": port, "open": False, "error": str(exc)}
    return {
        "host": host,
        "port": port,
        "open": result == 0,
        "status": "listening" if result == 0 else "closed_or_unreachable",
        "duration": round(time.perf_counter() - started, 4),
    }


class TerminalProcess:
    def __init__(self, process_id: str, command: str, cwd: str, process: subprocess.Popen, shell: str, started_at: float):
        self.id = process_id
        self.command = command
        self.cwd = cwd
        self.process = process
        self.shell = shell
        self.started_at = started_at
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.timed_out = False
        self.status = "running"
        self._stop_requested = False
        self._buffer = deque(maxlen=2000)
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._reader_threads: list[threading.Thread] = []
        self._monitor = threading.Thread(target=self._monitor_process, daemon=True)
        self._monitor.start()
        self._start_reader(self.process.stdout, "stdout")
        self._start_reader(self.process.stderr, "stderr")

    def _start_reader(self, stream, channel: str) -> None:
        if stream is None:
            return
        thread = threading.Thread(target=self._reader, args=(stream, channel), daemon=True)
        self._reader_threads.append(thread)
        thread.start()

    def _reader(self, stream, channel: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\r\n")
                with self._lock:
                    self._buffer.append({"channel": channel, "text": text, "at": time.time()})
                # Notify listeners immediately so the UI can show live stdout/stderr
                # while a server, watcher or automation script is still running.
                self._notify()
        except Exception as exc:
            with self._lock:
                self._buffer.append({"channel": "stderr", "text": f"[reader error] {exc}", "at": time.time()})
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _monitor_process(self) -> None:
        self.process.wait()
        # Give stdout/stderr reader threads a moment to drain their final lines so
        # the completion notification contains the actual final terminal output.
        for thread in list(self._reader_threads):
            thread.join(timeout=0.5)
        with self._lock:
            self.exit_code = self.process.returncode
            self.ended_at = time.time()
            if self._stop_requested:
                self.status = "stopped"
            else:
                self.status = "success" if self.exit_code == 0 else "error"
            self._done.set()
        self._notify()

    def _notify(self) -> None:
        info = self.to_dict(output_chars=10000)
        for callback in list(self._callbacks):
            try:
                callback(info)
            except Exception:
                pass

    def add_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)
            done = self._done.is_set()
        if done:
            callback(self.to_dict(output_chars=10000))

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def stop(self) -> dict[str, Any]:
        if self._done.is_set():
            return self.to_dict()
        with self._lock:
            self._stop_requested = True
        try:
            if os.name == "nt":
                # Kill the whole Windows process tree so child Python/Node servers do not survive the shell.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, text=True, timeout=8)
            else:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            if not self._done.wait(2.0):
                self.process.kill()
        except (ProcessLookupError, FileNotFoundError):
            pass
        return self.to_dict()

    def to_dict(self, output_chars: int = 12000) -> dict[str, Any]:
        with self._lock:
            lines = list(self._buffer)
            text = "\n".join(item["text"] for item in lines)
            if len(text) > output_chars:
                text = text[-output_chars:]
            ended = self.ended_at
            end_for_elapsed = ended if ended is not None else time.time()
            return {
                "process_id": self.id,
                "pid": self.process.pid,
                "command": self.command,
                "working_directory": self.cwd,
                "shell": self.shell,
                "status": self.status,
                "running": self.status == "running",
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "ended_at": ended,
                "duration": round(max(0.0, end_for_elapsed - self.started_at), 3),
                "output": text,
                "lines": lines[-300:],
            }


class TerminalManager:
    def __init__(self) -> None:
        self._items: dict[str, TerminalProcess] = {}
        self._lock = threading.RLock()

    def start(self, command: str, cwd: str, wait_seconds: float = 2.0, *, background: bool = False) -> dict[str, Any]:
        if not str(command or "").strip():
            raise ValueError("Command is required.")
        path = Path(cwd).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {path}")
        argv, shell = _build_argv(command)
        creationflags = 0
        preexec_fn = None
        if os.name == "nt":
            # Detached from console while remaining manageable by TerminateProcess.
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            preexec_fn = os.setsid

        started = time.time()
        proc = subprocess.Popen(
            argv,
            cwd=str(path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            bufsize=1,
        )
        item = TerminalProcess(uuid.uuid4().hex[:12], command, str(path), proc, shell, started)
        with self._lock:
            self._items[item.id] = item
        if background:
            return item.to_dict()
        try:
            item.wait(max(0.0, min(float(wait_seconds), 12.0)))
        except Exception:
            pass
        return item.to_dict()

    def get(self, process_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(str(process_id))
        if not item:
            raise KeyError(f"Unknown terminal process: {process_id}")
        return item.to_dict()

    def wait(self, process_id: str, timeout: float = 120.0) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(str(process_id))
        if not item:
            raise KeyError(f"Unknown terminal process: {process_id}")
        item.wait(max(0.0, min(float(timeout), 600.0)))
        return item.to_dict()

    def stop(self, process_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._items.get(str(process_id))
        if not item:
            raise KeyError(f"Unknown terminal process: {process_id}")
        return item.stop()

    def list(self, include_finished: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._items.values())
        if not include_finished:
            values = [item for item in values if item.status == "running"]
        values.sort(key=lambda item: item.started_at, reverse=True)
        return [item.to_dict(output_chars=4000) for item in values[:100]]

    def attach_activity(self, process_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            item = self._items.get(str(process_id))
        if item:
            item.add_callback(callback)


TERMINAL_MANAGER = TerminalManager()


def run_terminal(
    command: str,
    working_directory: str,
    timeout_seconds: int = 120,
    *,
    background: bool = False,
    wait_seconds: float = 2.0,
) -> dict[str, Any]:
    """Start a local command.

    Commands are never killed merely because the initial wait window expires.
    Long-running commands remain alive under TerminalManager and are queryable with
    get_terminal_status / list_terminals / wait_terminal / stop_terminal.
    """
    if not command or not command.strip():
        raise ValueError("Command is required.")
    result = TERMINAL_MANAGER.start(command, working_directory, wait_seconds, background=background)
    running = bool(result.get("running"))
    exit_code = result.get("exit_code")
    # A command that exits with nonzero status is a failed command even if the
    # shell launched successfully. Long-running commands are accepted as active.
    result["ok"] = running or exit_code == 0
    result["mode"] = "background" if background and running else ("completed" if not running else "detached_after_wait")
    if not running and exit_code not in (None, 0):
        result["error"] = f"Command exited with code {exit_code}. Review stdout/stderr for the cause."
    return result


_PROTECTED_PROCESS_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe",
    "fontdrvhost.exe", "dwm.exe", "memory compression",
}


def _windows_netstat_listener_pids(port: int) -> set[int]:
    """Use Windows netstat as a fallback when psutil cannot see every PID."""
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, errors="replace", timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        local_address = fields[1].strip()
        try:
            local_port = int(local_address.rsplit(":", 1)[-1])
            pid = int(fields[4])
        except (TypeError, ValueError):
            continue
        if local_port == int(port) and pid > 0:
            pids.add(pid)
    return pids


def kill_port(port: int, host: str = "127.0.0.1", wait_seconds: float = 3.0) -> dict[str, Any]:
    """Safely stop processes listening on an exact local TCP port.

    This targets only PIDs reported as LISTEN on the requested port, never uses
    broad image/service filters, avoids protected Windows processes and refuses
    to terminate this agent's own process. Caller/UI must require confirmation.
    """
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    host = str(host or "127.0.0.1")
    inspection_error = None
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError) as exc:
        connections = []
        inspection_error = str(exc)

    pid_set: set[int] = set()
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        try:
            if int(conn.laddr.port) == port and conn.pid is not None:
                pid_set.add(int(conn.pid))
        except (AttributeError, TypeError, ValueError):
            continue

    if os.name == "nt":
        # `netstat -ano` can still resolve exact listener PIDs when Windows hides
        # some socket ownership details from a non-elevated psutil call.
        pid_set.update(_windows_netstat_listener_pids(port))

    if not pid_set:
        verification = check_port(port, host)
        error = f" Cannot inspect process ownership: {inspection_error}" if inspection_error else ""
        return {
            "ok": not verification.get("open", False), "port": port, "host": host,
            "message": ("No process was reported listening on this exact TCP port." if not verification.get("open") else "The port responds locally, but its listener PID could not be identified; nothing was terminated.") + error,
            "terminated": [], "blocked": [], "verification": verification,
        }

    terminated: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    protected_pids = {0, 4, os.getpid()}
    for pid in sorted(pid_set):
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            record = {"pid": pid, "name": name}
            if pid in protected_pids or name.lower() in _PROTECTED_PROCESS_NAMES:
                blocked.append({**record, "reason": "Protected/system/agent process; not terminated."})
                continue
            if proc.status() == psutil.STATUS_ZOMBIE:
                terminated.append({**record, "result": "already_exited"})
                continue
            proc.terminate()
            try:
                proc.wait(timeout=max(0.1, min(float(wait_seconds), 10.0)))
                terminated.append({**record, "result": "terminated"})
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
                terminated.append({**record, "result": "killed_after_timeout"})
        except psutil.NoSuchProcess:
            terminated.append({"pid": pid, "result": "already_exited"})
        except (psutil.AccessDenied, OSError, psutil.Error) as exc:
            blocked.append({"pid": pid, "reason": str(exc)})

    # Recheck after processes have exited to avoid claiming the port is freed
    # while an unrelated listener or protected service still owns it.
    time.sleep(0.15)
    verification = check_port(port, host)
    return {
        "ok": not verification.get("open") and not blocked,
        "port": port, "host": host, "terminated": terminated, "blocked": blocked,
        "verification": verification,
        "message": "Port is no longer responding on the requested host." if not verification.get("open") else "Port is still reachable; a remaining or unidentifiable listener may be using it.",
    }

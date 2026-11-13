#!/usr/bin/env python3
import os
import re
import sys
import glob
import json
import stat
import time
import shlex
import shutil
import socket
import asyncio
import secrets
import tempfile
import threading
import subprocess
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import (
    Header,
    Footer,
    Static,
    DataTable,
    RichLog,
    Label,
    Input,
    Button,
)
from textual.screen import ModalScreen
from textual import work, events

from rich.table import Table
from rich.markup import escape

CONFIG_PATH = os.path.expanduser("~/.config/aerotunnel/config.json")
DEFAULT_SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")

# Version Information
MAJOR = 0
MINOR = 2
PATCH = 5
VERSION = f"{MAJOR}.{MINOR}.{PATCH}"
DATE_RELEASED = "2026-11-11"

# Timing & Notification Constants
STABILITY_WAIT_SEC = 6.0
RECONNECT_DELAY_SEC = 5.0
WIDE_TABLE_MIN_WIDTH = 84
MOBILE_SCREEN_MAX_WIDTH = 79


class ConfigValidationError(Exception):
    """Custom exception for configuration parsing and validation errors."""

    pass


def format_ssh_host(host: str) -> str:
    """Wrap bare IPv6 literals in brackets for OpenSSH -L/-R specs."""
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _is_valid_port(val) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and 1 <= val <= 65535


def is_ssh_password_prompt(text: str) -> bool:
    """Return True if a line/chunk looks like an interactive SSH credential prompt."""
    lower = text.strip().lower()
    if not lower:
        return False
    # Ignore failure/diagnostic lines that happen to mention auth keywords
    if any(
        err in lower
        for err in (
            "permission denied",
            "authentication failed",
            "connection closed",
            "address already in use",
        )
    ):
        return False
    if re.search(r"\b(password|passphrase)\b\s*:", lower):
        return True
    if re.search(r"\benter\s+(?:password|passphrase)\b.*:\s*$", lower):
        return True
    if re.search(r"\b(verification code|one-time password|otp|pin)\b\s*:\s*$", lower):
        return True
    return False


def detect_failed_forward_services(stderr_lines, active_services, services_config):
    """Identify which services in active_services failed to bind from SSH stderr lines."""
    failed = set()
    for line in stderr_lines:
        lower = line.lower()
        if "remote port forwarding failed" in lower:
            m = re.search(r"(?:listen\s+port|port)\s+(\d+)\b", line, re.IGNORECASE)
            if m:
                port = int(m.group(1))
                for srv in active_services:
                    conf = services_config[srv]
                    if (
                        conf.get("type", "local") == "remote"
                        and conf.get("remote_port") == port
                    ):
                        failed.add(srv)
        elif "address already in use" in lower or "cannot listen to port" in lower:
            m = re.search(r"(?:\]:|port:\s*)(\d+)\b", line, re.IGNORECASE)
            if m:
                port = int(m.group(1))
                matched_local = False
                for srv in active_services:
                    conf = services_config[srv]
                    if (
                        conf.get("type", "local") == "local"
                        and conf.get("local_port") == port
                    ):
                        failed.add(srv)
                        matched_local = True
                if not matched_local:
                    for srv in active_services:
                        conf = services_config[srv]
                        if (
                            conf.get("type", "local") == "remote"
                            and conf.get("remote_port") == port
                        ):
                            failed.add(srv)
    return failed


def is_proxy_auth_prompt(text: str) -> bool:
    """Return True if a line indicates ProxyCommand (e.g. cloudflared) is awaiting SSO/browser auth."""
    lower = text.strip().lower()
    if not lower:
        return False
    return any(
        phrase in lower
        for phrase in (
            "please open the following url",
            "waiting for login",
            "waiting for authentication",
            "authenticate in your browser",
            "a browser window should have opened",
        )
    )


def _strip_ssh_comment(line: str) -> str:
    in_quotes = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "#" and not in_quotes:
            return line[:idx]
    return line


def _iter_ssh_config_lines(filepath: str, visited=None):
    if visited is None:
        visited = set()
    real = os.path.realpath(os.path.expanduser(filepath))
    if real in visited or not os.path.isfile(real):
        return
    visited.add(real)
    base_dir = os.path.dirname(real)
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    for raw in lines:
        cleaned = _strip_ssh_comment(raw).strip()
        if not cleaned:
            continue
        parts = re.split(r"[\s=]+", cleaned, maxsplit=1)
        key = parts[0].lower()
        val = parts[1].strip() if len(parts) > 1 else ""
        if key == "include" and val:
            for pattern in shlex.split(val):
                expanded = os.path.expanduser(pattern)
                if not os.path.isabs(expanded):
                    expanded = os.path.join(base_dir, expanded)
                for match_path in sorted(glob.glob(expanded)):
                    yield from _iter_ssh_config_lines(match_path, visited)
        else:
            yield key, val


def list_ssh_config_hosts(ssh_config_path=None):
    """Parse concrete Host entries from ~/.ssh/config (or a custom SSH config file)."""
    cfg_path = (
        os.path.expanduser(ssh_config_path)
        if ssh_config_path
        else DEFAULT_SSH_CONFIG_PATH
    )
    if not os.path.isfile(cfg_path):
        return []

    hosts_by_alias = {}
    ordered_aliases = []
    current_aliases = []
    in_match_block = False

    for key, val in _iter_ssh_config_lines(cfg_path):
        if key == "host":
            in_match_block = False
            try:
                patterns = shlex.split(val)
            except ValueError:
                patterns = val.split()
            current_aliases = [
                p
                for p in patterns
                if p and not p.startswith("!") and "*" not in p and "?" not in p
            ]
            for alias in current_aliases:
                if alias not in hosts_by_alias:
                    hosts_by_alias[alias] = {
                        "host": alias,
                        "hostname": alias,
                        "user": "",
                        "port": 22,
                        "proxy_command": "",
                        "proxy_jump": "",
                        "identity_files": [],
                    }
                    ordered_aliases.append(alias)
        elif key == "match":
            in_match_block = True
            current_aliases = []
        elif not in_match_block and current_aliases:
            for alias in current_aliases:
                entry = hosts_by_alias[alias]
                if key == "hostname" and entry["hostname"] == alias:
                    entry["hostname"] = val
                elif key == "user" and not entry["user"]:
                    entry["user"] = val
                elif key == "port" and entry["port"] == 22:
                    try:
                        entry["port"] = int(val)
                    except ValueError:
                        pass
                elif key == "proxycommand" and not entry["proxy_command"]:
                    entry["proxy_command"] = val
                elif key == "proxyjump" and not entry["proxy_jump"]:
                    entry["proxy_jump"] = val
                elif key == "identityfile":
                    entry["identity_files"].append(val)

    return [hosts_by_alias[a] for a in ordered_aliases]


def resolve_ssh_target(
    target_host: str, user=None, port=None, ssh_config_path=None
) -> dict:
    """Resolve effective OpenSSH settings for target_host via `ssh -G`."""
    effective_cfg = (
        os.path.expanduser(ssh_config_path)
        if ssh_config_path
        else DEFAULT_SSH_CONFIG_PATH
    )
    known_hosts = {h["host"] for h in list_ssh_config_hosts(effective_cfg)}
    matched = target_host in known_hosts

    result = {
        "target": target_host,
        "user": user or "",
        "hostname": target_host,
        "port": port if port is not None else 22,
        "proxy_command": "",
        "proxy_jump": "",
        "identity_files": [],
        "matched_ssh_config": matched,
        "route_summary": "Direct SSH",
        "error": None,
        "ssh_config_path": effective_cfg,
        "explicit_user": bool(user),
        "explicit_port": port is not None,
    }

    cmd = ["ssh", "-G"]
    if os.path.isfile(effective_cfg):
        cmd.extend(["-F", effective_cfg])
    elif ssh_config_path:
        result["error"] = f"SSH config file not found: {effective_cfg}"
        return result
    else:
        cmd.extend(["-F", "/dev/null"])

    if port is not None:
        cmd.extend(["-p", str(port)])
    target_spec = f"{user}@{target_host}" if user else target_host
    cmd.append(target_spec)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
    except FileNotFoundError:
        return result
    except Exception as e:
        result["error"] = str(e)
        return result

    if proc.returncode != 0:
        err_text = (proc.stderr or proc.stdout or "").strip()
        result["error"] = err_text or f"ssh -G exited with code {proc.returncode}"
        return result

    resolved_identities = []
    for line in proc.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        k, v = parts[0].strip().lower(), parts[1].strip()
        if k == "user" and not result["explicit_user"]:
            result["user"] = v
        elif k == "hostname":
            result["hostname"] = v
        elif k == "port" and not result["explicit_port"]:
            try:
                result["port"] = int(v)
            except ValueError:
                pass
        elif k == "proxycommand" and v.lower() != "none":
            result["proxy_command"] = v
        elif k == "proxyjump" and v.lower() != "none":
            result["proxy_jump"] = v
        elif k == "identityfile":
            resolved_identities.append(v)

    result["identity_files"] = resolved_identities
    if (
        result["proxy_command"]
        or result["proxy_jump"]
        or result["hostname"] != target_host
        or (not result["explicit_port"] and result["port"] != 22)
    ):
        result["matched_ssh_config"] = True

    if result["proxy_command"]:
        proxy_short = result["proxy_command"]
        if len(proxy_short) > 42:
            proxy_short = f"{proxy_short[:39]}..."
        result["route_summary"] = f"ProxyCommand: {proxy_short}"
    elif result["proxy_jump"]:
        result["route_summary"] = f"ProxyJump: {result['proxy_jump']}"

    return result


def get_ssh_target(config: dict) -> str:
    return config.get("remote_host") or config.get("ssh_host") or ""


def build_ssh_connection_args(config: dict) -> list[str]:
    """Build target, port (-p only if set), and -F arguments preserving ~/.ssh/config."""
    args = []
    ssh_cfg = config.get("ssh_config")
    if ssh_cfg:
        args.extend(["-F", os.path.expanduser(ssh_cfg)])
    elif DEFAULT_SSH_CONFIG_PATH != os.path.expanduser(
        "~/.ssh/config"
    ) and os.path.isfile(DEFAULT_SSH_CONFIG_PATH):
        args.extend(["-F", DEFAULT_SSH_CONFIG_PATH])
    if config.get("remote_port") is not None:
        args.extend(["-p", str(config["remote_port"])])
    target = get_ssh_target(config)
    user = config.get("remote_user")
    args.append(f"{user}@{target}" if user else target)
    return args


def build_ssh_tunnel_command(
    config: dict,
    active_services: list[str],
    service_bindings: dict,
    include_verbose: bool = True,
) -> list[str]:
    """Build the full `ssh` command line for active -L / -R port forwards."""
    cmd = [
        "ssh",
        "-N",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if include_verbose:
        cmd.extend(["-o", "LogLevel=VERBOSE"])

    ssh_cfg = config.get("ssh_config")
    if ssh_cfg:
        cmd.extend(["-F", os.path.expanduser(ssh_cfg)])
    elif DEFAULT_SSH_CONFIG_PATH != os.path.expanduser(
        "~/.ssh/config"
    ) and os.path.isfile(DEFAULT_SSH_CONFIG_PATH):
        cmd.extend(["-F", DEFAULT_SSH_CONFIG_PATH])

    if config.get("remote_port") is not None:
        cmd.extend(["-p", str(config["remote_port"])])

    services = config.get("services", {})
    for srv_name in active_services:
        srv_conf = services[srv_name]
        srv_type = srv_conf.get("type", "local")
        service_host = format_ssh_host(srv_conf.get("service_host", "127.0.0.1"))
        service_port = srv_conf["service_port"]
        bind_ip = format_ssh_host(
            service_bindings.get(srv_name, srv_conf.get("bind_address", "127.0.0.1"))
        )
        if srv_type == "remote":
            remote_port = srv_conf["remote_port"]
            cmd.extend(["-R", f"{bind_ip}:{remote_port}:{service_host}:{service_port}"])
        else:
            local_port = srv_conf["local_port"]
            cmd.extend(["-L", f"{bind_ip}:{local_port}:{service_host}:{service_port}"])

    target = get_ssh_target(config)
    user = config.get("remote_user")
    cmd.append(f"{user}@{target}" if user else target)
    return cmd


def classify_ssh_failure(
    stderr_lines, *, stderr_password_detected: bool = False, resolved_ssh=None
):
    """Map SSH stderr output into (status_label, user_error_msg, optional_hint)."""
    stderr_content = "\n".join(stderr_lines).lower()

    if (
        "bad configuration option" in stderr_content
        or ("terminating," in stderr_content and "bad configuration" in stderr_content)
        or "bad owner or permissions on" in stderr_content
    ):
        return (
            "SSH Config Error",
            "Invalid ~/.ssh/config syntax or file permissions",
            "Check your ~/.ssh/config syntax and ensure permissions are 600.",
        )

    if "unprotected private key file" in stderr_content or (
        "permissions" in stderr_content and "are too open" in stderr_content
    ):
        return (
            "Key Perms Error",
            "SSH private key permissions too open (run chmod 600 on key)",
            None,
        )

    has_proxy = bool(
        resolved_ssh
        and (resolved_ssh.get("proxy_command") or resolved_ssh.get("proxy_jump"))
    )
    if (
        "proxycommand" in stderr_content
        or "exec:" in stderr_content
        or has_proxy
        or "kex_exchange_identification" in stderr_content
    ) and (
        "no such file or directory" in stderr_content
        or "not found" in stderr_content
        or "command not found" in stderr_content
    ):
        return (
            "Proxy Cmd Missing",
            "ProxyCommand executable not found (check ~/.ssh/config)",
            None,
        )

    if (
        "kex_exchange_identification" in stderr_content
        or "connection closed by unknown port 65535" in stderr_content
        or ("proxycommand" in stderr_content and "exited with status" in stderr_content)
    ):
        return (
            "Proxy Failed",
            "ProxyCommand / ProxyJump connection closed before SSH handshake",
            None,
        )

    if "permission denied" in stderr_content or stderr_password_detected:
        return ("Auth Failure", "Permission denied (auth failure)", None)

    if "connection refused" in stderr_content:
        return ("Conn Refused", "Connection refused by remote host", None)

    if (
        "no route to host" in stderr_content
        or "destination host unreachable" in stderr_content
        or "network is unreachable" in stderr_content
    ):
        return ("Host Unreachable", "Host unreachable / No route to host", None)

    if "timed out" in stderr_content:
        return ("Timed Out", "Connection timed out", None)

    if (
        "could not resolve hostname" in stderr_content
        or "name or service not known" in stderr_content
    ):
        hint = None
        if resolved_ssh and not resolved_ssh.get("matched_ssh_config"):
            target = resolved_ssh.get("target", "")
            hint = f"Hint: '{target}' did not match any Host block in ~/.ssh/config."
        return ("DNS Failure", "Could not resolve remote hostname", hint)

    if (
        "address already in use" in stderr_content
        or "cannot listen to port" in stderr_content
        or "remote port forwarding failed" in stderr_content
    ):
        return ("All Ports In Use", "All requested ports are already in use", None)

    return ("Host Unreachable", "Connection rejected or unreachable", None)


def create_askpass_helper(askpass_dir: str, sock_path: str, token: str) -> str:
    """Create an executable SSH_ASKPASS helper that proxies prompts over a Unix socket."""
    py_path = os.path.join(askpass_dir, "askpass.py")
    sh_path = os.path.join(askpass_dir, "askpass.sh")

    py_body = f"""import json
import socket
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.connect({sock_path!r})
    payload = json.dumps({{"token": {token!r}, "prompt": prompt}}) + "\\n"
    sock.sendall(payload.encode("utf-8"))
    data = b""
    while not data.endswith(b"\\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
finally:
    sock.close()

if not data:
    sys.exit(1)

resp = json.loads(data.decode("utf-8"))
if resp.get("cancel") or "password" not in resp:
    sys.exit(1)

sys.stdout.write(resp["password"] + "\\n")
sys.stdout.flush()
"""
    with open(py_path, "w") as f:
        f.write(py_body)
    os.chmod(py_path, stat.S_IRUSR | stat.S_IWUSR)

    sh_body = (
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(py_path)} "$@"\n'
    )
    with open(sh_path, "w") as f:
        f.write(sh_body)
    os.chmod(sh_path, stat.S_IRWXU)
    return sh_path


def load_json_with_comments(filepath):
    with open(filepath, "r") as f:
        content = f.read()

    # Remove // and /* */ comments
    pattern = r'("(?:\\.|[^"\\])*")|//.*|/\*[\s\S]*?\*/'

    def replacer(match):
        if match.group(1):
            return match.group(1)
        else:
            return ""

    clean_content = re.sub(pattern, replacer, content)

    # Remove # comments
    pattern_hash = r'("(?:\\.|[^"\\])*")|#.*'

    def replacer_hash(match):
        if match.group(1):
            return match.group(1)
        else:
            return ""

    clean_content = re.sub(pattern_hash, replacer_hash, clean_content)

    try:
        return json.loads(clean_content)
    except json.JSONDecodeError as e:
        # Provide exact context for the syntax failure
        lines = clean_content.splitlines()
        error_line = (
            lines[e.lineno - 1].strip() if e.lineno <= len(lines) else "Unknown"
        )
        raise ConfigValidationError(
            f"Invalid JSON syntax\n"
            f"  Reason: {e.msg}\n"
            f"  Location: Line {e.lineno}, Column {e.colno}\n"
            f"  Snippet: '{error_line}'\n"
            f"  (Common issues: missing commas, trailing commas, or unquoted strings)"
        )


def validate_config(data, logger_cb=print):
    """Validates the structure of the config dictionary and logs parsed parameters."""
    if not isinstance(data, dict):
        raise ConfigValidationError(
            "The root of the configuration file must be a JSON object (dictionary)."
        )

    has_remote_host = "remote_host" in data and data["remote_host"] is not None
    has_ssh_host = "ssh_host" in data and data["ssh_host"] is not None
    if not has_remote_host and not has_ssh_host:
        raise ConfigValidationError(
            "Missing required root-level key: 'remote_host' (or 'ssh_host' for ~/.ssh/config alias)."
        )

    if has_remote_host:
        if not isinstance(data["remote_host"], str) or not data["remote_host"].strip():
            raise ConfigValidationError(
                "The 'remote_host' setting must be a non-empty string."
            )
    if has_ssh_host:
        if not isinstance(data["ssh_host"], str) or not data["ssh_host"].strip():
            raise ConfigValidationError(
                "The 'ssh_host' setting must be a non-empty string."
            )
    if (
        has_remote_host
        and has_ssh_host
        and data["remote_host"].strip() != data["ssh_host"].strip()
    ):
        raise ConfigValidationError(
            "Conflicting host settings: specify either 'remote_host' or 'ssh_host', not both with different values."
        )

    target_host = (data.get("remote_host") or data.get("ssh_host")).strip()
    logger_cb(f"[Parsed] target host: {target_host}")

    if "ssh_config" in data and data["ssh_config"] is not None:
        if not isinstance(data["ssh_config"], str) or not data["ssh_config"].strip():
            raise ConfigValidationError(
                "The 'ssh_config' setting must be a non-empty file path string."
            )
        expanded_ssh_cfg = os.path.expanduser(data["ssh_config"].strip())
        if not os.path.isfile(expanded_ssh_cfg):
            raise ConfigValidationError(
                f"Configured 'ssh_config' file not found: '{expanded_ssh_cfg}'"
            )
        logger_cb(f"[Parsed] ssh_config: {expanded_ssh_cfg}")

    if "remote_port" in data and data["remote_port"] is not None:
        if not _is_valid_port(data["remote_port"]):
            raise ConfigValidationError(
                "The 'remote_port' setting must be a valid port integer (1-65535)."
            )
        logger_cb(f"[Parsed] remote_port: {data['remote_port']} (explicit override)")
    else:
        logger_cb("[Parsed] remote_port: (inherit from ~/.ssh/config or default 22)")

    if "remote_user" in data and data["remote_user"] is not None:
        if not isinstance(data["remote_user"], str):
            raise ConfigValidationError(
                f"The 'remote_user' setting must be of type str. Found: {type(data['remote_user']).__name__}"
            )
        if data["remote_user"].strip():
            logger_cb(
                f"[Parsed] remote_user: {data['remote_user']} (explicit override)"
            )
        else:
            logger_cb(
                "[Parsed] remote_user: (inherit from ~/.ssh/config or current user)"
            )
    else:
        logger_cb("[Parsed] remote_user: (inherit from ~/.ssh/config or current user)")

    if "services" not in data:
        raise ConfigValidationError("Missing required root-level key: 'services'")
    if not isinstance(data["services"], dict):
        raise ConfigValidationError(
            f"The 'services' setting must be of type dict. Found: {type(data['services']).__name__}"
        )

    logger_cb(
        f"[Parsed] Found {len(data['services'])} service(s) to map in 'services' block."
    )

    for srv_name, srv_conf in data["services"].items():
        if not isinstance(srv_conf, dict):
            raise ConfigValidationError(
                f"Service definition for '{srv_name}' must be an object (dictionary)."
            )

        logger_cb(f"  ↳ Validating service definition: '{srv_name}'")

        srv_type = srv_conf.get("type", "local")
        if srv_type not in ["local", "remote"]:
            raise ConfigValidationError(
                f"Service '{srv_name}': 'type' must be 'local' or 'remote'. Got '{srv_type}'."
            )
        logger_cb(f"      [Parsed] type: {srv_type}")

        if "service_port" not in srv_conf:
            raise ConfigValidationError(
                f"Service '{srv_name}': Missing required key 'service_port'."
            )
        if not _is_valid_port(srv_conf["service_port"]):
            raise ConfigValidationError(
                f"Service '{srv_name}': 'service_port' must be a valid port integer (1-65535)."
            )
        logger_cb(f"      [Parsed] service_port: {srv_conf['service_port']}")

        if srv_type == "local":
            if "local_port" not in srv_conf:
                raise ConfigValidationError(
                    f"Service '{srv_name}': Missing 'local_port' (required for 'local' type services)."
                )
            if not _is_valid_port(srv_conf["local_port"]):
                raise ConfigValidationError(
                    f"Service '{srv_name}': 'local_port' must be a valid port integer (1-65535)."
                )
            logger_cb(f"      [Parsed] local_port: {srv_conf['local_port']}")
        else:
            if "remote_port" not in srv_conf:
                raise ConfigValidationError(
                    f"Service '{srv_name}': Missing 'remote_port' (required for 'remote' type services)."
                )
            if not _is_valid_port(srv_conf["remote_port"]):
                raise ConfigValidationError(
                    f"Service '{srv_name}': 'remote_port' must be a valid port integer (1-65535)."
                )
            logger_cb(f"      [Parsed] remote_port: {srv_conf['remote_port']}")

        service_host = srv_conf.get("service_host", "127.0.0.1")
        if not isinstance(service_host, str):
            raise ConfigValidationError(
                f"Service '{srv_name}': 'service_host' must be a string."
            )
        logger_cb(f"      [Parsed] service_host: {service_host}")

        bind_address = srv_conf.get("bind_address", "127.0.0.1")
        if not isinstance(bind_address, str):
            raise ConfigValidationError(
                f"Service '{srv_name}': 'bind_address' must be a string."
            )
        logger_cb(f"      [Parsed] bind_address: {bind_address}")

    logger_cb("Configuration validation complete. No errors found.")


def create_boilerplate_config(filepath):
    boilerplate = """// ============================================================================
// ▲ AEROTUNNEL CONFIGURATION TEMPLATE
// ============================================================================
// Use this file to configure your local SSH tunnel to your remote LLM or services.
// Comments (single-line // or #, block /* */) are fully supported!
// ============================================================================
{
  // SSH destination: a hostname, IP address, OR a Host alias defined in ~/.ssh/config
  // (e.g., "ssh.example.com" with ProxyCommand/User/IdentityFile, or "192.168.1.100").
  // Tip: You can also use "ssh_host" instead of "remote_host", and set "ssh_config" to use a custom SSH config path.
  "remote_host": "127.0.0.1",

  // Optional SSH port override. Omit or comment out to inherit Port from ~/.ssh/config (defaults to 22).
  "remote_port": 22,

  // Optional SSH username override. Omit or comment out to inherit User from ~/.ssh/config.
  "remote_user": "change_me",

  // Services to forward through the secure SSH tunnel.
  // Each service supports:
  //   - "type": "local" (default) or "remote" (reverse tunnel)
  //   - "local_port": local listen port (required for local type)
  //   - "remote_port": remote listen port (required for remote type)
  //   - "service_port": target port (always required)
  //   - "service_host": target destination host (defaults to "127.0.0.1" / localhost)
  //   - "bind_address": listening bind address override (defaults to "127.0.0.1")
  "services": {
    // 1. Minimally configured listener (-L) service forwarding.
    // Listens on local 127.0.0.1:11434, forwards to remote 127.0.0.1:11434
    "ollama": {
      "local_port": 11434,
      "service_port": 11434
    },
    // 2. Minimally configured reverse-forwarded (-R) service forwarding.
    // Listens on remote 127.0.0.1:9000, forwards to local 127.0.0.1:9000
    "reverse_test": {
      "type": "remote",
      "remote_port": 9000,
      "service_port": 9000
    },
    // 3. Maximally configured example (specifying all fields)
    // Opens a port on local 0.0.0.0:8080, tunnels through SSH to web.internal.someserver.com:80
    "max_example": {
      "type": "local",
      "local_port": 8080,
      "bind_address": "0.0.0.0",
      "service_host": "web.internal.someserver.com",
      "service_port": 80
    }
  }
}
"""
    with open(filepath, "w") as f:
        f.write(boilerplate)


def open_editor(filepath):
    editors = []

    # Try environment variables first
    if os.environ.get("VISUAL"):
        editors.append(os.environ.get("VISUAL"))
    if os.environ.get("EDITOR"):
        editors.append(os.environ.get("EDITOR"))

    # Standard terminal editors prioritized
    standard_editors = [
        "nvim",
        "vim",
        "nano",
        "pico",
        "micro",
        "emacs",
        "vi",
        "joe",
        "ee",
        "mcedit",
    ]
    if os.name == "nt":
        standard_editors.extend(["notepad.exe", "notepad"])

    for ed in standard_editors:
        if ed not in editors:
            editors.append(ed)

    success = False
    for editor in editors:
        cmd_parts = editor.split()
        binary = cmd_parts[0]
        resolved_binary = shutil.which(binary)
        if resolved_binary:
            cmd = [resolved_binary] + cmd_parts[1:] + [filepath]
            if binary == "emacs" and len(cmd_parts) == 1:
                cmd = [resolved_binary, "-nw", filepath]
            try:
                subprocess.run(cmd)
                success = True
                break
            except Exception:
                continue

    if not success:
        print(
            f"\n[!] Error: Could not launch any terminal-based text editor automatically."
        )
        print(f"    Please manually edit/create the configuration file at:")
        print(f"    {filepath}\n")
        input("Press Enter to continue once you have created/edited the file...")


def check_config_on_startup():
    config_dir = os.path.dirname(CONFIG_PATH)
    if not os.path.exists(config_dir):
        try:
            os.makedirs(config_dir, exist_ok=True)
        except Exception as e:
            print(f"Error creating config directory: {e}")

    if not os.path.exists(CONFIG_PATH):
        print("\n\033[1;36m▲ WELCOME TO AEROTUNNEL\033[0m")
        print(
            "\033[1;33mConfiguration file not found. Creating a boilerplate template...\033[0m"
        )
        ssh_hosts = list_ssh_config_hosts()
        if ssh_hosts:
            names = ", ".join(h["host"] for h in ssh_hosts[:8])
            print(f"\033[1;36mDetected ~/.ssh/config Host definitions:\033[0m {names}")
        create_boilerplate_config(CONFIG_PATH)
        time.sleep(1.5)

        while True:
            print(f"\nOpening {CONFIG_PATH} in your text editor...")
            time.sleep(1.0)
            open_editor(CONFIG_PATH)

            try:
                config_data = load_json_with_comments(CONFIG_PATH)
                validate_config(config_data, logger_cb=lambda m: print(f"    {m}"))
                print(
                    "\n\033[1;32m[✓] Configuration parsed and validated successfully!\033[0m"
                )
                time.sleep(1.0)
                break
            except ConfigValidationError as e:
                print(
                    f"\n\033[1;31m[!] Configuration Parsing/Validation Error:\033[0m\n{e}"
                )
                ans = (
                    input(
                        "\nWould you like to reopen the editor to fix the error? [Y/n]: "
                    )
                    .strip()
                    .lower()
                )
                if ans == "n":
                    break
            except Exception as e:
                print(
                    f"\n\033[1;31m[!] Unexpected Error parsing configuration file:\033[0m\n{e}"
                )
                ans = (
                    input(
                        "\nWould you like to reopen the editor to fix the error? [Y/n]: "
                    )
                    .strip()
                    .lower()
                )
                if ans == "n":
                    break


# --- CONFIGURABLE NOTIFICATION SETTINGS ---
NOTIFICATION_FAIL_THRESHOLD = 5
NOTIFICATION_COOLDOWN_SEC = 3600  # 1 hour


def send_os_notification(title: str, message: str):
    if "com.termux" in os.environ.get("PREFIX", ""):
        if shutil.which("termux-notification"):
            subprocess.run(
                ["termux-notification", "-t", title, "-c", message], check=False
            )
    else:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False)


def fmt_time(seconds: float) -> str:
    """Helper to convert raw seconds into a readable Xh Ym Zs format."""
    if seconds < 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{int(seconds)}s"


class TunnelStats:
    def __init__(self):
        self.app_start_time = time.time()
        self.attempts = 0
        self.success_count = 0
        self.errors = 0
        self.first_success = "N/A"
        self.status = "Initializing"

        # Connection trackers
        self.current_session_start = None
        self.max_duration = 0.0
        self.total_duration = 0.0

        # Outage trackers
        self.current_outage_start = time.time()
        self.max_outage = 0.0
        self.total_outage = 0.0

        # Notification state
        self.consecutive_errors = 0
        self.last_error_notify_time = 0.0
        self.in_error_state = False

    def record_disconnect(self):
        if self.current_session_start:
            duration = time.time() - self.current_session_start
            self.total_duration += duration
            if duration > self.max_duration:
                self.max_duration = duration
        self.current_session_start = None

        # Start the outage timer if it isn't already running
        if not self.current_outage_start:
            self.current_outage_start = time.time()

    def record_connect(self):
        if self.current_outage_start:
            outage = time.time() - self.current_outage_start
            self.total_outage += outage
            if outage > self.max_outage:
                self.max_outage = outage
        self.current_outage_start = None
        self.current_session_start = time.time()


# --- HELP MODAL ---
class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen", "Close"), ("h", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        help_text = f"""[bold cyan]▲ AEROTUNNEL // HELP & CONTROLS[/bold cyan]

[bold]Keyboard Shortcuts:[/bold]
• [yellow]Enter / Space[/yellow] : Expand or Collapse Service Row
  [dim]Inspect full tunnel details (type, bind listen address, and target destination host/port) inline.[/dim]

• [yellow]t[/yellow] : Switch Target Host (~/.ssh/config)
  [dim]Browse concrete Host definitions in ~/.ssh/config (including ProxyCommand / IdentityFile routes) and switch targets live.[/dim]

• [yellow]b[/yellow] : Configure Network Bindings
  [dim]Selectively expose services to your network (0.0.0.0) or restrict them to loopback only (127.0.0.1).[/dim]

• [yellow]l[/yellow] : View System Logs
  [dim]Opens the raw SSH output buffer. Essential for debugging connection drops or auth failures.[/dim]

• [yellow]s[/yellow] : Interactive Shell
  [dim]Suspends the UI and drops you into a native terminal session on the remote server.[/dim]
  [bold red]CRITICAL:[/bold red] Type 'exit' or press [bold]Ctrl+D[/bold] in the remote shell to return to this dashboard!

• [yellow]c[/yellow] : Edit Configuration
  [dim]Pauses the TUI and opens the configuration file in your preferred editor. The tunnel restarts automatically after exit.[/dim]

• [yellow]r[/yellow] : Retry connections
  [dim]Drops all active connections and reattempts to bind all configured services (useful after manually freeing blocked ports).[/dim]

• [yellow]h[/yellow] : Show this Help dialog

• [yellow]q[/yellow] : Quit application

[bold cyan]============================================================[/bold cyan]
[bold]About:[/bold]
• [yellow]Version:[/yellow] v{VERSION}
• [yellow]Released:[/yellow] {DATE_RELEASED}
• [yellow]Author:[/yellow] Andrew Wilson

[dim]Press ESC or 'h' to close this dialog.[/dim]"""
        yield Container(Static(help_text, markup=True), id="help_panel")


# --- BINDING CONFIGURATION MODAL ---
class BindingModal(ModalScreen[dict]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("space", "toggle", "Toggle 0.0.0.0 / 127.0.0.1"),
    ]

    def __init__(self, current_bindings):
        super().__init__()
        self.working_bindings = current_bindings.copy()

    def compose(self) -> ComposeResult:
        yield Container(
            Label(
                "[bold cyan]Configure Service Network Bindings[/bold cyan]\n[dim]Up/Down to select • Space to toggle • Enter to apply[/dim]",
                id="dialog_title",
            ),
            DataTable(id="binding_table"),
            id="dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_column("Service", key="srv")
        table.add_column("Bind Address", key="ip")

        for srv, ip in self.working_bindings.items():
            self._add_or_update_row(table, srv, ip)

    def _add_or_update_row(self, table, srv, ip):
        ip_display = (
            f"[bold white on red] 0.0.0.0 [/]"
            if ip == "0.0.0.0"
            else f"[bold green]{ip}[/]"
        )
        if srv in [row.value for row in table.rows]:
            table.update_cell(srv, "ip", ip_display)
        else:
            table.add_row(srv.upper().replace("_", "-"), ip_display, key=srv)

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            self.action_apply()
            event.prevent_default()

    def action_toggle(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return

        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        srv = row_key.value

        current_ip = self.working_bindings[srv]
        new_ip = "0.0.0.0" if current_ip == "127.0.0.1" else "127.0.0.1"
        self.working_bindings[srv] = new_ip

        self._add_or_update_row(table, srv, new_ip)

    def action_apply(self) -> None:
        self.dismiss(self.working_bindings)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- SSH CONFIG HOST PICKER MODAL ---
class SshHostModal(ModalScreen[str]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, hosts, current_host: str = ""):
        super().__init__()
        self.hosts = hosts
        self.current_host = current_host

    def compose(self) -> ComposeResult:
        if self.hosts:
            yield Container(
                Label(
                    "[bold cyan]Select SSH Host from ~/.ssh/config[/bold cyan]\n"
                    "[dim]Up/Down to select • Enter to switch target (session) • ESC to cancel[/dim]",
                    id="dialog_title",
                ),
                DataTable(id="ssh_hosts_table"),
                id="dialog",
            )
        else:
            yield Container(
                Label(
                    "[bold cyan]No Concrete Hosts Found in ~/.ssh/config[/bold cyan]\n"
                    "[dim]Define a `Host <name>` block in ~/.ssh/config or press 'c' to edit config.json.\n"
                    "Press ESC to close.[/dim]",
                    id="dialog_title",
                ),
                id="dialog",
            )

    def on_mount(self) -> None:
        if not self.hosts:
            return
        table = self.query_one("#ssh_hosts_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Host Alias", "Resolved Target", "Route")
        current_idx = 0
        for idx, entry in enumerate(self.hosts):
            alias = entry["host"]
            user = entry.get("user") or ""
            hostname = entry.get("hostname") or alias
            port = entry.get("port", 22)
            target_str = f"{user}@{hostname}:{port}" if user else f"{hostname}:{port}"
            if entry.get("proxy_command"):
                route = f"ProxyCommand"
            elif entry.get("proxy_jump"):
                route = f"ProxyJump: {entry['proxy_jump']}"
            else:
                route = "Direct"
            label = f"[bold cyan]* {alias}[/]" if alias == self.current_host else alias
            if alias == self.current_host:
                current_idx = idx
            table.add_row(label, target_str, route, key=alias)
        table.cursor_coordinate = (current_idx, 0)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(event.row_key.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- SSH PASSWORD PROMPT MODAL ---
class SshPasswordPrompt(ModalScreen[str]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self, prompt_text: str = "Enter SSH password or passphrase to proceed"
    ):
        super().__init__()
        self.prompt_text = prompt_text

    def compose(self) -> ComposeResult:
        yield Container(
            Label(
                f"[bold cyan]SSH Authentication Required[/bold cyan]\n[dim]{escape(self.prompt_text)}[/dim]",
                id="dialog_title",
            ),
            Input(
                placeholder="Enter password / passphrase",
                password=True,
                id="password_input",
            ),
            Container(
                Button("Submit", variant="primary", id="submit_btn"),
                Button("Cancel", id="cancel_btn"),
                id="button_container",
            ),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#password_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit_btn":
            input_widget = self.query_one("#password_input", Input)
            self.dismiss(input_widget.value)
        elif event.button.id == "cancel_btn":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- LOGGING MODAL ---
class LogScreen(ModalScreen):
    BINDINGS = [
        ("l", "app.pop_screen", "Close Log"),
        ("escape", "app.pop_screen", "Close"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            RichLog(id="app_log", highlight=True, markup=True), id="log_panel"
        )

    def on_mount(self) -> None:
        logger = self.query_one(RichLog)
        for msg in getattr(self.app, "log_history", []):
            logger.write(msg)


# --- MAIN APP ---
class TunnelApp(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 1; 
        grid-columns: 5fr 7fr;
        padding: 1;
    }
    
    Screen.mobile_layout {
        grid-size: 1 2; 
        grid-rows: 1fr 1fr;
        grid-columns: 1fr;
    }
    
    #stats_container, #services_container {
        height: 100%;
        border: round cyan;
        padding: 1;
    }
    
    #stats_container { border: round magenta; border-title-color: magenta; }
    #services_container { border-title-color: cyan; }
    
    LogScreen, BindingModal, HelpScreen, SshPasswordPrompt, SshHostModal {
        align: center middle;
        background: $background 80%;
    }
    
    #log_panel, #help_panel, #dialog {
        width: 90%; 
        max-width: 90;
        border: solid $primary; 
        background: $surface;
        padding: 1 2;
    }
    
    #log_panel { height: 90%; }
    #help_panel, #dialog { height: auto; }
    #dialog_title { text-align: center; margin-bottom: 1; width: 100%; }
    #button_container { layout: horizontal; height: auto; align: center middle; margin-top: 1; }
    #button_container Button { margin: 0 1; }
    """

    BINDINGS = [
        ("h", "show_help", "Help"),
        ("t", "select_ssh_host", "Hosts"),
        ("b", "configure_bindings", "Bindings"),
        ("l", "toggle_log", "Logs"),
        ("s", "open_shell", "Shell"),
        ("c", "edit_config", "Config"),
        ("r", "retry_connections", "Retry"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.stats = TunnelStats()
        self.log_history = []
        self.resolved_ssh = None
        self.config = self.load_config()
        self.running = True
        self.ssh_process = None
        self.intentional_restart = False
        self.sleep_task = None

        self._auth_lock = asyncio.Lock()
        self._auth_prompt_active = False
        self._auth_cancelled = False
        self._active_password_modal = None
        self._askpass_server = None
        self._askpass_dir = None

        self.service_bindings = {}
        self.service_statuses = {}
        self.expanded_services = set()
        self.is_partial_mode = False
        self.active_services_to_bind = []
        if self.config:
            for srv, srv_conf in self.config.get("services", {}).items():
                self.service_bindings[srv] = srv_conf.get("bind_address", "127.0.0.1")
                self.service_statuses[srv] = "pending"
            self.refresh_resolved_ssh()

    def refresh_resolved_ssh(self) -> None:
        if not self.config:
            self.resolved_ssh = None
            return
        target = get_ssh_target(self.config)
        user = self.config.get("remote_user") or None
        port = self.config.get("remote_port")
        ssh_cfg = self.config.get("ssh_config")
        info = resolve_ssh_target(target, user=user, port=port, ssh_config_path=ssh_cfg)
        self.resolved_ssh = info
        if info.get("error"):
            self.log_msg(
                f"[bold red]SSH Config Resolution Error:[/bold red] {info['error']}"
            )
        elif info.get("matched_ssh_config"):
            self.log_msg(
                f"[cyan]Resolved SSH Host '{target}' via {info['ssh_config_path']}:[/cyan] "
                f"{info['user']}@{info['hostname']}:{info['port']} ({info['route_summary']})"
            )
            if info.get("explicit_user") or info.get("explicit_port"):
                base_info = resolve_ssh_target(target, ssh_config_path=ssh_cfg)
                if (
                    info.get("explicit_user")
                    and base_info.get("user")
                    and base_info["user"] != info["user"]
                ):
                    self.log_msg(
                        f"[dim]Note: config.json 'remote_user' ('{info['user']}') overrides ~/.ssh/config User ('{base_info['user']}').[/dim]"
                    )
                if (
                    info.get("explicit_port")
                    and base_info.get("port")
                    and base_info["port"] != info["port"]
                ):
                    self.log_msg(
                        f"[dim]Note: config.json 'remote_port' ({info['port']}) overrides ~/.ssh/config Port ({base_info['port']}).[/dim]"
                    )

    def load_config(self):
        self.log_msg(
            f"[bold cyan]Trying to read config file '{CONFIG_PATH}'...[/bold cyan]"
        )

        try:
            config_data = load_json_with_comments(CONFIG_PATH)
            self.log_msg(
                "[bold cyan]Loading and validating configuration data...[/bold cyan]"
            )
            validate_config(
                config_data, logger_cb=lambda m: self.log_msg(f"[dim]{m}[/dim]")
            )
            return config_data
        except ConfigValidationError as e:
            self.log_msg(
                f"\n[bold red]Configuration Validation Error:[/bold red]\n[red]{e}[/red]\n"
            )
            return None
        except Exception as e:
            self.log_msg(
                f"\n[bold red]Unexpected Error parsing config:[/bold red]\n[red]{e}[/red]\n"
            )
            return None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        stats_container = Container(Static(id="stats_text"), id="stats_container")
        stats_container.border_title = "Session Diagnostics"
        yield stats_container

        srv_container = Container(
            DataTable(id="services_table"), id="services_container"
        )
        srv_container.border_title = "Active Port Mappings"
        yield srv_container

        yield Footer()

    def on_mount(self) -> None:
        self.install_screen(LogScreen(), name="log_screen")
        self.install_screen(HelpScreen(), name="help_screen")
        self.title = "▲ AEROTUNNEL COMMAND STATUS"
        self.sub_title = f"v{VERSION} ({DATE_RELEASED})"

        table = self.query_one("#services_table", DataTable)
        table.cursor_type = "row"

        self.update_services_table()
        self.update_ui()
        self.set_interval(1.0, self.update_ui)
        self.run_ssh_loop()

    def on_unmount(self) -> None:
        self.running = False
        if self._active_password_modal is not None:
            try:
                self._active_password_modal.dismiss(None)
            except Exception:
                pass
        if self.sleep_task:
            self.sleep_task.cancel()
        if self._askpass_server is not None:
            try:
                self._askpass_server.close()
            except Exception:
                pass
        if self._askpass_dir and os.path.exists(self._askpass_dir):
            shutil.rmtree(self._askpass_dir, ignore_errors=True)
            self._askpass_dir = None
        if self.ssh_process:
            try:
                self.ssh_process.terminate()
            except Exception:
                pass

    def _is_compact_table(self, screen_width=None) -> bool:
        width = screen_width if screen_width is not None else self.size.width
        if not width:
            return False
        if width <= MOBILE_SCREEN_MAX_WIDTH:
            return True
        # Desktop uses 5fr : 7fr columns with 2 chars screen padding and 4 chars container chrome
        est_panel_width = int((width - 2) * (7 / 12)) - 4
        return est_panel_width < WIDE_TABLE_MIN_WIDTH

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width <= MOBILE_SCREEN_MAX_WIDTH:
            self.screen.add_class("mobile_layout")
        else:
            self.screen.remove_class("mobile_layout")
        self.update_services_table(screen_width=event.size.width)

    @staticmethod
    def _compact_target_label(service_host: str, service_port: int) -> str:
        if service_host in ("127.0.0.1", "localhost"):
            return str(service_port)
        host_short = service_host if len(service_host) <= 7 else f"{service_host[:6]}…"
        return f"{host_short}:{service_port}"

    def _resolve_parent_service(self, row_key: str):
        if not self.config:
            return None
        services = self.config.get("services", {})
        if row_key in services:
            return row_key
        if row_key.endswith(("_sub_type", "_sub_listen", "_sub_dest")):
            prefix = row_key.rsplit("_sub_", 1)[0]
            if prefix in services:
                return prefix
        return None

    def _toggle_service_row(self, row_key: str) -> bool:
        parent_service = self._resolve_parent_service(row_key)
        if not parent_service:
            return False
        focus_key = None
        if parent_service in self.expanded_services:
            self.expanded_services.remove(parent_service)
            if row_key != parent_service:
                focus_key = parent_service
        else:
            self.expanded_services.add(parent_service)
        self.update_services_table(focus_row_key=focus_key)
        return True

    def update_services_table(self, focus_row_key=None, screen_width=None):
        table = self.query_one("#services_table", DataTable)

        if focus_row_key is not None:
            old_row_key = focus_row_key
        else:
            try:
                old_coordinate = table.cursor_coordinate
                old_row_key = table.coordinate_to_cell_key(old_coordinate).row_key.value
            except Exception:
                old_row_key = None

        table.clear(columns=True)

        is_compact = self._is_compact_table(screen_width=screen_width)
        if is_compact:
            table.add_columns("Service", "Tunnel Map", "Status")
        else:
            table.add_columns(
                "Service", "Listen (Source)", "Flow", "Target (Destination)", "Status"
            )

        if self.config:
            for name, srv_conf in self.config.get("services", {}).items():
                srv_type = srv_conf.get("type", "local")
                service_host = srv_conf.get("service_host", "127.0.0.1")
                service_port = srv_conf["service_port"]

                if srv_type == "remote":
                    listen_port = srv_conf["remote_port"]
                else:
                    listen_port = srv_conf["local_port"]

                bind_ip = self.service_bindings.get(
                    name, srv_conf.get("bind_address", "127.0.0.1")
                )
                bind_display = (
                    f"[bold white on red] 0.0.0.0 [/]"
                    if bind_ip == "0.0.0.0"
                    else f"[bold green]{bind_ip}[/]"
                )
                listen_display = f"{bind_display}:{listen_port}"

                status_val = self.service_statuses.get(name, "pending")
                if status_val == "active":
                    status_display = "[bold green]✔️ ACTIVE[/]"
                elif status_val == "failed_port_in_use":
                    status_display = "[bold red]❌ PORT IN USE[/]"
                else:
                    status_display = "[bold yellow]⏳ PENDING[/]"

                if is_compact:
                    compact_bind = (
                        "[bold white on red]0.0.0.0[/]"
                        if bind_ip == "0.0.0.0"
                        else f"[bold green]{bind_ip}[/]"
                    )
                    target_compact = self._compact_target_label(
                        service_host, service_port
                    )
                    if srv_type == "remote":
                        map_str = f"[bold cyan][R][/] {compact_bind}:{listen_port} [yellow]↢[/] {target_compact}"
                    else:
                        map_str = f"[bold cyan][L][/] {compact_bind}:{listen_port} [yellow]↣[/] {target_compact}"

                    table.add_row(
                        name.upper().replace("_", "-"),
                        map_str,
                        status_display,
                        key=name,
                    )
                else:
                    flow_str = (
                        "[yellow]↢ [R] ↢[/]"
                        if srv_type == "remote"
                        else "[yellow]↣ [L] ↣[/]"
                    )
                    table.add_row(
                        name.upper().replace("_", "-"),
                        listen_display,
                        flow_str,
                        f"{service_host}:{service_port}",
                        status_display,
                        key=name,
                    )

                if name in self.expanded_services:
                    type_str = (
                        "Remote Forwarding (-R)"
                        if srv_type == "remote"
                        else "Local Forwarding (-L)"
                    )
                    if is_compact:
                        table.add_row(
                            "  [dim]↳ Type:[/dim]",
                            f"[cyan]{type_str}[/]",
                            "",
                            key=f"{name}_sub_type",
                        )
                        table.add_row(
                            "  [dim]↳ Listen:[/dim]",
                            listen_display,
                            "",
                            key=f"{name}_sub_listen",
                        )
                        table.add_row(
                            "  [dim]↳ Target:[/dim]",
                            f"[white]{service_host}:{service_port}[/]",
                            "",
                            key=f"{name}_sub_dest",
                        )
                    else:
                        table.add_row(
                            "  [dim]↳ Type:[/dim]",
                            f"[cyan]{type_str}[/]",
                            "",
                            "",
                            "",
                            key=f"{name}_sub_type",
                        )
                        table.add_row(
                            "  [dim]↳ Listen:[/dim]",
                            listen_display,
                            "",
                            "",
                            "",
                            key=f"{name}_sub_listen",
                        )
                        table.add_row(
                            "  [dim]↳ Target:[/dim]",
                            "",
                            "",
                            f"[white]{service_host}:{service_port}[/]",
                            "",
                            key=f"{name}_sub_dest",
                        )

        if old_row_key is not None:
            try:
                for idx, rk in enumerate(table.rows.keys()):
                    if rk.value == old_row_key:
                        table.cursor_coordinate = (idx, 0)
                        break
            except Exception:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._toggle_service_row(event.row_key.value)

    def on_key(self, event: events.Key) -> None:
        if event.key == "space" and not self.screen.is_modal:
            try:
                table = self.query_one("#services_table", DataTable)
            except Exception:
                return
            if self.focused is table:
                try:
                    coordinate = table.cursor_coordinate
                    row_key = table.coordinate_to_cell_key(coordinate).row_key.value
                    if self._toggle_service_row(row_key):
                        event.prevent_default()
                except Exception:
                    pass

    def action_show_help(self) -> None:
        self.push_screen("help_screen")

    def action_toggle_log(self) -> None:
        self.push_screen("log_screen")

    def action_open_shell(self) -> None:
        if not self.config:
            self.log_msg("[red]Cannot open shell: No valid config loaded.[/red]")
            return

        with self.suspend():
            os.system("cls" if os.name == "nt" else "clear")
            print("\n\033[1;36m▲ LOCAL LLM TUNNEL // INTERACTIVE SHELL\033[0m")
            print(
                "\033[1;31m============================================================\033[0m"
            )
            print(
                "\033[1;33m REMINDER: Press CTRL+D or type 'exit' to return to the TUI \033[0m"
            )
            print(
                "\033[1;31m============================================================\033[0m\n"
            )
            print("Handshaking with remote server...")

            time.sleep(2.5)

            cmd = [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ] + build_ssh_connection_args(self.config)

            try:
                subprocess.run(cmd)
            except Exception as e:
                print(f"Shell error: {e}")

            time.sleep(0.5)

    def action_edit_config(self) -> None:
        with self.suspend():
            os.system("cls" if os.name == "nt" else "clear")
            print("\n\033[1;36m▲ LOCAL LLM TUNNEL // EDIT CONFIGURATION\033[0m")
            print(
                "\033[1;31m============================================================\033[0m"
            )
            print(
                "\033[1;33m REMINDER: Save and quit your editor to return to the TUI.  \033[0m"
            )
            print(
                "\033[1;31m============================================================\033[0m\n"
            )

            time.sleep(1.0)
            open_editor(CONFIG_PATH)
            time.sleep(0.5)

        old_services = self.config.get("services", {}) if self.config else {}
        new_config = self.load_config()
        if new_config:
            self.config = new_config
            self.is_partial_mode = False
            self.refresh_resolved_ssh()

            # Re-initialize and update service bindings & statuses
            new_bindings = {}
            new_statuses = {}
            for srv, srv_conf in self.config.get("services", {}).items():
                new_file_bind = srv_conf.get("bind_address", "127.0.0.1")
                old_file_bind = old_services.get(srv, {}).get(
                    "bind_address", "127.0.0.1"
                )
                if srv in self.service_bindings and new_file_bind == old_file_bind:
                    new_bindings[srv] = self.service_bindings[srv]
                else:
                    new_bindings[srv] = new_file_bind
                new_statuses[srv] = "pending"
            self.service_bindings = new_bindings
            self.service_statuses = new_statuses
            self.expanded_services = {
                s
                for s in self.expanded_services
                if s in self.config.get("services", {})
            }

            self.update_services_table()
            self.update_ui()
            self.log_msg(
                "[yellow]Configuration reloaded. Restarting SSH tunnel...[/yellow]"
            )
            self.restart_tunnel()
        else:
            self.log_msg(
                "[bold red]Failed to apply reloaded config due to parsing errors. Retaining previous stable config.[/bold red]"
            )

    @work
    async def action_select_ssh_host(self) -> None:
        ssh_cfg = self.config.get("ssh_config") if self.config else None
        hosts = list_ssh_config_hosts(ssh_cfg)
        current = get_ssh_target(self.config) if self.config else ""
        selected = await self.push_screen_wait(
            SshHostModal(hosts, current_host=current)
        )
        if selected:
            if not self.config:
                self.log_msg("[red]Cannot switch host: No valid config loaded.[/red]")
                return
            self.config["remote_host"] = selected
            self.config.pop("ssh_host", None)
            self.config.pop("remote_user", None)
            self.config.pop("remote_port", None)
            self.refresh_resolved_ssh()
            self.update_ui()
            self.log_msg(
                f"[yellow]Switched active SSH Host to '{selected}' for this session (edit config with 'c' to persist).[/yellow]"
            )
            self.restart_tunnel()

    @work
    async def action_configure_bindings(self) -> None:
        new_bindings = await self.push_screen_wait(BindingModal(self.service_bindings))
        if new_bindings:
            self.service_bindings = new_bindings
            self.update_services_table()
            self.log_msg(
                "[yellow]Network bindings updated. Restarting SSH tunnel...[/yellow]"
            )
            self.restart_tunnel()

    def restart_tunnel(self):
        self.intentional_restart = True
        self.is_partial_mode = False
        self._auth_cancelled = False
        if self._active_password_modal is not None:
            try:
                self._active_password_modal.dismiss(None)
            except Exception:
                pass
        if self.ssh_process:
            try:
                self.ssh_process.terminate()
            except Exception:
                pass
        if self.sleep_task:
            self.sleep_task.cancel()

    def action_retry_connections(self) -> None:
        self.log_msg(
            "[yellow]User requested connection retry. Resetting and dropping active links...[/yellow]"
        )

        # Reset all service statuses to pending
        if self.config:
            for srv in self.config.get("services", {}).keys():
                self.service_statuses[srv] = "pending"
        self.update_services_table()

        self.restart_tunnel()

    def log_msg(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[dim]{timestamp}[/dim] {msg}"

        self.log_history.append(formatted_msg)
        if len(self.log_history) > 500:
            self.log_history.pop(0)

        try:
            if self.is_screen_installed("log_screen"):
                log_screen = self.get_screen("log_screen")
                logger = log_screen.query_one(RichLog)
                app_thread_id = getattr(self, "_thread_id", threading.get_ident())
                if threading.get_ident() == app_thread_id:
                    logger.write(formatted_msg)
                else:
                    self.call_from_thread(logger.write, formatted_msg)
        except Exception:
            pass

    def update_ui(self):
        is_connected = self.stats.current_session_start is not None

        # Calculate real-time active timers
        app_runtime = max(0, time.time() - self.stats.app_start_time)
        active_session = (
            time.time() - self.stats.current_session_start if is_connected else 0.0
        )
        active_outage = (
            time.time() - self.stats.current_outage_start if not is_connected else 0.0
        )

        # Calculate cumulative totals
        total_up = self.stats.total_duration + active_session
        total_down = self.stats.total_outage + active_outage

        # Derived SLAs
        uptime_pct = (total_up / app_runtime * 100) if app_runtime > 0 else 0.0
        mtbf = total_up / max(1, self.stats.errors)
        mttr = total_down / max(1, self.stats.errors)
        max_outage_display = max(self.stats.max_outage, active_outage)

        # Build the Table
        table = Table.grid(padding=(0, 2, 0, 0))
        table.add_column(style="bold magenta", justify="right")
        table.add_column(style="white")
        table.add_column(style="bold magenta", justify="right")
        table.add_column(style="white")

        remote_host = "N/A"
        remote_port = "N/A"
        extra_route_rows = []
        if self.config:
            target = get_ssh_target(self.config)
            res = self.resolved_ssh or {}
            eff_user = self.config.get("remote_user") or res.get("user") or ""
            eff_port = (
                self.config.get("remote_port")
                if self.config.get("remote_port") is not None
                else res.get("port", 22)
            )
            host_label = f"{eff_user}@{target}" if eff_user else target
            if res.get("matched_ssh_config"):
                remote_host = (
                    f"[bold cyan]{escape(host_label)}[/] [dim green](ssh_config)[/]"
                )
            else:
                remote_host = f"[bold cyan]{escape(host_label)}[/]"
            remote_port = f"[bold cyan]{eff_port}[/]"
            eff_hostname = res.get("hostname", target)
            if eff_hostname and eff_hostname != target:
                extra_route_rows.append(
                    ("Resolved Host:", f"[cyan]{escape(eff_hostname)}[/]")
                )
            if res.get("proxy_command") or res.get("proxy_jump"):
                extra_route_rows.append(
                    ("SSH Route:", f"[cyan]{escape(res.get('route_summary', ''))}[/]")
                )

        table.add_row("Remote Host:", remote_host, "SSH Port:", remote_port)
        for idx in range(0, len(extra_route_rows), 2):
            left_k, left_v = extra_route_rows[idx]
            if idx + 1 < len(extra_route_rows):
                right_k, right_v = extra_route_rows[idx + 1]
            else:
                right_k, right_v = "", ""
            table.add_row(left_k, left_v, right_k, right_v)
        table.add_row(
            "Engine Runtime:",
            fmt_time(app_runtime),
            "Uptime Ratio:",
            f"{uptime_pct:.5f}%",
        )
        table.add_row(
            "Total Uptime:", fmt_time(total_up), "Total Downtime:", fmt_time(total_down)
        )
        table.add_row(
            "Current Link:",
            fmt_time(active_session),
            "Longest Link:",
            fmt_time(max(self.stats.max_duration, active_session)),
        )
        table.add_row(
            "Current Outage:",
            fmt_time(active_outage),
            "Longest Outage:",
            fmt_time(max_outage_display),
        )
        table.add_row(
            "MTBF (Avg Up):", fmt_time(mtbf), "MTTR (Avg Down):", fmt_time(mttr)
        )

        if is_connected:
            if self.is_partial_mode or self.stats.status in ["DEGRADED", "PARTIAL"]:
                status_ui = f"[bold yellow]{self.stats.status}[/]"
            else:
                status_ui = "[bold green]CONNECTED[/]"
        else:
            status_ui = f"[bold red]{self.stats.status}[/]"

        table.add_row(
            "Attempts/Drops:",
            f"{self.stats.attempts} / {self.stats.errors}",
            "Status:",
            status_ui,
        )

        self.query_one("#stats_text", Static).update(table)

    def handle_connection_failure(self):
        self.stats.record_disconnect()
        self.stats.consecutive_errors += 1
        self.stats.errors += 1

        if self.stats.consecutive_errors >= NOTIFICATION_FAIL_THRESHOLD:
            now = time.time()
            if (
                not self.stats.in_error_state
                or (now - self.stats.last_error_notify_time) > NOTIFICATION_COOLDOWN_SEC
            ):
                send_os_notification(
                    "LLM Tunnel Down",
                    f"Connection failed {self.stats.consecutive_errors} times in a row.",
                )
                self.stats.last_error_notify_time = now
                self.stats.in_error_state = True

    def handle_connection_success(self):
        self.stats.record_connect()
        if self.stats.in_error_state:
            send_os_notification(
                "LLM Tunnel Restored", "Connection to remote LLM server re-established."
            )
            self.stats.in_error_state = False

        self.stats.consecutive_errors = 0
        self.stats.success_count += 1
        if self.stats.first_success == "N/A":
            self.stats.first_success = datetime.now().strftime("%H:%M:%S")

    async def _start_askpass_server(self):
        self._askpass_dir = tempfile.mkdtemp(prefix="at_")
        os.chmod(self._askpass_dir, stat.S_IRWXU)
        sock_path = os.path.join(self._askpass_dir, "s")
        token = secrets.token_hex(16)
        script_path = create_askpass_helper(self._askpass_dir, sock_path, token)

        async def handle_client(reader, writer):
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not raw:
                    return
                req = json.loads(raw.decode("utf-8"))
                if (
                    not secrets.compare_digest(req.get("token", ""), token)
                    or not self.running
                ):
                    writer.write(b'{"cancel": true}\n')
                    await writer.drain()
                    return

                prompt_text = str(req.get("prompt", "Enter SSH password:"))
                async with self._auth_lock:
                    if self._auth_cancelled or not self.running:
                        writer.write(b'{"cancel": true}\n')
                        await writer.drain()
                        return

                    self._auth_prompt_active = True
                    self.stats.status = "Password Required"
                    self.log_msg(
                        f"[bold yellow]SSH is asking for a password:[/bold yellow] {prompt_text}"
                    )
                    modal = SshPasswordPrompt(prompt_text=prompt_text)
                    self._active_password_modal = modal
                    try:
                        password = await self.push_screen_wait(modal)
                    finally:
                        self._active_password_modal = None
                        self._auth_prompt_active = False

                    if password is None:
                        self._auth_cancelled = True
                        self.stats.status = "Auth Cancelled"
                        writer.write(b'{"cancel": true}\n')
                        await writer.drain()
                        if self.ssh_process and self.ssh_process.returncode is None:
                            try:
                                self.ssh_process.terminate()
                            except Exception:
                                pass
                    else:
                        payload = (json.dumps({"password": password}) + "\n").encode(
                            "utf-8"
                        )
                        writer.write(payload)
                        await writer.drain()
            except Exception:
                try:
                    writer.write(b'{"cancel": true}\n')
                    await writer.drain()
                except Exception:
                    pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        self._askpass_server = await asyncio.start_unix_server(
            handle_client, path=sock_path
        )
        os.chmod(sock_path, stat.S_IRUSR | stat.S_IWUSR)
        return script_path

    @work(exclusive=True)
    async def run_ssh_loop(self):
        if not self.config:
            self.log_msg(
                "[red]Error: System looping stalled without a valid config.[/red]"
            )
            return

        self.log_msg(
            "[bold cyan]System Initialized. Starting routing loop...[/bold cyan]"
        )

        askpass_script = None
        try:
            askpass_script = await self._start_askpass_server()
        except Exception as e:
            self.log_msg(
                f"[yellow]Askpass helper unavailable ({e}); continuing without interactive auth modal.[/yellow]"
            )

        try:
            while self.running:
                self.intentional_restart = False
                self._auth_cancelled = False

                # If not in partial mode, this is a fresh attempt to bind all configured services
                if not self.is_partial_mode:
                    self.stats.status = "Routing..."
                    for srv in self.config["services"].keys():
                        self.service_statuses[srv] = "pending"
                    self.active_services_to_bind = list(self.config["services"].keys())
                    self.update_services_table()

                self.stats.attempts += 1

                cmd = build_ssh_tunnel_command(
                    self.config,
                    self.active_services_to_bind,
                    self.service_bindings,
                )
                self.log_msg(f"[yellow]Spawning Subprocess:[/yellow] {' '.join(cmd)}")

                if self.resolved_ssh and self.resolved_ssh.get("proxy_command"):
                    try:
                        proxy_parts = shlex.split(self.resolved_ssh["proxy_command"])
                        if (
                            proxy_parts
                            and not shutil.which(proxy_parts[0])
                            and not os.path.exists(proxy_parts[0])
                        ):
                            self.log_msg(
                                f"[bold yellow]Warning: ProxyCommand binary '{proxy_parts[0]}' not found on PATH.[/bold yellow]"
                            )
                    except ValueError:
                        pass

                env = os.environ.copy()
                if askpass_script:
                    env["SSH_ASKPASS"] = askpass_script
                    env["SSH_ASKPASS_REQUIRE"] = "force"
                    if not env.get("DISPLAY"):
                        env["DISPLAY"] = ":0"

                stream_tasks = []
                try:
                    self.ssh_process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                        env=env,
                    )

                    stderr_lines = []
                    stderr_password_detected = False
                    proxy_auth_pending = False

                    def handle_stream_line(line_str: str, is_stderr: bool):
                        nonlocal stderr_password_detected, proxy_auth_pending
                        if is_stderr:
                            stderr_lines.append(line_str)
                            lower_line = line_str.lower()
                            if lower_line.startswith("authenticated to "):
                                proxy_auth_pending = False
                            elif is_proxy_auth_prompt(line_str):
                                proxy_auth_pending = True
                                self.stats.status = "Proxy Auth"
                                self.log_msg(
                                    "[bold yellow]ProxyCommand waiting for external/browser authentication.[/bold yellow]"
                                )
                            elif is_ssh_password_prompt(line_str):
                                stderr_password_detected = True
                                self.stats.status = "Password Required"
                                self.log_msg(
                                    "[bold yellow]SSH is asking for a password.[/bold yellow]"
                                )
                                self.log_msg(
                                    "[yellow]This may be because no SSH keys are loaded in ssh-agent.[/yellow]"
                                )
                                self.log_msg(
                                    "[yellow]Please check your SSH agent and keys, or try connecting manually.[/yellow]"
                                )
                        color = "red" if is_stderr else "dim white"
                        self.log_msg(f"[{color}][SSH] {line_str}[/{color}]")

                    async def read_stream(stream, is_stderr=False):
                        if stream is None:
                            return
                        buf = ""
                        last_partial_logged = ""
                        while True:
                            chunk = await stream.read(1024)
                            if not chunk:
                                rem = buf.strip()
                                if rem and rem != last_partial_logged:
                                    handle_stream_line(rem, is_stderr)
                                break
                            buf += chunk.decode(errors="replace")
                            while "\n" in buf or "\r" in buf:
                                idx_n = buf.find("\n")
                                idx_r = buf.find("\r")
                                if idx_n == -1:
                                    idx = idx_r
                                elif idx_r == -1:
                                    idx = idx_n
                                else:
                                    idx = min(idx_n, idx_r)
                                line_str = buf[:idx].strip()
                                buf = buf[idx + 1 :]
                                if line_str and line_str != last_partial_logged:
                                    handle_stream_line(line_str, is_stderr)
                                last_partial_logged = ""
                            partial = buf.strip()
                            if (
                                is_stderr
                                and partial
                                and partial != last_partial_logged
                                and (
                                    is_ssh_password_prompt(partial)
                                    or is_proxy_auth_prompt(partial)
                                )
                            ):
                                handle_stream_line(partial, is_stderr=True)
                                last_partial_logged = partial

                    stream_tasks = [
                        asyncio.create_task(
                            read_stream(self.ssh_process.stdout, False)
                        ),
                        asyncio.create_task(read_stream(self.ssh_process.stderr, True)),
                    ]

                    # Wait STABILITY_WAIT_SEC excluding time spent in interactive password/proxy auth prompts
                    stable_elapsed = 0.0
                    exited_early = self.ssh_process.returncode is not None
                    last_tick = time.monotonic()
                    while not exited_early:
                        now = time.monotonic()
                        dt = now - last_tick
                        last_tick = now
                        if self._auth_prompt_active or proxy_auth_pending:
                            stable_elapsed = 0.0
                        else:
                            stable_elapsed += dt
                            if stable_elapsed >= STABILITY_WAIT_SEC:
                                break
                        try:
                            await asyncio.wait_for(
                                self.ssh_process.wait(), timeout=0.05
                            )
                            exited_early = True
                            break
                        except asyncio.TimeoutError:
                            if self.ssh_process.returncode is not None:
                                exited_early = True
                                break

                    if exited_early:
                        if not self.intentional_restart and not self._auth_cancelled:
                            try:
                                await asyncio.wait_for(
                                    asyncio.gather(
                                        *stream_tasks, return_exceptions=True
                                    ),
                                    timeout=0.2,
                                )
                            except asyncio.TimeoutError:
                                pass

                        if self.intentional_restart:
                            self.log_msg(
                                "[dim]Process terminated for reconfiguration.[/dim]"
                            )
                            self.stats.record_disconnect()
                            continue

                        if self._auth_cancelled:
                            self.log_msg(
                                "[bold yellow]Authentication cancelled by user. Press 'r' to retry or 'c' to edit config.[/bold yellow]"
                            )
                            self.stats.status = "Auth Cancelled"
                            try:
                                self.sleep_task = asyncio.create_task(asyncio.Future())
                                await self.sleep_task
                            except asyncio.CancelledError:
                                pass
                            continue

                        # Connection failed before stability window completed
                        status_str, err_msg, hint = classify_ssh_failure(
                            stderr_lines,
                            stderr_password_detected=stderr_password_detected,
                            resolved_ssh=self.resolved_ssh,
                        )

                        self.log_msg(
                            f"[bold red]{err_msg} (Code: {self.ssh_process.returncode})[/bold red]"
                        )
                        if hint:
                            self.log_msg(f"[yellow]{hint}[/yellow]")

                        if status_str == "All Ports In Use":
                            for srv in self.active_services_to_bind:
                                self.service_statuses[srv] = "failed_port_in_use"
                            self.update_services_table()

                        self.stats.status = status_str
                        self.handle_connection_failure()

                    elif stderr_password_detected:
                        # SSH printed a prompt on stderr and stayed stalled without askpass completion
                        self.log_msg(
                            "[bold red]SSH stalled waiting for a password prompt on stderr.[/bold red]"
                        )
                        self.stats.status = "Password Required"
                        if self.ssh_process:
                            try:
                                self.ssh_process.terminate()
                            except Exception:
                                pass
                        self.handle_connection_failure()

                    else:
                        # Connection survived the stability window.
                        failed_set = detect_failed_forward_services(
                            stderr_lines,
                            self.active_services_to_bind,
                            self.config["services"],
                        )

                        # If some ports were already in use
                        if failed_set and not self.is_partial_mode:
                            failed_services = []
                            successful_services = []

                            for srv_name in self.active_services_to_bind:
                                if srv_name in failed_set:
                                    self.service_statuses[srv_name] = (
                                        "failed_port_in_use"
                                    )
                                    failed_services.append(srv_name)
                                else:
                                    self.service_statuses[srv_name] = "active"
                                    successful_services.append(srv_name)

                            self.update_services_table()

                            # Terminate the first SSH command so we can redo it with only working ports
                            if self.ssh_process:
                                try:
                                    self.ssh_process.terminate()
                                except Exception:
                                    pass

                            if not successful_services:
                                self.log_msg(
                                    "[bold red]All requested ports are already in use.[/bold red]"
                                )
                                self.stats.status = "All Ports In Use"
                                self.handle_connection_failure()
                            else:
                                self.log_msg(
                                    f"[bold yellow]Partial connection established. Active: {successful_services}. Blocked: {failed_services}[/bold yellow]"
                                )

                                for line in stderr_lines:
                                    lower_l = line.lower()
                                    if (
                                        "address already in use" in lower_l
                                        or "cannot listen to port" in lower_l
                                        or "remote port forwarding failed" in lower_l
                                    ):
                                        self.log_msg(
                                            f"[bold red][PORT WARNING] {line}[/bold red]"
                                        )

                                notify_details = ", ".join(
                                    [
                                        f"{srv} ({self.config['services'][srv]['remote_port'] if self.config['services'][srv].get('type') == 'remote' else self.config['services'][srv]['local_port']})"
                                        for srv in failed_services
                                    ]
                                )
                                send_os_notification(
                                    "LLM Tunnel Degraded",
                                    f"Connected partially. Ports already in use: {notify_details}",
                                )

                                self.is_partial_mode = True
                                self.active_services_to_bind = successful_services
                                self.stats.status = "DEGRADED"
                                continue
                        else:
                            for srv_name in self.active_services_to_bind:
                                self.service_statuses[srv_name] = "active"
                            self.update_services_table()

                            if self.is_partial_mode:
                                self.stats.status = "DEGRADED"
                                self.log_msg(
                                    "[bold yellow]Degraded Link Established. Working ports are hot.[/bold yellow]"
                                )
                            else:
                                self.stats.status = "CONNECTED"
                                self.log_msg(
                                    "[bold green]Link Established. Ports are hot.[/bold green]"
                                )

                            self.handle_connection_success()

                            await self.ssh_process.wait()

                            if self.intentional_restart:
                                self.log_msg(
                                    "[dim]Process terminated for reconfiguration.[/dim]"
                                )
                                self.stats.record_disconnect()
                                continue

                            self.log_msg(
                                f"[red]Link dropped unexpectedly (Code: {self.ssh_process.returncode})[/red]"
                            )
                            self.handle_connection_failure()
                            self.is_partial_mode = False

                except Exception as e:
                    self.log_msg(f"[bold red]System Exception:[/bold red] {str(e)}")
                    self.handle_connection_failure()
                    self.is_partial_mode = False
                finally:
                    for t in stream_tasks:
                        if not t.done():
                            t.cancel()
                    if stream_tasks:
                        await asyncio.gather(*stream_tasks, return_exceptions=True)

                if self.is_partial_mode:
                    self.stats.status = "DEGRADED"
                elif not self.stats.status or self.stats.status in [
                    "CONNECTED",
                    "Routing...",
                ]:
                    self.stats.status = "Host Unreachable"

                self.log_msg(
                    f"[yellow]Awaiting {int(RECONNECT_DELAY_SEC)} seconds before attempting reconnect...[/yellow]"
                )

                try:
                    self.sleep_task = asyncio.create_task(
                        asyncio.sleep(RECONNECT_DELAY_SEC)
                    )
                    await self.sleep_task
                except asyncio.CancelledError:
                    pass
        finally:
            self.running = False
            if self._askpass_server is not None:
                try:
                    self._askpass_server.close()
                except Exception:
                    pass
                self._askpass_server = None
            if self._askpass_dir and os.path.exists(self._askpass_dir):
                shutil.rmtree(self._askpass_dir, ignore_errors=True)
                self._askpass_dir = None
            if self.ssh_process:
                try:
                    self.ssh_process.terminate()
                except Exception:
                    pass


def main():
    check_config_on_startup()
    app = TunnelApp()
    try:
        app.run()
    finally:
        if hasattr(app, "ssh_process") and app.ssh_process:
            try:
                app.ssh_process.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()

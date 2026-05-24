#!/usr/bin/env python3
import os
import json
import time
import asyncio
import subprocess
import shutil
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static, DataTable, RichLog, Label
from textual.screen import ModalScreen
from textual import work, events

from rich.table import Table

CONFIG_PATH = os.path.expanduser("~/.config/aerotunnel/config.json")

# Version Information
MAJOR = 0
MINOR = 2
PATCH = 3
VERSION = f"{MAJOR}.{MINOR}.{PATCH}"
DATE_RELEASED = "2026-05-24"


def load_json_with_comments(filepath):
    import re

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

    return json.loads(clean_content)


def create_boilerplate_config(filepath):
    boilerplate = """// ============================================================================
// ▲ AEROTUNNEL CONFIGURATION TEMPLATE
// ============================================================================
// Use this file to configure your local SSH tunnel to your remote LLM or services.
// Comments (single-line // or #, block /* */) are fully supported!
// ============================================================================
{
  // The hostname or IP address of your remote server (e.g., "ssh.someserver.com" or "192.168.1.100")
  "remote_host": "127.0.0.1",

  // The SSH port of your remote server (default is 22)
  "remote_port": 22,

  // The SSH username for logging into your remote server
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
        create_boilerplate_config(CONFIG_PATH)
        time.sleep(1.5)

        while True:
            print(f"\nOpening {CONFIG_PATH} in your text editor...")
            time.sleep(1.0)
            open_editor(CONFIG_PATH)

            try:
                load_json_with_comments(CONFIG_PATH)
                print("\n\033[1;32m[✓] Configuration parsed successfully!\033[0m")
                time.sleep(1.0)
                break
            except Exception as e:
                print(f"\n\033[1;31m[!] Error parsing configuration file:\033[0m {e}")
                ans = (
                    input(
                        "Would you like to reopen the editor to fix the error? [Y/n]: "
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
    return f"{int(seconds)}s"  # <--- Changed this line from {seconds:.1f}s


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
        help_text = f"""[bold cyan]▲ LOCAL LLM TUNNEL // HELP & CONTROLS[/bold cyan]

[bold]Keyboard Shortcuts:[/bold]
• [yellow]b[/yellow] : Configure Network Bindings
  [dim]Selectively expose services to your local network (0.0.0.0) or restrict them to your device only (127.0.0.1).[/dim]

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
                "[bold cyan]Configure Local Network Bindings[/bold cyan]\n[dim]Up/Down to select • Space to toggle • Enter to apply[/dim]",
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
        grid-columns: 1fr 1fr;
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
    
    LogScreen, BindingModal, HelpScreen {
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
    """

    BINDINGS = [
        ("h", "show_help", "Help"),
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
        self.config = self.load_config()
        self.running = True
        self.ssh_process = None
        self.intentional_restart = False
        self.sleep_task = None
        self.log_history = []

        self.service_bindings = {}
        self.service_statuses = {}
        self.expanded_services = set()
        self.is_partial_mode = False
        self.active_services_to_bind = []
        if self.config:
            for srv, srv_conf in self.config.get("services", {}).items():
                self.service_bindings[srv] = srv_conf.get("bind_address", "127.0.0.1")
                self.service_statuses[srv] = "pending"

    def load_config(self):
        try:
            return load_json_with_comments(CONFIG_PATH)
        except Exception:
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
        self.set_interval(1.0, self.update_ui)
        self.run_ssh_loop()

    def on_unmount(self) -> None:
        self.running = False
        if self.ssh_process:
            try:
                self.ssh_process.terminate()
            except Exception:
                pass

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width < 80:
            self.screen.add_class("mobile_layout")
        else:
            self.screen.remove_class("mobile_layout")
        self.update_services_table()

    def update_services_table(self):
        table = self.query_one("#services_table", DataTable)

        # Remember current cursor coordinate to restore focus/scroll position!
        try:
            old_coordinate = table.cursor_coordinate
            old_row_key = table.coordinate_to_cell_key(old_coordinate).row_key.value
        except Exception:
            old_row_key = None

        table.clear(columns=True)

        is_mobile = self.screen.has_class("mobile_layout")
        if is_mobile:
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

                status_val = self.service_statuses.get(name, "pending")
                if status_val == "active":
                    status_display = "[bold green]✔️ ACTIVE[/]"
                elif status_val == "failed_port_in_use":
                    status_display = "[bold red]❌ PORT IN USE[/]"
                else:
                    status_display = "[bold yellow]⏳ PENDING[/]"

                if is_mobile:
                    if srv_type == "remote":
                        map_str = f"[bold cyan][R][/] {bind_display}:{listen_port} [yellow]↢[/] {listen_port}"
                    else:
                        map_str = f"[bold cyan][L][/] {bind_display}:{listen_port} [yellow]↣[/] {service_port}"

                    table.add_row(
                        name.upper().replace("_", "-"),
                        map_str,
                        status_display,
                        key=name,
                    )

                    if name in self.expanded_services:
                        type_str = (
                            "Remote Forwarding (-R)"
                            if srv_type == "remote"
                            else "Local Forwarding (-L)"
                        )
                        table.add_row(
                            "  [dim]↳ Type:[/dim]",
                            f"[cyan]{type_str}[/]",
                            "",
                            key=f"{name}_sub_type",
                        )
                        table.add_row(
                            "  [dim]↳ Listen:[/dim]",
                            f"[green]{bind_ip}:{listen_port}[/]",
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
                    flow_str = (
                        "[yellow]↢ [R] ↢[/]"
                        if srv_type == "remote"
                        else "[yellow]↣ [L] ↣[/]"
                    )

                    table.add_row(
                        name.upper().replace("_", "-"),
                        f"{bind_display}:{listen_port}",
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
                            f"[green]{bind_ip}:{listen_port}[/]",
                            "",
                            "",
                            "",
                            key=f"{name}_sub_listen",
                        )
                        table.add_row(
                            "  [dim]↳ Target:[/dim]",
                            f"[white]{service_host}:{service_port}[/]",
                            "",
                            "",
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
        row_key = event.row_key.value
        parent_service = None
        if row_key in self.config.get("services", {}):
            parent_service = row_key
        else:
            for srv in self.config.get("services", {}):
                if row_key.startswith(f"{srv}_sub_"):
                    parent_service = srv
                    break
        if parent_service:
            if parent_service in self.expanded_services:
                self.expanded_services.remove(parent_service)
            else:
                self.expanded_services.add(parent_service)
            self.update_services_table()

    def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            table = self.query_one("#services_table", DataTable)
            if table.has_focus:
                try:
                    coordinate = table.cursor_coordinate
                    row_key = table.coordinate_to_cell_key(coordinate).row_key.value
                    parent_service = None
                    if row_key in self.config.get("services", {}):
                        parent_service = row_key
                    else:
                        for srv in self.config.get("services", {}):
                            if row_key.startswith(f"{srv}_sub_"):
                                parent_service = srv
                                break
                    if parent_service:
                        if parent_service in self.expanded_services:
                            self.expanded_services.remove(parent_service)
                        else:
                            self.expanded_services.add(parent_service)
                        self.update_services_table()
                        event.prevent_default()
                except Exception:
                    pass

    def action_show_help(self) -> None:
        self.push_screen("help_screen")

    def action_toggle_log(self) -> None:
        self.push_screen("log_screen")

    def action_open_shell(self) -> None:
        if not self.config:
            self.log_msg("[red]Cannot open shell: No config loaded.[/red]")
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
                "-p",
                str(self.config["remote_port"]),
                f"{self.config['remote_user']}@{self.config['remote_host']}",
            ]

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

        new_config = self.load_config()
        if new_config:
            self.config = new_config
            self.is_partial_mode = False

            # Re-initialize and update service bindings & statuses
            new_bindings = {}
            new_statuses = {}
            for srv, srv_conf in self.config.get("services", {}).items():
                new_bindings[srv] = self.service_bindings.get(
                    srv, srv_conf.get("bind_address", "127.0.0.1")
                )
                new_statuses[srv] = "pending"
            self.service_bindings = new_bindings
            self.service_statuses = new_statuses
            self.expanded_services = {
                s
                for s in self.expanded_services
                if s in self.config.get("services", {})
            }

            self.update_services_table()
            self.log_msg(
                "[yellow]Configuration reloaded. Restarting SSH tunnel...[/yellow]"
            )
            self.restart_tunnel()
        else:
            self.log_msg(
                "[bold red]Failed to reload config: invalid JSON or missing file.[/bold red]"
            )

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
        if self.config:
            remote_host = f"[bold cyan]{self.config.get('remote_user', '')}@{self.config.get('remote_host', '')}[/]"
            remote_port = f"[bold cyan]{self.config.get('remote_port', '')}[/]"

        table.add_row("Remote Host:", remote_host, "SSH Port:", remote_port)
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

    @work(exclusive=True)
    async def run_ssh_loop(self):
        if not self.config:
            self.log_msg(f"[red]Error: Could not load config at {CONFIG_PATH}[/red]")
            return

        self.log_msg(
            "[bold cyan]System Initialized. Starting routing loop...[/bold cyan]"
        )

        try:
            while self.running:
                self.intentional_restart = False

                # If not in partial mode, this is a fresh attempt to bind all configured services
                if not self.is_partial_mode:
                    self.stats.status = "Routing..."
                    for srv in self.config["services"].keys():
                        self.service_statuses[srv] = "pending"
                    self.active_services_to_bind = list(self.config["services"].keys())
                    self.update_services_table()

                self.stats.attempts += 1

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
                    "-p",
                    str(self.config["remote_port"]),
                ]

                for srv_name in self.active_services_to_bind:
                    srv_conf = self.config["services"][srv_name]
                    srv_type = srv_conf.get("type", "local")
                    service_host = srv_conf.get("service_host", "127.0.0.1")
                    service_port = srv_conf["service_port"]
                    bind_ip = self.service_bindings.get(
                        srv_name, srv_conf.get("bind_address", "127.0.0.1")
                    )

                    if srv_type == "remote":
                        remote_port = srv_conf["remote_port"]
                        cmd.extend(
                            [
                                "-R",
                                f"{bind_ip}:{remote_port}:{service_host}:{service_port}",
                            ]
                        )
                    else:
                        local_port = srv_conf["local_port"]
                        cmd.extend(
                            [
                                "-L",
                                f"{bind_ip}:{local_port}:{service_host}:{service_port}",
                            ]
                        )

                cmd.append(f"{self.config['remote_user']}@{self.config['remote_host']}")
                self.log_msg(f"[yellow]Spawning Subprocess:[/yellow] {' '.join(cmd)}")

                try:
                    self.ssh_process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    stderr_lines = []

                    async def read_stream(stream, is_stderr=False):
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            line_str = line.decode().strip()
                            if is_stderr:
                                stderr_lines.append(line_str)
                            color = "red" if is_stderr else "dim white"
                            self.log_msg(f"[{color}][SSH] {line_str}[/{color}]")

                    asyncio.create_task(read_stream(self.ssh_process.stdout))
                    asyncio.create_task(read_stream(self.ssh_process.stderr, True))

                    # We give SSH exactly 6 seconds to prove it's stable.
                    try:
                        await asyncio.wait_for(self.ssh_process.wait(), timeout=6.0)

                        # If it exits before 6 seconds, the connection failed early.
                        err_msg = "Connection rejected or unreachable"
                        status_str = "Host Unreachable"

                        stderr_content = "\n".join(stderr_lines).lower()
                        if "permission denied" in stderr_content:
                            status_str = "Auth Failure"
                            err_msg = "Permission denied (auth failure)"
                        elif "connection refused" in stderr_content:
                            status_str = "Conn Refused"
                            err_msg = "Connection refused by remote host"
                        elif (
                            "no route to host" in stderr_content
                            or "destination host unreachable" in stderr_content
                            or "network is unreachable" in stderr_content
                        ):
                            status_str = "Host Unreachable"
                            err_msg = "Host unreachable / No route to host"
                        elif "timed out" in stderr_content:
                            status_str = "Timed Out"
                            err_msg = "Connection timed out"
                        elif (
                            "could not resolve hostname" in stderr_content
                            or "name or service not known" in stderr_content
                        ):
                            status_str = "DNS Failure"
                            err_msg = "Could not resolve remote hostname"
                        elif (
                            "address already in use" in stderr_content
                            or "cannot listen to port" in stderr_content
                        ):
                            status_str = "All Ports In Use"
                            err_msg = "All requested ports are already in use"

                        self.log_msg(
                            f"[bold red]{err_msg} (Code: {self.ssh_process.returncode})[/bold red]"
                        )

                        # Mark all requested services as blocked if we got All Ports In Use
                        if (
                            status_str == "All Ports In Use"
                            or "address already in use" in stderr_content
                        ):
                            for srv in self.active_services_to_bind:
                                self.service_statuses[srv] = "failed_port_in_use"
                            self.update_services_table()

                        self.stats.status = status_str
                        self.handle_connection_failure()

                    except asyncio.TimeoutError:
                        # Connection survived the 6-second timeout.
                        # Parse stderr to see if any requested ports failed to bind.
                        listen_port_to_service = {}
                        for srv in self.active_services_to_bind:
                            srv_conf = self.config["services"][srv]
                            srv_type = srv_conf.get("type", "local")
                            if srv_type == "remote":
                                listen_port = srv_conf["remote_port"]
                            else:
                                listen_port = srv_conf["local_port"]
                            listen_port_to_service[listen_port] = srv

                        failed_ports = set()
                        import re

                        for line in stderr_lines:
                            if (
                                "address already in use" in line.lower()
                                or "cannot listen to port" in line.lower()
                            ):
                                digits = re.findall(r"\b\d{2,5}\b", line)
                                for d in digits:
                                    p = int(d)
                                    if p in listen_port_to_service:
                                        failed_ports.add(p)

                        # If some ports were already in use
                        if failed_ports and not self.is_partial_mode:
                            failed_services = []
                            successful_services = []

                            for srv_name in self.active_services_to_bind:
                                srv_conf = self.config["services"][srv_name]
                                srv_type = srv_conf.get("type", "local")
                                p = (
                                    srv_conf["remote_port"]
                                    if srv_type == "remote"
                                    else srv_conf["local_port"]
                                )
                                if p in failed_ports:
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
                                # All ports were blocked
                                self.log_msg(
                                    "[bold red]All requested ports are already in use.[/bold red]"
                                )
                                self.stats.status = "All Ports In Use"
                                self.handle_connection_failure()
                            else:
                                # Succeeded for some, failed for others!
                                self.log_msg(
                                    f"[bold yellow]Partial connection established. Active: {successful_services}. Blocked: {failed_services}[/bold yellow]"
                                )

                                # Log the warning lines verbatim
                                for line in stderr_lines:
                                    if (
                                        "address already in use" in line.lower()
                                        or "cannot listen to port" in line.lower()
                                    ):
                                        self.log_msg(
                                            f"[bold red][PORT WARNING] {line}[/bold red]"
                                        )

                                # Send desktop notification
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

                                # Switch to partial mode and continue to restart the connection with only successful ports
                                self.is_partial_mode = True
                                self.active_services_to_bind = successful_services
                                self.stats.status = "DEGRADED"
                                continue
                        else:
                            # Succeeded completely for all requested ports
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

                            # Wait indefinitely for the session to organically drop
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
                            self.is_partial_mode = False  # Reset on drop

                except Exception as e:
                    self.log_msg(f"[bold red]System Exception:[/bold red] {str(e)}")
                    self.handle_connection_failure()
                    self.is_partial_mode = False

                if self.is_partial_mode:
                    self.stats.status = "DEGRADED"
                elif not self.stats.status or self.stats.status in [
                    "CONNECTED",
                    "Routing...",
                ]:
                    self.stats.status = "Host Unreachable"

                self.log_msg(
                    "[yellow]Awaiting 5 seconds before attempting reconnect...[/yellow]"
                )

                try:
                    self.sleep_task = asyncio.create_task(asyncio.sleep(5))
                    await self.sleep_task
                except asyncio.CancelledError:
                    pass
        finally:
            self.running = False
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

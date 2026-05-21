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

CONFIG_PATH = os.path.expanduser("~/.config/localai_tunnel.json")

# --- CONFIGURABLE NOTIFICATION SETTINGS ---
NOTIFICATION_FAIL_THRESHOLD = 5
NOTIFICATION_COOLDOWN_SEC = 3600  # 1 hour

def send_os_notification(title: str, message: str):
    if "com.termux" in os.environ.get("PREFIX", ""):
        if shutil.which("termux-notification"):
            subprocess.run(["termux-notification", "-t", title, "-c", message], check=False)
    else:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False)
def fmt_time(seconds: float) -> str:
    """Helper to convert raw seconds into a readable Xh Ym Zs format."""
    if seconds < 0: return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}h {m}m {s}s"
    if m > 0: return f"{m}m {s}s"
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
        help_text = """[bold cyan]▲ LOCAL LLM TUNNEL // HELP & CONTROLS[/bold cyan]

[bold]Keyboard Shortcuts:[/bold]
• [yellow]b[/yellow] : Configure Network Bindings
  [dim]Selectively expose services to your local network (0.0.0.0) or restrict them to your device only (127.0.0.1).[/dim]

• [yellow]l[/yellow] : View System Logs
  [dim]Opens the raw SSH output buffer. Essential for debugging connection drops or auth failures.[/dim]

• [yellow]s[/yellow] : Interactive Shell
  [dim]Suspends the UI and drops you into a native terminal session on the remote server.[/dim]
  [bold red]CRITICAL:[/bold red] Type 'exit' or press [bold]Ctrl+D[/bold] in the remote shell to return to this dashboard!

• [yellow]h[/yellow] : Show this Help dialog

• [yellow]q[/yellow] : Quit application

[dim]Press ESC or 'h' to close this dialog.[/dim]"""
        yield Container(Static(help_text, markup=True), id="help_panel")

# --- BINDING CONFIGURATION MODAL ---
class BindingModal(ModalScreen[dict]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("space", "toggle", "Toggle 0.0.0.0 / 127.0.0.1")
    ]

    def __init__(self, current_bindings):
        super().__init__()
        self.working_bindings = current_bindings.copy()

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold cyan]Configure Local Network Bindings[/bold cyan]\n[dim]Up/Down to select • Space to toggle • Enter to apply[/dim]", id="dialog_title"),
            DataTable(id="binding_table"),
            id="dialog"
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_column("Service", key="srv")
        table.add_column("Bind Address", key="ip")
        
        for srv, ip in self.working_bindings.items():
            self._add_or_update_row(table, srv, ip)

    def _add_or_update_row(self, table, srv, ip):
        ip_display = f"[bold white on red] 0.0.0.0 [/]" if ip == "0.0.0.0" else f"[bold green]{ip}[/]"
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
        if table.row_count == 0: return
        
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
    BINDINGS = [("l", "app.pop_screen", "Close Log"), ("escape", "app.pop_screen", "Close")]
    
    def compose(self) -> ComposeResult:
        yield Container(RichLog(id="app_log", highlight=True, markup=True), id="log_panel")

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
        ("q", "quit", "Quit")
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
        if self.config:
            for srv in self.config.get("services", {}).keys():
                self.service_bindings[srv] = "127.0.0.1"

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        stats_container = Container(Static(id="stats_text"), id="stats_container")
        stats_container.border_title = "Session Diagnostics"
        yield stats_container
        
        srv_container = Container(DataTable(id="services_table"), id="services_container")
        srv_container.border_title = "Active Port Mappings"
        yield srv_container
        
        yield Footer()

    def on_mount(self) -> None:
        self.install_screen(LogScreen(), name="log_screen")
        self.install_screen(HelpScreen(), name="help_screen")
        self.title = "▲ LOCAL LLM TUNNEL CONTROL ENGINE"
        
        self.update_services_table()
        self.set_interval(1.0, self.update_ui)
        self.run_ssh_loop()

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width < 80:
            self.screen.add_class("mobile_layout")
        else:
            self.screen.remove_class("mobile_layout")

    def update_services_table(self):
        table = self.query_one("#services_table", DataTable)
        table.clear(columns=True)
        table.add_columns("Service", "Local Map", "Target Port")
        
        if self.config:
            for name, ports in self.config.get("services", {}).items():
                bind_ip = self.service_bindings.get(name, "127.0.0.1")
                bind_display = f"[bold white on red] 0.0.0.0 [/]" if bind_ip == "0.0.0.0" else f"[bold green]{bind_ip}[/]"
                table.add_row(
                    name.upper().replace("_", "-"), 
                    f"{bind_display}:{ports['local']}", 
                    str(ports['remote'])
                )

    def action_show_help(self) -> None:
        self.push_screen("help_screen")

    def action_toggle_log(self) -> None:
        self.push_screen("log_screen")
        
    def action_open_shell(self) -> None:
        if not self.config:
            self.log_msg("[red]Cannot open shell: No config loaded.[/red]")
            return

        with self.suspend():
            os.system('cls' if os.name == 'nt' else 'clear')
            print("\n\033[1;36m▲ LOCAL LLM TUNNEL // INTERACTIVE SHELL\033[0m")
            print("\033[1;31m============================================================\033[0m")
            print("\033[1;33m REMINDER: Press CTRL+D or type 'exit' to return to the TUI \033[0m")
            print("\033[1;31m============================================================\033[0m\n")
            print("Handshaking with remote server...")
            
            time.sleep(2.5) 
            
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(self.config["remote_port"]),
                f"{self.config['remote_user']}@{self.config['remote_ip']}"
            ]
            
            try:
                subprocess.run(cmd)
            except Exception as e:
                print(f"Shell error: {e}")
                
            time.sleep(0.5) 

    @work
    async def action_configure_bindings(self) -> None:
        new_bindings = await self.push_screen_wait(BindingModal(self.service_bindings))
        if new_bindings:
            self.service_bindings = new_bindings
            self.update_services_table()
            self.log_msg("[yellow]Network bindings updated. Restarting SSH tunnel...[/yellow]")
            self.restart_tunnel()

    def restart_tunnel(self):
        self.intentional_restart = True
        if self.ssh_process:
            try:
                self.ssh_process.terminate()
            except Exception:
                pass
        if self.sleep_task:
            self.sleep_task.cancel()

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
        active_session = time.time() - self.stats.current_session_start if is_connected else 0.0
        active_outage = time.time() - self.stats.current_outage_start if not is_connected else 0.0
        
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
        
        table.add_row("Engine Runtime:", fmt_time(app_runtime), "Uptime Ratio:", f"{uptime_pct:.3f}%")
        table.add_row("Total Uptime:", fmt_time(total_up), "Total Downtime:", fmt_time(total_down))
        table.add_row("Current Link:", fmt_time(active_session), "Longest Link:", fmt_time(max(self.stats.max_duration, active_session)))
        table.add_row("Current Outage:", fmt_time(active_outage), "Longest Outage:", fmt_time(max_outage_display))
        table.add_row("MTBF (Avg Up):", fmt_time(mtbf), "MTTR (Avg Down):", fmt_time(mttr))
        
        status_ui = "[bold green]CONNECTED[/]" if is_connected else f"[bold red]{self.stats.status}[/]"
        table.add_row("Attempts/Drops:", f"{self.stats.attempts} / {self.stats.errors}", "Status:", status_ui)

        self.query_one("#stats_text", Static).update(table)

    def handle_connection_failure(self):
        self.stats.record_disconnect()
        self.stats.consecutive_errors += 1
        self.stats.errors += 1
        
        if self.stats.consecutive_errors >= NOTIFICATION_FAIL_THRESHOLD:
            now = time.time()
            if not self.stats.in_error_state or (now - self.stats.last_error_notify_time) > NOTIFICATION_COOLDOWN_SEC:
                send_os_notification(
                    "LLM Tunnel Down", 
                    f"Connection failed {self.stats.consecutive_errors} times in a row."
                )
                self.stats.last_error_notify_time = now
                self.stats.in_error_state = True

    def handle_connection_success(self):
        self.stats.record_connect()
        if self.stats.in_error_state:
            send_os_notification(
                "LLM Tunnel Restored", 
                "Connection to remote LLM server re-established."
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

        self.log_msg("[bold cyan]System Initialized. Starting routing loop...[/bold cyan]")

        while self.running:
            self.intentional_restart = False
            self.stats.attempts += 1
            self.stats.status = "Routing..."
            
            cmd = [
                "ssh", "-N",
                "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=2", 
                "-o", "ConnectTimeout=5",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(self.config["remote_port"])
            ]
            
            for srv_name, ports in self.config["services"].items():
                bind_ip = self.service_bindings.get(srv_name, "127.0.0.1")
                cmd.extend(["-L", f"{bind_ip}:{ports['local']}:127.0.0.1:{ports['remote']}"])
                
            cmd.append(f"{self.config['remote_user']}@{self.config['remote_ip']}")
            self.log_msg(f"[yellow]Spawning Subprocess:[/yellow] {' '.join(cmd)}")
            
            try:
                self.ssh_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                async def read_stream(stream, is_stderr=False):
                    while True:
                        line = await stream.readline()
                        if not line: break
                        color = "red" if is_stderr else "dim white"
                        self.log_msg(f"[{color}][SSH] {line.decode().strip()}[/{color}]")

                asyncio.create_task(read_stream(self.ssh_process.stdout))
                asyncio.create_task(read_stream(self.ssh_process.stderr, True))
                
                # We give SSH exactly 6 seconds to prove it's stable.
                try:
                    await asyncio.wait_for(self.ssh_process.wait(), timeout=6.0)
                    # If it exits before 6 seconds, the connection failed early.
                    self.log_msg(f"[bold red]Connection rejected or unreachable (Code: {self.ssh_process.returncode})[/bold red]")
                    self.handle_connection_failure()
                    
                except asyncio.TimeoutError:
                    # Connection survived the 6-second gauntlet. It is legitimate.
                    self.log_msg("[bold green]Link Established. Ports are hot.[/bold green]")
                    self.handle_connection_success()
                    
                    # Wait indefinitely for the session to organically drop
                    await self.ssh_process.wait()
                    
                    if self.intentional_restart:
                        self.log_msg("[dim]Process terminated for reconfiguration.[/dim]")
                        self.stats.record_disconnect()
                        continue
                        
                    self.log_msg(f"[red]Link dropped unexpectedly (Code: {self.ssh_process.returncode})[/red]")
                    self.handle_connection_failure()
                    
            except Exception as e:
                self.log_msg(f"[bold red]System Exception:[/bold red] {str(e)}")
                self.handle_connection_failure()
                
            self.stats.status = "Host Unreachable"
            self.log_msg("[yellow]Awaiting 5 seconds before attempting reconnect...[/yellow]")
            
            try:
                self.sleep_task = asyncio.create_task(asyncio.sleep(5))
                await self.sleep_task
            except asyncio.CancelledError:
                pass

if __name__ == "__main__":
    app = TunnelApp()
    app.run()

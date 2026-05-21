#!/usr/bin/env python3
import os
import json
import time
import asyncio
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static, DataTable, RichLog, Label
from textual.screen import ModalScreen
from textual import work, events

from rich.table import Table

CONFIG_PATH = os.path.expanduser("~/.config/localai_tunnel.json")

class TunnelStats:
    def __init__(self):
        self.attempts = 0
        self.success_count = 0
        self.errors = 0
        self.first_success = "N/A"
        self.max_duration = 0.0
        self.total_duration = 0.0
        self.status = "Initializing"
        self.current_session_start = None

    def record_disconnect(self):
        if self.current_session_start:
            duration = time.time() - self.current_session_start
            self.total_duration += duration
            if duration > self.max_duration:
                self.max_duration = duration
        self.current_session_start = None

    @property
    def avg_duration(self):
        if self.success_count == 0: return 0.0
        active = (time.time() - self.current_session_start) if self.current_session_start else 0
        return (self.total_duration + active) / self.success_count

# --- BINDING CONFIGURATION MODAL ---
class BindingModal(ModalScreen[dict]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "apply", "Apply & Restart"),
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
            table.add_row(srv.upper().replace("_", "-"), ip, key=srv)

    def action_toggle(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0: return
        
        # Get the underlying service key from the cursor's row
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        srv = row_key.value
        
        # Toggle logic
        current_ip = self.working_bindings[srv]
        new_ip = "0.0.0.0" if current_ip == "127.0.0.1" else "127.0.0.1"
        self.working_bindings[srv] = new_ip
        
        # Visually update the table cell
        ip_display = f"[bold green]{new_ip}[/]" if new_ip == "0.0.0.0" else new_ip
        table.update_cell(row_key, "ip", ip_display)

    def action_apply(self) -> None:
        self.dismiss(self.working_bindings)

    def action_cancel(self) -> None:
        self.dismiss(None)

# --- LOGGING MODAL ---
class LogScreen(ModalScreen):
    BINDINGS = [("l", "app.pop_screen", "Close Log"), ("escape", "app.pop_screen", "Close")]
    def compose(self) -> ComposeResult:
        yield Container(RichLog(id="app_log", highlight=True, markup=True), id="log_panel")

# --- MAIN APP ---
class TunnelApp(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 1; /* Default: 2 columns, 1 row */
        grid-columns: 1fr 1fr;
        padding: 1;
    }
    
    /* Python on_resize hook will apply this class on narrow terminals */
    Screen.mobile_layout {
        grid-size: 1 2; /* Switch to: 1 column, 2 rows */
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
    
    LogScreen, BindingModal {
        align: center middle;
        background: $background 80%;
    }
    
    #log_panel {
        width: 90%; height: 90%;
        border: thick $primary; background: $surface;
    }
    
    #dialog {
        padding: 1 2;
        border: thick cyan; background: $surface;
        width: 60; height: auto;
    }
    
    #dialog_title { text-align: center; margin-bottom: 1; width: 100%; }
    """

    BINDINGS = [
        ("b", "configure_bindings", "Bindings"),
        ("l", "toggle_log", "Logs"),
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
        
        # Initialize default bindings to secure localhost
        self.service_bindings = {}
        if self.config:
            for srv in self.config.get("services", {}).keys():
                self.service_bindings[srv] = "127.0.0.1"

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
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
        self.title = "▲ LOCAL LLM TUNNEL CONTROL ENGINE"
        
        self.update_services_table()
        self.set_interval(0.1, self.update_ui)
        self.run_ssh_loop()

    def on_resize(self, event: events.Resize) -> None:
        # Dynamically toggle Textual CSS layout on resize instead of @media
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
                bind_display = f"[bold green]{bind_ip}[/]" if bind_ip == "0.0.0.0" else bind_ip
                table.add_row(
                    name.upper().replace("_", "-"), 
                    f"{bind_display}:{ports['local']}", 
                    str(ports['remote'])
                )

    def action_toggle_log(self) -> None:
        self.push_screen("log_screen")

    @work
    async def action_configure_bindings(self) -> None:
        # Wait for user to interact with the modal
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
        log_screen = self.screens.get("log_screen")
        if log_screen:
            logger = log_screen.query_one(RichLog)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.call_from_thread(logger.write, f"[dim]{timestamp}[/dim] {msg}")

    def update_ui(self):
        is_connected = self.stats.current_session_start is not None
        table = Table.grid(padding=(0, 2, 0, 0))
        table.add_column(style="bold magenta", justify="right")
        table.add_column(style="white")
        table.add_column(style="bold magenta", justify="right")
        table.add_column(style="white")
        
        table.add_row("Attempts:", str(self.stats.attempts), "Successes:", str(self.stats.success_count))
        table.add_row("Errors/Drops:", str(self.stats.errors), "First Link:", self.stats.first_success)
        
        curr_dur = time.time() - self.stats.current_session_start if is_connected else 0.0
        table.add_row("Current Time:", f"{curr_dur:.1f}s", "Max Time:", f"{self.stats.max_duration:.1f}s")
        table.add_row("Avg Time:", f"{self.stats.avg_duration:.1f}s", "Status:", f"[bold green]CONNECTED[/]" if is_connected else f"[bold red]{self.stats.status}[/]")

        self.query_one("#stats_text", Static).update(table)

    @work(exclusive=True) # Runs on main asyncio loop
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
                "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(self.config["remote_port"])
            ]
            
            # Map ports dynamically based on user's active bindings
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
                
                await asyncio.sleep(1.5)
                
                if self.ssh_process.returncode is not None:
                    self.log_msg(f"[bold red]SSH process died immediately with code {self.ssh_process.returncode}[/bold red]")
                    self.stats.errors += 1
                else:
                    self.log_msg("[bold green]Link Established. Ports are hot.[/bold green]")
                    self.stats.success_count += 1
                    self.stats.current_session_start = time.time()
                    if self.stats.first_success == "N/A":
                        self.stats.first_success = datetime.now().strftime("%H:%M:%S")
                    
                    await self.ssh_process.wait()
                    
                    # Intercept intentional user restarts so they aren't tracked as errors
                    if self.intentional_restart:
                        self.log_msg("[dim]Process terminated for reconfiguration.[/dim]")
                        self.stats.record_disconnect()
                        continue
                        
                    self.log_msg(f"[red]Link dropped (Code: {self.ssh_process.returncode})[/red]")
                    self.stats.record_disconnect()
                    self.stats.errors += 1
                    
            except Exception as e:
                self.log_msg(f"[bold red]System Exception:[/bold red] {str(e)}")
                self.stats.record_disconnect()
                self.stats.errors += 1
                
            self.stats.status = "Host Unreachable"
            self.log_msg("[yellow]Awaiting 5 seconds before attempting reconnect...[/yellow]")
            
            try:
                self.sleep_task = asyncio.create_task(asyncio.sleep(5))
                await self.sleep_task
            except asyncio.CancelledError:
                # Caught an intentional restart during the sleep backoff
                pass

if __name__ == "__main__":
    app = TunnelApp()
    app.run()

#!/usr/bin/env python3
import os
import json
import time
import asyncio
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static, DataTable, RichLog
from textual.screen import ModalScreen, Screen
from textual import work

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

# --- LOGGING MODAL ---
class LogScreen(ModalScreen):
    BINDINGS = [("l", "app.pop_screen", "Close Log"), ("escape", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        log_widget = RichLog(id="app_log", highlight=True, markup=True)
        yield Container(log_widget, id="log_panel")

# --- MAIN APP ---
class TunnelApp(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 1; /* 2 columns, 1 row on large screens */
        grid-columns: 1fr 1fr;
        padding: 1;
    }
    
    #stats_container, #services_container {
        height: 100%;
        border: round cyan;
        padding: 1;
    }
    
    #stats_container {
        border: round magenta;
        border-title-color: magenta;
    }

    #services_container {
        border-title-color: cyan;
    }
    
    /* Responsive threshold for Termux/Mobile */
    @media (max-width: 80) {
        Screen {
            grid-size: 1 2; /* 1 column, 2 rows on small screens */
            grid-rows: 1fr 1fr;
        }
    }
    
    LogScreen {
        align: center middle;
        background: $background 80%; /* Dim background */
    }
    
    #log_panel {
        width: 90%;
        height: 90%;
        border: thick $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        ("l", "toggle_log", "Toggle Logs"),
        ("q", "quit", "Quit App")
    ]

    def __init__(self):
        super().__init__()
        self.stats = TunnelStats()
        self.config = self.load_config()
        self.running = True

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            return None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        # Stats pane
        self.stats_widget = Static(id="stats_text")
        stats_container = Container(self.stats_widget, id="stats_container")
        stats_container.border_title = "Session Diagnostics"
        yield stats_container
        
        # Services pane
        self.services_table = DataTable(id="services_table")
        srv_container = Container(self.services_table, id="services_container")
        srv_container.border_title = "Active Port Mappings"
        yield srv_container
        
        yield Footer()

    def on_mount(self) -> None:
        self.install_screen(LogScreen(), name="log_screen")
        self.title = "▲ LOCAL LLM TUNNEL CONTROL ENGINE"
        
        # Setup DataTable
        self.services_table.add_columns("Service", "Local Map", "Target Port")
        if self.config:
            for name, ports in self.config.get("services", {}).items():
                self.services_table.add_row(
                    name.upper().replace("_", "-"), 
                    f"0.0.0.0:{ports['local']}", 
                    str(ports['remote'])
                )

        # Start periodic UI refresh (10 times a second for smooth timers)
        self.set_interval(0.1, self.update_ui)
        
        # Start background SSH loop
        self.run_ssh_loop()

    def action_toggle_log(self) -> None:
        self.push_screen("log_screen")

    def log_msg(self, msg: str):
        # Push to the RichLog widget safely
        log_screen = self.screens.get("log_screen")
        if log_screen:
            logger = log_screen.query_one(RichLog)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.call_from_thread(logger.write, f"[dim]{timestamp}[/dim] {msg}")

    def update_ui(self):
        is_connected = self.stats.current_session_start is not None
        
        # Format the 4-column Stats Table
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

        self.stats_widget.update(table)

    @work(exclusive=True, thread=True)
    async def run_ssh_loop(self):
        if not self.config:
            self.log_msg(f"[red]Error: Could not load config at {CONFIG_PATH}[/red]")
            return

        self.log_msg("[bold cyan]System Initialized. Starting routing loop...[/bold cyan]")

        while self.running:
            self.stats.attempts += 1
            self.stats.status = "Routing..."
            
            cmd = [
                "ssh", "-N",
                "-o", "ServerAliveInterval=30",
                "-o", "ExitOnForwardFailure=yes", # CRITICAL: Crash if ports can't bind
                "-o", "StrictHostKeyChecking=accept-new", # CRITICAL: Don't hang on new WAN IPs
                "-p", str(self.config["remote_port"])
            ]
            
            # Map ports safely: bind local 0.0.0.0, point to remote 127.0.0.1
            for _, ports in self.config["services"].items():
                cmd.extend(["-L", f"0.0.0.0:{ports['local']}:127.0.0.1:{ports['remote']}"])
                
            cmd.append(f"{self.config['remote_user']}@{self.config['remote_ip']}")
            
            self.log_msg(f"[yellow]Spawning Subprocess:[/yellow] {' '.join(cmd)}")
            
            try:
                # Run async to capture stream outputs natively
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Async functions to read the SSH stdout/stderr streams to our logger
                async def read_stream(stream, is_stderr=False):
                    while True:
                        line = await stream.readline()
                        if not line: break
                        color = "red" if is_stderr else "dim white"
                        self.log_msg(f"[{color}][SSH] {line.decode().strip()}[/{color}]")

                asyncio.create_task(read_stream(process.stdout))
                asyncio.create_task(read_stream(process.stderr, True))
                
                # Check for instant failure
                await asyncio.sleep(1.5)
                if process.returncode is not None:
                    self.log_msg(f"[bold red]SSH process died immediately with code {process.returncode}[/bold red]")
                    self.stats.errors += 1
                else:
                    self.log_msg("[bold green]Link Established. Ports are hot.[/bold green]")
                    self.stats.success_count += 1
                    self.stats.current_session_start = time.time()
                    if self.stats.first_success == "N/A":
                        self.stats.first_success = datetime.now().strftime("%H:%M:%S")
                    
                    # Await process exit
                    await process.wait()
                    self.log_msg(f"[red]Link dropped (Code: {process.returncode})[/red]")
                    self.stats.record_disconnect()
                    self.stats.errors += 1
                    
            except Exception as e:
                self.log_msg(f"[bold red]System Exception:[/bold red] {str(e)}")
                self.stats.record_disconnect()
                self.stats.errors += 1
                
            self.stats.status = "Host Unreachable"
            self.log_msg("[yellow]Awaiting 5 seconds before attempting reconnect...[/yellow]")
            await asyncio.sleep(5)

if __name__ == "__main__":
    app = TunnelApp()
    app.run()


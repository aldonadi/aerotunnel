#!/usr/bin/env python3
import os
import json
import time
import subprocess
from datetime import datetime
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.table import Table
from rich.console import Console
from rich.text import Text

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
        if self.success_count == 0:
            return 0.0
        # If currently connected, include active session in average calculation
        active_extra = (time.time() - self.current_session_start) if self.current_session_start else 0
        return (self.total_duration + active_extra) / self.success_count

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        return f"Error loading config from {CONFIG_PATH}: {str(e)}"

def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1)
    )
    layout["body"].split_row(
        Layout(name="stats", ratio=1),
        Layout(name="services", ratio=1)
    )
    return layout

def generate_dashboard(layout, config, stats):
    # Header
    is_connected = stats.current_session_start is not None
    status_color = "bold green" if is_connected else "bold red"
    status_text = "CONNECTED" if is_connected else stats.status
    
    layout["header"].update(
        Panel(
            Text(f"▲ LOCAL LLM TUNNEL CONTROL ENGINE // STATUS: {status_text}", style=status_color, justify="center"),
            border_style="cyan"
        )
    )

    # Stats Panel
    stats_table = Table.grid(padding=(0, 1, 0, 1))
    stats_table.add_column(style="bold magenta", justify="right")
    stats_table.add_column(style="white")
    
    stats_table.add_row("Connection Attempts:", str(stats.attempts))
    stats_table.add_row("Successful Connections:", str(stats.success_count))
    stats_table.add_row("Dropped/Error Counts:", str(stats.errors))
    stats_table.add_row("First Success Timestamp:", str(stats.first_success))
    
    if is_connected:
        current_dur = time.time() - stats.current_session_start
        stats_table.add_row("Current Session Time:", f"{current_dur:.1f}s")
    else:
        stats_table.add_row("Current Session Time:", "0.0s")
        
    stats_table.add_row("Max Session Duration:", f"{stats.max_duration:.1f}s")
    stats_table.add_row("Avg Session Duration:", f"{stats.avg_duration:.1f}s")

    layout["stats"].update(Panel(stats_table, title="[bold telemetry]Session Diagnostics", border_style="magenta"))

    # Services Panel
    srv_table = Table(box=None, expand=True)
    srv_table.add_column("Service", style="bold cyan")
    srv_table.add_column("Local Map", style="green")
    srv_table.add_column("Target Port", style="dim white")

    if isinstance(config, dict):
        for name, ports in config.get("services", {}).items():
            srv_table.add_row(name.upper().replace("_", "-"), f"localhost:{ports['local']}", str(ports['remote']))
    
    layout["services"].update(Panel(srv_table, title="[bold network]Active Port Mappings", border_style="cyan"))

def build_ssh_cmd(config):
    cmd = ["ssh", "-N", "-o", "ServerAliveInterval=30", "-p", str(config["remote_port"])]
    
    # Map all services dynamically
    for _, ports in config["services"].items():
        cmd.extend(["-L", f"localhost:{ports['local']}:localhost:{ports['remote']}"])
        
    cmd.append(f"{config['remote_user']}@{config['remote_ip']}")
    return cmd

def main():
    console = Console()
    config = load_config()
    
    if isinstance(config, str):
        console.print(f"[bold red]{config}[/bold red]")
        return

    stats = TunnelStats()
    layout = make_layout()

    with Live(layout, refresh_per_second=4, screen=True) as live:
        while True:
            stats.attempts += 1
            stats.status = "Routing..."
            generate_dashboard(layout, config, stats)
            
            cmd = build_ssh_cmd(config)
            
            try:
                # Run the SSH command asynchronously
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # Tiny grace period to catch instant auth errors
                time.sleep(1.5)
                if process.poll() is not None:
                    raise subprocess.SubprocessError("SSH handshake rejected.")
                
                # Connection successful
                stats.success_count += 1
                if stats.first_success == "N/A":
                    stats.first_success = datetime.now().strftime("%H:%M:%S")
                stats.current_session_start = time.time()
                
                # Hold loop while process runs
                while process.poll() is None:
                    generate_dashboard(layout, config, stats)
                    time.sleep(0.25)
                    
                stats.record_disconnect()
                stats.errors += 1
                stats.status = "Link Dropped"
                
            except Exception:
                stats.record_disconnect()
                stats.errors += 1
                stats.status = "Host Unreachable"
                
            generate_dashboard(layout, config, stats)
            time.sleep(5)

if __name__ == "__main__":
    main()


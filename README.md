# ▲ AEROTUNNEL

> **A Premium, "Apple HIG Showcase" TUI Dashboard for SSH Port Forwarding and Live LLM Service Monitoring.**

Aerotunnel is a robust and elegant terminal user interface (TUI) designed to seamlessly manage, monitor, and route secure SSH port-forwarding tunnels for local LLM (Large Language Model) interfaces, remote APIs, database backends, or any microservices. 

Built on top of Python's modern async **Textual** framework and powered by **Rich**, Aerotunnel provides a real-time terminal operations center displaying link diagnostic SLAs (MTBF, MTTR, uptime ratio, active session duration, outage tracking) and interactive system controls.

---

## Key Features

- 📊 **Session Diagnostics Panel**: Tracks total uptime, link survival time, network outage tracking, drops, and industry-grade SLAs (Uptime Ratio, MTBF, MTTR).
- 🔗 **Active Port Mappings**: Tabular view of all forwarded services, local address maps, and remote destination ports.
- ⚙️ **On-the-fly Binding Customization**: Interactive modal (`b` key) to selectively bind individual services to local-only (`127.0.0.1`) or expose them to your local network (`0.0.0.0`) dynamically.
- 🛠️ **In-App Config Editor**: Zero-friction config management (`c` key) that pauses the UI, lets you edit the configuration file with your preferred text editor (using a robust fallback editor chain), validates JSON structure upon exiting, and restarts your tunnel.
- 💬 **Interactive Shell suspension**: Suspends the TUI and safely drops you into a native SSH interactive terminal session on the remote server (`s` key).
- 🔔 **OS Notifications**: Smart notification alerts (via `notify-send` on desktop Linux/BSD or `termux-notification` on Android Termux) that trigger on threshold-based disconnect outages.
- 📝 **Live Logs View**: Access raw SSH output logs buffer (`l` key) in real-time, built right into a dedicated overlay screen for debugging connection issues.
- 💬 **Comment-Supported Configuration**: Configuration files natively support single-line (`//`, `#`) and multi-line (`/* */`) comments for beautiful self-documenting parameters.

---

## Keyboard Shortcuts & Controls

When running Aerotunnel, control your operations space with the following global hotkeys:

| Key | Action | Description |
|:---:|:---|:---|
| `h` | **Show Help** | Opens a stunning modal dialog summarizing hotkeys and functions. |
| `b` | **Network Bindings** | Open a table-modal to toggle service bindings between local (`127.0.0.1`) and network-wide (`0.0.0.0`) with `Space`. |
| `l` | **View System Logs** | Opens the raw SSH output and connection lifecycle logs stream. |
| `s` | **Interactive Shell** | Suspends TUI, opens an active SSH shell session on the host. Exit shell (`exit` or `Ctrl+D`) to return to the TUI. |
| `c` | **Edit Configuration** | Suspends TUI, opens the active configuration file in your terminal's preferred editor. Reloads and restarts tunnel on exit. |
| `q` | **Quit Application** | Gracefully cleans up subprocesses, terminates tunnels, and exits. |

---

## Installation & Setup

### Prerequisites

- **Python**: `>= 3.12`
- **SSH Client**: `ssh` executable present in your system's PATH.
- **Dependencies**: Managed via Python dependencies (e.g., `textual`).

### Quick Start

Initialize the application by running:

```bash
python aerotunnel.py
```

### Automatic Bootstrap & Configuration Setup

If no configuration file exists at `~/.config/aerotunnel/config.json` when the application starts:
1. Aerotunnel will automatically create the parent directory and output a detailed, beautifully commented boilerplate config file.
2. It will chain-search for available terminal-based text editors using environment variables (`$VISUAL`, `$EDITOR`) and system commands (`nvim`, `vim`, `nano`, `pico`, `micro`, `emacs`, `vi`, `joe`, `ee`, `mcedit`, or `notepad.exe` on Windows).
3. The editor opens automatically to guide you through the initial configuration parameters.
4. Upon closing the editor, Aerotunnel validates the JSON structure. If syntax errors exist, it politely prompts to re-open the editor to correct the error, ensuring a smooth, crash-proof boot process.

---

## Configuration Reference

Your configuration is stored at `~/.config/aerotunnel/config.json`. Thanks to Aerotunnel's comment-stripping loader, you can comment this file freely!

Here is an example config:

```json
// ============================================================================
// ▲ AEROTUNNEL CONFIGURATION
// ============================================================================
{
  // The IP address or domain name of your remote server
  "remote_ip": "192.168.1.100",

  // The SSH username for logging into your remote server
  "remote_user": "ubuntu",

  // The SSH port of your remote server (default: 22)
  "remote_port": 22,

  // Services to forward through the secure SSH tunnel
  "services": {
    // Ollama AI Service (local 11434 -> remote 11434)
    "ollama": {
      "local": 11434,
      "remote": 11434
    },
    // Open WebUI / Gradio interface (local 3000 -> remote 8080)
    "webui": {
      "local": 3000,
      "remote": 8080
    }
  }
}
```

---

## Diagnostics & SLA SLA Metrics Explained

Aerotunnel calculates system-critical routing telemetry in real-time:
- **Engine Runtime**: Total elapsed time since the TUI was launched.
- **Uptime Ratio**: Percentage of engine runtime where active SSH tunnel routing was fully functional.
- **MTBF (Mean Time Between Failures)**: Average duration of a stable link connection before experiencing a drop.
- **MTTR (Mean Time To Recovery)**: Average duration of an outage before the connection is successfully re-established.
- **Current Link / Outage**: Real-time counters showing the length of the active connection session or the length of the current connection outage.

---

## Customizing Notifications

Outage notifications are sent if a connection drop persists through multiple retry cycles. You can customize the threshold and cooldown frequency directly in the source code of `aerotunnel.py`:

```python
# --- CONFIGURABLE NOTIFICATION SETTINGS ---
NOTIFICATION_FAIL_THRESHOLD = 5  # Number of consecutive connection failures before alerting
NOTIFICATION_COOLDOWN_SEC = 3600  # Minimum cooldown wait time between alerts (in seconds)
```

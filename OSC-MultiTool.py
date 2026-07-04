#!/usr/bin/env python3
"""
OSC Multi Tool
──────────────
A command-line OSC monitor for inspecting messages sent by wardrive-midi
or any OSC source. Three modes:
  - Raw monitor   : prints every incoming message with hex dump + human-readable
  - Channel monitor: splits the terminal into per-address panels (up to 6)
  - Config        : set listening IP / port

Dependencies: python-osc, blessed
  pip install python-osc blessed
"""

import sys
import time
import threading
import textwrap
from collections import deque
from datetime import datetime

from pythonosc import dispatcher, osc_server
from blessed import Terminal

# ─── ANSI palette (works on Linux, macOS, Windows 10+) ────────────────────────

term = Terminal()

C_TITLE   = term.bold + term.color_rgb(255, 107, 53)   # orange — brand accent
C_MENU    = term.bold + term.bright_white
C_LABEL   = term.color_rgb(136, 136, 136)               # mid grey
C_DIM     = term.color_rgb(68, 68, 68)                  # dim grey
C_VALUE   = term.color_rgb(53, 197, 255)                # cyan — payload values
C_HEX     = term.color_rgb(120, 220, 120)               # green — hex dump
C_TIME    = term.color_rgb(180, 180, 180)               # light grey timestamps
C_INPUT   = term.bold + term.bright_white
C_WARN    = term.yellow
C_RESET   = term.normal

# ─── Shared config (mutable at runtime via config menu) ───────────────────────

config = {
    "ip":   "0.0.0.0",   # bind to all interfaces by default
    "port": 9000,
}

# ─── ASCII logo ─────────────────────────────────────────────────────────────
logo = textwrap.dedent(r"""
 ▄██████▄     ▄████████  ▄████████
███    ███   ███    ███ ███    ███
███    ███   ███    █▀  ███    █▀ 
███    ███   ███        ███       
███    ███ ▀███████████ ███       
███    ███          ███ ███    █▄ 
███    ███    ▄█    ███ ███    ███
 ▀██████▀   ▄████████▀  ████████▀ 
              |  |   _)    |                |
  ` \   |  |  |   _|  |     _|   _ \   _ \  |
_|_|_| \_,_| _| \__| _|   \__| \___/ \___/ _|
""").strip("\n")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def clear():
    print(term.clear, end="")

def header(subtitle: str = ""):
    """Print the app name banner + optional subtitle."""
    print(C_TITLE + logo + C_RESET)
    print(C_DIM   + "  ─" * 20 + C_RESET)
    if subtitle:
        print(C_LABEL + f"  {subtitle}" + C_RESET)
    print()

def prompt(msg: str) -> str:
    """Simple styled input prompt."""
    return input(C_INPUT + f"  {msg}" + C_RESET)

def format_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def hex_dump(data: bytes) -> str:
    """Format raw bytes as a compact hex string + ASCII side-car."""
    hex_part = " ".join(f"{b:02X}" for b in data)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return f"{hex_part}   {ascii_part}"

def format_args(args) -> str:
    """Pretty-print OSC argument list with type hints."""
    parts = []
    for a in args:
        if isinstance(a, float):
            parts.append(f"{a:.4f}(f)")
        elif isinstance(a, int):
            parts.append(f"{a}(i)")
        elif isinstance(a, bytes):
            parts.append(f"<blob {len(a)}B>")
        else:
            parts.append(f'"{a}"(s)')
    return "  ".join(parts)

# ─── OSC server lifecycle ──────────────────────────────────────────────────────

class OSCListener:
    """
    Wraps python-osc's blocking server in a daemon thread so the main
    thread stays free for TUI input.  Call start() / stop() around each mode.
    """

    def __init__(self, on_message):
        self._on_message = on_message
        self._server = None
        self._thread = None

    def start(self):
        d = dispatcher.Dispatcher()
        # Map every address to our single handler
        d.set_default_handler(self._handle)
        try:
            self._server = osc_server.ThreadingOSCUDPServer(
                (config["ip"], config["port"]), d
            )
        except OSError as e:
            print(C_WARN + f"\n  Cannot bind to {config['ip']}:{config['port']} — {e}" + C_RESET)
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None

    def _handle(self, address, *args):
        # Reconstruct raw bytes for hex dump from the OSC bundle internals.
        # python-osc doesn't expose the raw packet easily, so we rebuild a
        # minimal representation: address + args as bytes where possible.
        raw = address.encode() + b"\x00" + b"".join(
            a.encode() if isinstance(a, str) else
            a if isinstance(a, bytes) else
            str(a).encode()
            for a in args
        )
        self._on_message(address, list(args), raw)

# ─── Mode: Config ─────────────────────────────────────────────────────────────

def mode_config():
    while True:
        clear()
        header("Config")
        print(C_LABEL + f"  Current IP  : " + C_VALUE + config["ip"]           + C_RESET)
        print(C_LABEL + f"  Current port: " + C_VALUE + str(config["port"])     + C_RESET)
        print()
        print(C_MENU  + "  1" + C_RESET + C_LABEL + "  Set listen IP")
        print(C_MENU  + "  2" + C_RESET + C_LABEL + "  Set listen port")
        print(C_MENU  + "  0" + C_RESET + C_LABEL + "  Back")
        print()
        choice = prompt("> ").strip()

        if choice == "1":
            val = prompt("Listen IP (blank = 0.0.0.0): ").strip()
            config["ip"] = val if val else "0.0.0.0"
        elif choice == "2":
            val = prompt("Port (blank = 9000): ").strip()
            try:
                config["port"] = int(val) if val else 9000
            except ValueError:
                print(C_WARN + "  Invalid port." + C_RESET)
                time.sleep(1)
        elif choice == "0":
            return

# ─── Mode: Raw Monitor ────────────────────────────────────────────────────────

def mode_raw():
    """Print every OSC message as it arrives: timestamp, address, args, hex."""
    clear()
    header(f"Raw Monitor  —  listening on {config['ip']}:{config['port']}")
    print(C_DIM + "  Press ENTER to stop.\n" + C_RESET)

    def on_msg(address, args, raw):
        ts   = format_timestamp()
        args_str = format_args(args)
        dump = hex_dump(raw)
        # Each message block: timestamp + address on one line, args + hex below
        print(C_TIME  + f"  [{ts}] "   + C_RESET
            + C_TITLE + f"{address}"   + C_RESET)
        print(C_LABEL + "  args : "    + C_VALUE + args_str + C_RESET)
        print(C_HEX   + "  hex  : "    + dump    + C_RESET)
        print(C_DIM   + "  " + "─" * 60 + C_RESET)

    listener = OSCListener(on_msg)
    if not listener.start():
        prompt("Press ENTER to return.")
        return

    input()          # block until user hits ENTER
    listener.stop()

# ─── Mode: Channel Monitor ────────────────────────────────────────────────────

# Each panel keeps a rolling buffer of the last N lines so we can redraw.
_PANEL_HISTORY = 50   # lines kept per channel buffer

def mode_channel():
    """
    Split the terminal vertically into one panel per monitored address.
    Redraw the whole screen at ~10 fps so the panels stay in sync.
    Up to 6 channels.
    """
    clear()
    header("Channel Monitor")

    # Collect addresses from user
    print(C_LABEL + "  Enter OSC addresses to monitor (max 6).")
    print(C_LABEL + "  Example: /wardrive/noteOn   (blank line to finish)\n" + C_RESET)

    addresses = []
    while len(addresses) < 6:
        val = prompt(f"  Channel {len(addresses)+1} address (blank to start): ").strip()
        if not val:
            break
        addresses.append(val)

    if not addresses:
        print(C_WARN + "  No addresses entered." + C_RESET)
        time.sleep(1)
        return

    # One deque per address — thread-safe for single-producer / single-consumer
    buffers = {addr: deque(maxlen=_PANEL_HISTORY) for addr in addresses}
    lock    = threading.Lock()

    def on_msg(address, args, _raw):
        if address in buffers:
            ts  = format_timestamp()
            txt = f"[{ts}] {format_args(args)}"
            with lock:
                buffers[address].append(txt)

    listener = OSCListener(on_msg)
    if not listener.start():
        prompt("Press ENTER to return.")
        return

    print(C_DIM + f"\n  Monitoring {len(addresses)} channel(s). Press ENTER to stop.\n" + C_RESET)

    stop_event = threading.Event()

    def render_loop():
        """Redraw all panels at 10 fps until stop_event is set."""
        n = len(addresses)
        while not stop_event.is_set():
            h = term.height
            w = term.width

            # Divide terminal width evenly among panels
            panel_w = max(w // n, 20)

            # Build the full frame as a list of rows
            # Row 0: panel headers
            header_row = ""
            for addr in addresses:
                label = addr[:panel_w - 2].ljust(panel_w - 1)
                header_row += C_TITLE + label + C_DIM + "│" + C_RESET
            
            # Rows 1..(h-3): message lines per panel
            with lock:
                cols = []
                for addr in addresses:
                    lines = list(buffers[addr])   # oldest → newest
                    # Pad or truncate to fit available height
                    visible_h = h - 3             # leave room for header + status bar
                    if len(lines) < visible_h:
                        lines = [""] * (visible_h - len(lines)) + lines
                    else:
                        lines = lines[-visible_h:]
                    # Truncate each line to panel width
                    cols.append([l[:panel_w - 1].ljust(panel_w - 1) for l in lines])

            # Print frame (move cursor to top, overwrite in place)
            out = term.home
            out += C_DIM + "─" * w + C_RESET + "\n"
            out += header_row + "\n"
            out += C_DIM + "─" * w + C_RESET + "\n"

            row_count = len(cols[0]) if cols else 0
            for row_i in range(row_count):
                row = ""
                for col_i, col in enumerate(cols):
                    cell = col[row_i] if row_i < len(col) else " " * (panel_w - 1)
                    row += C_VALUE + cell + C_DIM + "│" + C_RESET
                out += row + "\n"

            # Status bar at bottom
            status = f"  {config['ip']}:{config['port']}  |  {len(addresses)} channels  |  ENTER to stop"
            out += term.move_y(h - 1) + C_DIM + status[:w] + C_RESET

            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(0.1)

    renderer = threading.Thread(target=render_loop, daemon=True)

    with term.fullscreen(), term.hidden_cursor():
        renderer.start()
        input()           # block main thread until ENTER
        stop_event.set()

    listener.stop()

# ─── Main Menu ────────────────────────────────────────────────────────────────

def main_menu():
    while True:
        clear()
        header()
        print(C_LABEL + f"  Listening on {C_VALUE}{config['ip']}:{config['port']}{C_RESET}\n")
        print(C_MENU  + "  1" + C_RESET + C_LABEL + "  Config")
        print(C_MENU  + "  2" + C_RESET + C_LABEL + "  Raw monitor")
        print(C_MENU  + "  3" + C_RESET + C_LABEL + "  Channel monitor")
        print(C_MENU  + "  0" + C_RESET + C_LABEL + "  Exit")
        print()
        choice = prompt("> ").strip()

        if   choice == "1": mode_config()
        elif choice == "2": mode_raw()
        elif choice == "3": mode_channel()
        elif choice == "0": sys.exit(0)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print(C_RESET + "\n  Bye.\n")
        sys.exit(0)

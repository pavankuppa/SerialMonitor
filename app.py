#!/usr/bin/env python3
"""
Serial Port GUI Application with GTK
Multi-tab serial terminal with color-coded input/output
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

import serial
import serial.tools.list_ports
import threading
import queue
import os
import json

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_command_history.json")
MAX_HISTORY_PER_CONNECTION = 500


def load_all_history():
    """Load command history for all connections from disk"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_all_history(history):
    """Save command history for all connections to disk"""
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except IOError:
        pass


class SerialTab:
    """Represents a single serial connection tab"""

    def __init__(self, notebook, tab_number, parent):
        self.tab_number = tab_number
        self.parent = parent
        self.notebook = notebook
        self.serial_port = None
        self.is_connected = False
        self.read_thread = None
        self.message_queue = queue.Queue()

        # Command history per connection
        self.connection_name = ""
        self.command_history = []
        self.history_index = -1  # -1 means not navigating history
        self.current_input = ""  # Stores text typed before navigating history

        # Create main container
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.set_margin_top(10)
        self.box.set_margin_bottom(10)
        self.box.set_margin_start(10)
        self.box.set_margin_end(10)

        # Create tab label
        self.tab_label = Gtk.Label(label=f"Connection {tab_number}")

        # Add to notebook
        notebook.append_page(self.box, self.tab_label)

        # Create UI elements
        self.create_widgets()

        # Start queue processing
        GLib.timeout_add(100, self.process_queue)

    def create_widgets(self):
        # Connection name row
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.pack_start(name_box, False, False, 0)

        name_box.pack_start(Gtk.Label(label="Connection Name:"), False, False, 0)
        self.name_combo = Gtk.ComboBoxText.new_with_entry()
        self.name_combo.set_hexpand(True)
        self.populate_connection_names()
        self.name_entry = self.name_combo.get_child()
        self.name_entry.set_placeholder_text("e.g. ESP32, Arduino, Sensor-1")
        self.name_entry.connect("activate", self.on_connection_name_applied)
        self.name_entry.connect("focus-out-event", lambda w, e: self.on_connection_name_applied(w))
        self.name_combo.connect("changed", self.on_name_combo_selected)
        name_box.pack_start(self.name_combo, True, True, 0)

        # Control panel
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.pack_start(control_box, False, False, 0)

        # Port selection
        control_box.pack_start(Gtk.Label(label="Port:"), False, False, 0)
        self.port_combo = Gtk.ComboBoxText()
        self.port_combo.set_size_request(150, -1)
        control_box.pack_start(self.port_combo, False, False, 0)
        self.refresh_ports()

        # Baudrate selection
        control_box.pack_start(Gtk.Label(label="Baudrate:"), False, False, 0)
        self.baudrate_combo = Gtk.ComboBoxText()
        baudrates = ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        for rate in baudrates:
            self.baudrate_combo.append_text(rate)
        self.baudrate_combo.set_active(4)  # 115200
        control_box.pack_start(self.baudrate_combo, False, False, 0)

        # Refresh button
        refresh_btn = Gtk.Button(label="🔄 Refresh")
        refresh_btn.connect("clicked", lambda w: self.refresh_ports())
        control_box.pack_start(refresh_btn, False, False, 0)

        # Connect button
        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.connect("clicked", lambda w: self.toggle_connection())
        control_box.pack_start(self.connect_btn, False, False, 0)

        # Status label
        self.status_label = Gtk.Label(label="Disconnected")
        self.status_label.set_markup("<span foreground='red'>Disconnected</span>")
        control_box.pack_start(self.status_label, False, False, 10)

        # Console area with scrolling
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self.box.pack_start(scrolled, True, True, 0)

        # Text view for console
        self.console = Gtk.TextView()
        self.console.set_editable(False)
        self.console.set_cursor_visible(False)
        self.console.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        # Set monospace font and dark background
        font_desc = Pango.FontDescription("Monospace 10")
        self.console.modify_font(font_desc)

        # Set colors
        bg_color = Gdk.color_parse("#000000")
        fg_color = Gdk.color_parse("#FFFFFF")
        self.console.modify_bg(Gtk.StateType.NORMAL, bg_color)
        self.console.modify_fg(Gtk.StateType.NORMAL, fg_color)

        scrolled.add(self.console)

        # Create text buffer and tags
        self.text_buffer = self.console.get_buffer()

        # Create tags for colors
        self.text_buffer.create_tag("output", foreground="#00FF00")  # Green
        self.text_buffer.create_tag("input", foreground="#00BFFF")   # Light blue
        self.text_buffer.create_tag("error", foreground="#FF4500")   # Orange-red
        self.text_buffer.create_tag("info", foreground="#FFD700")    # Gold

        # Input area
        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.pack_start(input_box, False, False, 0)

        input_box.pack_start(Gtk.Label(label="Send:"), False, False, 0)

        self.input_entry = Gtk.Entry()
        self.input_entry.set_hexpand(True)
        self.input_entry.connect("activate", lambda w: self.send_data())
        self.input_entry.connect("key-press-event", self.on_input_key_press)
        input_box.pack_start(self.input_entry, True, True, 0)

        send_btn = Gtk.Button(label="Send")
        send_btn.connect("clicked", lambda w: self.send_data())
        input_box.pack_start(send_btn, False, False, 0)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda w: self.clear_console())
        input_box.pack_start(clear_btn, False, False, 0)

    def populate_connection_names(self):
        """Populate the connection name dropdown with existing names from history"""
        self.name_combo.remove_all()
        all_history = load_all_history()
        for name in sorted(all_history.keys()):
            self.name_combo.append_text(name)

    def on_name_combo_selected(self, combo):
        """When a name is selected from the dropdown, apply it immediately"""
        if combo.get_active() >= 0:
            self.on_connection_name_applied(self.name_entry)

    def on_connection_name_applied(self, widget):
        """Apply connection name on Enter or focus-out. Handles rename by migrating history."""
        new_name = widget.get_text().strip()
        old_name = self.connection_name

        if new_name == old_name:
            return

        all_history = load_all_history()

        if old_name and new_name and old_name != new_name:
            # Rename: migrate history from old name to new name
            old_history = all_history.pop(old_name, [])
            # Merge: existing new_name history (if any) + old history
            existing = all_history.get(new_name, [])
            merged = existing + old_history
            if len(merged) > MAX_HISTORY_PER_CONNECTION:
                merged = merged[-MAX_HISTORY_PER_CONNECTION:]
            all_history[new_name] = merged
            save_all_history(all_history)
            self.command_history = merged
        elif new_name:
            # New name set (no old name to migrate from)
            self.command_history = all_history.get(new_name, [])
        else:
            # Name cleared
            self.command_history = []

        self.connection_name = new_name
        self.tab_label.set_text(new_name if new_name else f"Connection {self.tab_number}")
        self.history_index = -1
        # Refresh dropdown for all tabs to show updated names
        for tab in self.parent.tabs:
            tab.populate_connection_names()
            # Restore the current text after repopulating
            tab.name_entry.set_text(tab.connection_name)

    def save_command_to_history(self, command):
        """Save a command to this connection's history and persist to disk"""
        if not self.connection_name or not command.strip():
            return
        # Avoid consecutive duplicates
        if not self.command_history or self.command_history[-1] != command:
            self.command_history.append(command)
        # Trim to max size, keeping the most recent commands
        if len(self.command_history) > MAX_HISTORY_PER_CONNECTION:
            self.command_history = self.command_history[-MAX_HISTORY_PER_CONNECTION:]
        # Persist
        all_history = load_all_history()
        all_history[self.connection_name] = self.command_history
        save_all_history(all_history)

    def on_input_key_press(self, widget, event):
        """Handle Up/Down arrow keys for command history navigation"""
        if not self.command_history:
            return False

        keyval = event.keyval

        if keyval == Gdk.KEY_Up:
            if self.history_index == -1:
                # Starting to navigate: save current typed text
                self.current_input = widget.get_text()
                self.history_index = len(self.command_history) - 1
            elif self.history_index > 0:
                self.history_index -= 1
            widget.set_text(self.command_history[self.history_index])
            # Move cursor to end
            GLib.idle_add(widget.set_position, -1)
            return True  # Consume the event

        elif keyval == Gdk.KEY_Down:
            if self.history_index == -1:
                return False  # Not navigating history
            if self.history_index < len(self.command_history) - 1:
                self.history_index += 1
                widget.set_text(self.command_history[self.history_index])
            else:
                # Went past the end, restore original typed text
                self.history_index = -1
                widget.set_text(self.current_input)
            GLib.idle_add(widget.set_position, -1)
            return True

        return False

    def get_available_ports(self):
        """Return filtered list of available serial ports"""
        ports = serial.tools.list_ports.comports()
        return sorted([port.device for port in ports if 'ttyS' not in port.device])

    def refresh_ports(self):
        """Refresh the list of available serial ports, preserving current selection"""
        if self.is_connected:
            return  # Don't change dropdown while connected

        current = self.port_combo.get_active_text()
        port_list = self.get_available_ports()

        self.port_combo.remove_all()
        for port in port_list:
            self.port_combo.append_text(port)

        # Restore previous selection if still available
        if current and current in port_list:
            self.port_combo.set_active(port_list.index(current))
        elif port_list:
            self.port_combo.set_active(0)

    def toggle_connection(self):
        """Connect or disconnect from serial port"""
        if self.is_connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        """Connect to the selected serial port"""
        port = self.port_combo.get_active_text()
        baudrate_text = self.baudrate_combo.get_active_text()

        if not port:
            self.log_message("Please select a port", "error")
            return

        try:
            baudrate = int(baudrate_text)
            self.serial_port = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1
            )

            self.is_connected = True
            self.connect_btn.set_label("Disconnect")
            self.status_label.set_markup("<span foreground='green'>Connected</span>")
            self.log_message(f"Connected to {port} at {baudrate} baud", "info")

            # Start read thread
            self.read_thread = threading.Thread(target=self.read_from_serial, daemon=True)
            self.read_thread.start()

            # Disable port/baudrate selection
            self.port_combo.set_sensitive(False)
            self.baudrate_combo.set_sensitive(False)

        except serial.SerialException as e:
            self.log_message(f"Error: {str(e)}", "error")
        except ValueError:
            self.log_message("Invalid baudrate", "error")

    def disconnect(self):
        """Disconnect from serial port"""
        self.is_connected = False

        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()

        self.connect_btn.set_label("Connect")
        self.status_label.set_markup("<span foreground='red'>Disconnected</span>")
        self.log_message("Disconnected", "info")

        # Enable port/baudrate selection
        self.port_combo.set_sensitive(True)
        self.baudrate_combo.set_sensitive(True)

    def read_from_serial(self):
        """Thread function to read from serial port"""
        while self.is_connected:
            try:
                if self.serial_port and self.serial_port.in_waiting > 0:
                    data = self.serial_port.readline()
                    try:
                        text = data.decode('utf-8').strip()
                        self.message_queue.put(("output", f"<< {text}\n"))
                    except UnicodeDecodeError:
                        text = f"[HEX] {data.hex()}"
                        self.message_queue.put(("output", f"<< {text}\n"))
            except Exception as e:
                if self.is_connected:
                    self.message_queue.put(("error", f"Read error: {str(e)}\n"))
                break

    def send_data(self):
        """Send data through serial port"""
        if not self.is_connected or not self.serial_port:
            self.log_message("Not connected", "error")
            return

        text = self.input_entry.get_text().upper()
        if not text:
            return

        try:
            self.serial_port.write((text + '\n').encode('utf-8'))
            self.log_message(f">> {text}\n", "input")
            self.save_command_to_history(text)
            self.history_index = -1
            self.current_input = ""
            self.input_entry.set_text("")
        except Exception as e:
            self.log_message(f"Send error: {str(e)}", "error")

    def log_message(self, message, tag="output"):
        """Add message to console with specified color tag"""
        self.message_queue.put((tag, message + "\n" if not message.endswith("\n") else message))

    def process_queue(self):
        """Process messages from the queue and update console"""
        try:
            while True:
                tag, message = self.message_queue.get_nowait()
                end_iter = self.text_buffer.get_end_iter()
                self.text_buffer.insert_with_tags_by_name(end_iter, message, tag)

                # Auto-scroll to bottom
                mark = self.text_buffer.get_insert()
                self.console.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        except queue.Empty:
            pass

        return True  # Continue calling this function

    def clear_console(self):
        """Clear the console"""
        self.text_buffer.set_text("")


class SerialGUI(Gtk.Window):
    """Main GUI application"""

    def __init__(self):
        super().__init__(title="Serial Port Terminal")
        self.set_default_size(900, 650)
        self.connect("delete-event", self.on_closing)

        # Set window icon
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_terminal_icon.svg")
        if os.path.exists(icon_path):
            self.set_icon_from_file(icon_path)

        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_box)

        # Create notebook (tabbed interface)
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        main_box.pack_start(self.notebook, True, True, 0)

        # Tab management
        self.tabs = []
        self.tab_counter = 1

        # Track known ports for auto-detection
        self.known_ports = set()

        # Create first tab
        self.add_tab()

        # Auto-detect USB port changes every 2 seconds
        GLib.timeout_add(2000, self.auto_detect_ports)

        # Button panel at bottom
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        button_box.set_margin_top(5)
        button_box.set_margin_bottom(5)
        button_box.set_margin_start(10)
        button_box.set_margin_end(10)
        main_box.pack_start(button_box, False, False, 0)

        new_tab_btn = Gtk.Button(label="+ New Tab")
        new_tab_btn.connect("clicked", lambda w: self.add_tab())
        button_box.pack_start(new_tab_btn, False, False, 0)

        close_tab_btn = Gtk.Button(label="Close Tab")
        close_tab_btn.connect("clicked", lambda w: self.close_current_tab())
        button_box.pack_start(close_tab_btn, False, False, 0)

    def auto_detect_ports(self):
        """Periodically check for new/removed USB ports and refresh all tabs"""
        ports = serial.tools.list_ports.comports()
        current_ports = set(p.device for p in ports if 'ttyS' not in p.device)

        if current_ports != self.known_ports:
            added = current_ports - self.known_ports
            removed = self.known_ports - current_ports
            self.known_ports = current_ports

            # Log and refresh all disconnected tabs
            for tab in self.tabs:
                if added:
                    tab.log_message(f"USB port(s) detected: {', '.join(sorted(added))}", "info")
                if removed:
                    tab.log_message(f"USB port(s) removed: {', '.join(sorted(removed))}", "info")
                tab.refresh_ports()

        return True  # Keep the timer running

    def add_tab(self):
        """Add a new serial connection tab"""
        tab = SerialTab(self.notebook, self.tab_counter, self)
        self.tabs.append(tab)
        self.tab_counter += 1
        self.show_all()

    def close_current_tab(self):
        """Close the currently selected tab"""
        if len(self.tabs) <= 1:
            return  # Keep at least one tab

        current_index = self.notebook.get_current_page()
        tab = self.tabs[current_index]

        # Disconnect if connected
        if tab.is_connected:
            tab.disconnect()

        # Remove tab
        self.notebook.remove_page(current_index)
        self.tabs.pop(current_index)

    def on_closing(self, widget, event):
        """Cleanup and close all connections before exiting"""
        # Disconnect all tabs
        for tab in self.tabs:
            if tab.is_connected:
                tab.disconnect()

        Gtk.main_quit()
        return False


def main():
    app = SerialGUI()
    app.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()

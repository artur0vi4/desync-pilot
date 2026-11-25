"""Embedded SVG icons and QSS dark theme, kept in code so we don't ship
extra binary assets. Colours follow a modern dark palette."""
from __future__ import annotations

DARK_QSS = """
QWidget {
    background-color: #1e1f24;
    color: #e6e6e8;
    font-family: 'Segoe UI', 'SF Pro Text', 'Noto Sans', sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background-color: #1e1f24; }
QStatusBar { background-color: #161719; color: #9aa0a6; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: #3a3b42; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #4a4c55; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #3a3b42; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QTabWidget::pane { border: 1px solid #2c2d33; border-radius: 8px; top: 1px; }
QTabBar { font-weight: 600; }
QTabBar::tab {
    background: transparent;
    color: #b0b3ba;
    padding: 7px 18px;
    border: none;
    border-radius: 7px;
    margin-right: 4px;
}
QTabBar::tab:selected { background: #2c5bd6; color: #fff; }
QTabBar::tab:hover:!selected { background: #2a2b32; }
QGroupBox {
    border: 1px solid #2c2d33;
    border-radius: 6px;
    margin-top: 10px;
    padding: 14px 12px 10px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: #8ab4ff;
}
QPushButton {
    background-color: #2c5bd6;
    color: #fff;
    border: none;
    padding: 8px 18px;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton:hover { background-color: #3a6ae0; }
QPushButton:pressed { background-color: #2349b8; }
QPushButton:disabled { background-color: #3a3b42; color: #6c6e76; }
QPushButton[variant="danger"] { background-color: #c0392b; }
QPushButton[variant="danger"]:hover { background-color: #d04334; }
QPushButton[variant="danger"]:disabled { background-color: #8a2f26; color: #d9d9dc; }
QPushButton[variant="ghost"] { background-color: transparent; border: 1px solid #3a3b42; color: #c0c3ca; }
QPushButton[variant="ghost"]:hover { background-color: #2a2b32; }
QPushButton[variant="ghost"]:disabled { color: #6c6e76; }
QLineEdit, QComboBox, QSpinBox {
    background-color: #2a2b32;
    color: #e6e6e8;
    border: 1px solid #3a3b42;
    border-radius: 5px;
    padding: 5px 8px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2c5bd6; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #2a2b32;
    color: #e6e6e8;
    selection-background-color: #2c5bd6;
    border: 1px solid #3a3b42;
}
QTableWidget {
    background-color: #232429;
    alternate-background-color: #27282e;
    gridline-color: #2c2d33;
    border: 1px solid #2c2d33;
    border-radius: 6px;
}
QTableWidget::item { padding: 5px 6px; }
QTableWidget::item:selected { background-color: #2c5bd6; color: #fff; }
QHeaderView::section {
    background-color: #2a2b32;
    color: #b0b3ba;
    padding: 7px 6px;
    border: none;
    border-right: 1px solid #2c2d33;
    font-weight: 600;
}
QTextEdit, QPlainTextEdit {
    background-color: #161719;
    color: #c8ccd4;
    border: 1px solid #2c2d33;
    border-radius: 6px;
    font-family: 'Cascadia Code', 'Consolas', 'Menlo', monospace;
    font-size: 12px;
}
QProgressBar {
    background-color: #2a2b32;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: #e6e6e8;
    max-height: 10px;
}
QProgressBar::chunk { background-color: #2c5bd6; border-radius: 5px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #3a3b42; border-radius: 4px; background: #2a2b32;
}
QCheckBox::indicator:checked { background: #2c5bd6; border-color: #2c5bd6; }
QMessageBox { background-color: #232429; }
QLabel[role="hero"] { font-size: 22px; font-weight: 700; color: #fff; }
QLabel[role="sub"] { color: #9aa0a6; }
QLabel[role="hint"] { color: #8b8f98; font-size: 12px; }
QLabel[role="value"] { color: #8ab4ff; font-weight: 600; }
QLabel[role="value-big"] { font-size: 17px; font-weight: 700; color: #8ab4ff; }
QLabel[role="badge"] {
    background-color: #2c5bd6; color: #fff; padding: 3px 10px;
    border-radius: 9px; font-size: 12px; font-weight: 700;
}
QLabel[role="badge-good"] { background-color: #1f7a44; }
QLabel[role="badge-bad"] { background-color: #a3302a; }
"""


def app_icon_svg() -> str:
    """Simple shield + play icon, used for window + tray."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3a6ae0"/>
      <stop offset="1" stop-color="#1f3fa6"/>
    </linearGradient>
  </defs>
  <path fill="url(#g)" d="M32 4 L56 14 V32 C56 46 44 56 32 60 C20 56 8 46 8 32 V14 Z"/>
  <path fill="#fff" d="M26 22 V42 L42 32 Z"/>
</svg>"""


def status_icon_svg(state: str) -> str:
    """Coloured badge variant of the app icon for the tray."""
    color = {
        "running": "#1f7a44",
        "testing": "#b07b00",
        "error": "#a3302a",
        "idle": "#5a5d65",
    }.get(state, "#5a5d65")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <path fill="{color}" d="M32 4 L56 14 V32 C56 46 44 56 32 60 C20 56 8 46 8 32 V14 Z"/>
  <path fill="#fff" d="M26 22 V42 L42 32 Z"/>
</svg>"""

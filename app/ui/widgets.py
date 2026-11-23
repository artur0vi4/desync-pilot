"""Reusable lightweight widgets."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)


class Card(QFrame):
    """A panel with a title, used as a section container."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(
            "#Card { background-color: #232429; border: 1px solid #2c2d33; "
            "border-radius: 8px; }"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)
        if title:
            t = QLabel(title)
            t.setStyleSheet("color:#8ab4ff; font-weight:600; font-size:13px;")
            self._layout.addWidget(t)

    def addWidget(self, w: QWidget) -> None:  # type: ignore[override]
        self._layout.addWidget(w)

    def addLayout(self, l) -> None:
        self._layout.addLayout(l)

    def addStretch(self) -> None:
        self._layout.addStretch()


class KV(QWidget):
    """A label + value row."""

    def __init__(self, key: str, value: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        k = QLabel(key)
        k.setProperty("role", "sub")
        v = QLabel(value)
        v.setProperty("role", "value")
        v.setAlignment(Qt.AlignmentFlag.AlignRight)
        h.addWidget(k)
        h.addStretch()
        h.addWidget(v)
        self.label = v

    def set_value(self, s: str) -> None:
        self.label.setText(s)


class Badge(QLabel):
    def __init__(self, text: str = "", good: bool = False, bad: bool = False) -> None:
        super().__init__(text)
        role = "badge-good" if good else ("badge-bad" if bad else "badge")
        self.setProperty("role", role)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

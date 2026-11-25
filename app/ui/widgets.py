"""Reusable lightweight widgets."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QSizePolicy, QVBoxLayout, QWidget,
)


class FlowLayout(QLayout):
    """A layout that wraps children onto the next row when width runs out.

    Used for the toolbar so that five buttons survive a narrow window
    instead of truncating their captions.
    """

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing
        self._items: list = []

    def addItem(self, item) -> None:  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        row_h = 0
        for item in self._items:
            w, h = item.sizeHint().width(), item.sizeHint().height()
            next_x = x + w + self._spacing
            if next_x - self._spacing > right and row_h > 0:
                x = rect.x() + m.left()
                y += row_h + self._spacing
                next_x = x + w + self._spacing
                row_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))
            x = next_x
            row_h = max(row_h, h)
        return y + row_h - rect.y() + m.bottom()


class Card(QFrame):
    """A panel with a title, used as a section container."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(
            "#Card { background-color: #232429; border: 1px solid #2c2d33; "
            "border-radius: 10px; }"
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

    def addLayout(self, layout) -> None:
        self._layout.addLayout(layout)

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
        # Long strategy names must be readable (and copyable), not truncated
        # into an unreadable sliver — the full text also goes to the tooltip.
        v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.setWordWrap(True)
        h.addWidget(k)
        h.addStretch()
        h.addWidget(v)
        self.label = v

    def set_value(self, s: str) -> None:
        self.label.setText(s)
        self.label.setToolTip(s)


class Badge(QLabel):
    def __init__(self, text: str = "", good: bool = False, bad: bool = False) -> None:
        super().__init__(text)
        role = "badge-good" if good else ("badge-bad" if bad else "badge")
        self.setProperty("role", role)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

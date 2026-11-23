"""System-tray icon and context menu."""
from __future__ import annotations

from PyQt6.QtCore import QByteArray, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .. import i18n
from .resources import app_icon_svg, status_icon_svg


def _svg_to_icon(svg: str) -> QIcon:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    renderer.render(p)
    p.end()
    return QIcon(pix)


class TrayIcon(QObject):
    show_main_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    retest_requested = pyqtSignal()
    exit_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._icon = QSystemTrayIcon()
        self._icon.setIcon(_svg_to_icon(app_icon_svg()))
        self._icon.setToolTip(i18n.t("tray.tooltip.idle"))
        # Icons are rendered from SVG; cache them instead of re-rendering on
        # every snapshot (the status is refreshed several times per minute).
        self._icon_cache: dict[str, QIcon] = {"": _svg_to_icon(app_icon_svg())}
        self._state: str = ""
        self._strategy_id: str = ""
        self._menu = QMenu()
        self._build_menu()
        self._icon.setContextMenu(self._menu)
        # left-click toggles the main window
        self._icon.activated.connect(self._on_activated)

    # ---------- availability ----------

    def show(self) -> None:
        self._icon.show()

    def available(self) -> bool:
        try:
            return bool(QSystemTrayIcon.isSystemTrayAvailable()) and self._icon.isVisible()
        except Exception:
            return False

    # ---------- menu ----------

    def _build_menu(self) -> None:
        m = self._menu
        m.clear()
        a_show = QAction(i18n.t("tray.show"), m)
        a_show.triggered.connect(self.show_main_requested.emit)
        m.addAction(a_show)
        m.addSeparator()
        self._a_start = QAction(i18n.t("tray.start"), m)
        self._a_start.triggered.connect(self.start_requested.emit)
        m.addAction(self._a_start)
        self._a_stop = QAction(i18n.t("tray.stop"), m)
        self._a_stop.triggered.connect(self.stop_requested.emit)
        m.addAction(self._a_stop)
        m.addSeparator()
        a_retest = QAction(i18n.t("tray.retest"), m)
        a_retest.triggered.connect(self.retest_requested.emit)
        m.addAction(a_retest)
        m.addSeparator()
        a_exit = QAction(i18n.t("tray.exit"), m)
        a_exit.triggered.connect(self.exit_requested.emit)
        m.addAction(a_exit)

    def retranslate(self) -> None:
        """Rebuild menu labels after a language change."""
        self._build_menu()
        self._apply_tooltip()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_main_requested.emit()

    # ---------- state ----------

    def _icon_for(self, state: str) -> QIcon:
        key = state or ""
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = _svg_to_icon(status_icon_svg(state))
            self._icon_cache[key] = icon
        return icon

    def _apply_tooltip(self) -> None:
        if self._state == "running":
            tip = i18n.t("tray.tooltip.running", self._strategy_id or "-")
        elif self._state == "testing":
            tip = i18n.t("tray.tooltip.testing")
        elif self._state == "diagnosing":
            tip = i18n.t("tray.tooltip.diagnosing")
        else:
            tip = i18n.t("tray.tooltip.idle")
        self._icon.setToolTip(tip)

    def set_state(self, state: str, strategy_id: str = "") -> None:
        if state == self._state and strategy_id == self._strategy_id:
            return  # nothing changed — do not touch the menu
        self._state = state
        self._strategy_id = strategy_id
        self._icon.setIcon(self._icon_for(state))
        self._apply_tooltip()
        self._a_start.setEnabled(state not in ("starting", "testing", "diagnosing", "running", "stopping"))
        self._a_stop.setEnabled(state in ("running", "testing", "diagnosing", "starting"))

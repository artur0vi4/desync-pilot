"""Main application window with five tabs: Status / Strategies / Diagnostics / Log / About."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from .. import i18n, logger, paths
from ..controller import AppController, AppSnapshot, State
from .resources import DARK_QSS, app_icon_svg
from .tray import TrayIcon
from .widgets import Badge, Card, FlowLayout, KV
from .settings_dialog import SettingsDialog

STRATEGY_ID_ROLE = Qt.ItemDataRole.UserRole


def _status_label(state: State) -> str:
    return {
        State.IDLE: i18n.t("status.idle"),
        State.STARTING: i18n.t("status.starting"),
        State.TESTING: i18n.t("status.testing"),
        State.DIAGNOSING: i18n.t("status.diagnosing"),
        State.RUNNING: i18n.t("status.running"),
        State.STOPPING: i18n.t("status.stopping"),
        State.ERROR: i18n.t("status.error"),
    }.get(state, str(state))


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self._ctrl = controller
        self.setWindowTitle(i18n.t("app.title"))
        self.resize(920, 680)
        self.setMinimumSize(700, 540)
        self._apply_window_icon()
        self.setStyleSheet(DARK_QSS)

        # Log view buffer size (kept early so tabs can reuse it)
        self._log_buffer_max = 2000
        # Close-to-tray
        self._exit_confirmed = False

        self.tray = TrayIcon()
        self.tray.show_main_requested.connect(self._show_and_raise)
        self.tray.start_requested.connect(self._ctrl.start)
        self.tray.stop_requested.connect(self._ctrl.stop)
        self.tray.retest_requested.connect(self._ctrl.retest)
        self.tray.exit_requested.connect(self._on_exit)
        self.tray.show()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header
        title = QLabel(i18n.t("app.title"))
        title.setProperty("role", "hero")
        sub = QLabel(i18n.t("app.subtitle"))
        sub.setProperty("role", "sub")
        root.addWidget(title)
        root.addWidget(sub)

        # Toolbar — wraps onto a second row in a narrow window instead of
        # squeezing the captions out of the buttons.
        tb = FlowLayout(spacing=8)
        self._btn_start = QPushButton(i18n.t("btn.start"))
        self._btn_stop = QPushButton(i18n.t("btn.stop"))
        self._btn_stop.setProperty("variant", "danger")
        self._btn_retest = QPushButton(i18n.t("btn.retest"))
        self._btn_retest.setProperty("variant", "ghost")
        self._btn_settings = QPushButton(i18n.t("btn.settings"))
        self._btn_settings.setProperty("variant", "ghost")
        self._btn_export = QPushButton(i18n.t("btn.export_log"))
        self._btn_export.setProperty("variant", "ghost")
        tb.addWidget(self._btn_start)
        tb.addWidget(self._btn_stop)
        tb.addWidget(self._btn_retest)
        stretch = QWidget()
        stretch.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(stretch)
        tb.addWidget(self._btn_export)
        tb.addWidget(self._btn_settings)
        root.addLayout(tb)

        # Status badge + progress
        self._status_badge = Badge(_status_label(State.IDLE))
        root.addWidget(self._status_badge, alignment=Qt.AlignmentFlag.AlignLeft)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        root.addWidget(self._progress)
        self._progress_label = QLabel("")
        self._progress_label.setProperty("role", "sub")
        self._progress_label.setVisible(False)
        root.addWidget(self._progress_label)

        # Tabs
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        self._build_status_tab()
        self._build_strategies_tab()
        self._build_diagnostics_tab()
        self._build_log_tab()
        self._build_about_tab()

        # Wire controller signals
        self._ctrl.state_changed.connect(self._on_snapshot)
        self._ctrl.log_appended.connect(self._on_log)
        self._ctrl.test_progress.connect(self._on_test_progress)
        self._ctrl.strategies_loaded.connect(self._on_strategies_loaded)
        self._ctrl.update_finished.connect(self._on_update_finished)
        self._ctrl.diagnostics_ready.connect(self._on_diagnostics)
        self._ctrl.strategy_found.connect(self._on_strategy_found)

        # id -> Strategy, rebuilt only when the catalog changes (used on every
        # snapshot tick — rebuilding it per tick used to walk the whole list).
        self._strat_index: dict = {}

        # Buttons
        self._btn_start.clicked.connect(self._ctrl.start)
        self._btn_stop.clicked.connect(self._ctrl.stop)
        self._btn_retest.clicked.connect(self._retest)
        self._btn_export.clicked.connect(self._export_log)
        self._btn_settings.clicked.connect(self._open_settings)
        self._btn_update.clicked.connect(self._update_strategies)
        self._btn_diag.clicked.connect(self._run_diagnostics)
        self._btn_diag_copy.clicked.connect(self._copy_diagnostics)

    @property
    def _strategies_by_id(self) -> dict:
        return self._strat_index

    # ---------- construction helpers ----------

    def _apply_window_icon(self) -> None:
        try:
            from PyQt6.QtSvg import QSvgRenderer
            from PyQt6.QtCore import QByteArray
            renderer = QSvgRenderer(QByteArray(app_icon_svg().encode("utf-8")))
            pix = QPixmap(64, 64)
            pix.fill(Qt.GlobalColor.transparent)
            p = QPainter(pix)
            renderer.render(p)
            p.end()
            self.setWindowIcon(QIcon(pix))
        except Exception:
            pass

    def _build_status_tab(self) -> None:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        # The cards no longer have to fight for vertical space in a small
        # window: everything scrolls, nothing gets squeezed or clipped.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(12)

        # --- current strategy (the hero row) ---
        c_strat = Card(i18n.t("group.current"))
        self._lbl_strategy_big = QLabel("—")
        self._lbl_strategy_big.setProperty("role", "value-big")
        self._lbl_strategy_big.setWordWrap(True)
        self._lbl_strategy_big.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        c_strat.addWidget(self._lbl_strategy_big)
        self._kv_group = KV(i18n.t("label.group"), "—")
        self._kv_modes = KV(i18n.t("label.modes"), "—")
        c_strat.addWidget(self._kv_group)
        c_strat.addWidget(self._kv_modes)
        v.addWidget(c_strat)

        # --- connection health ---
        c_health = Card(i18n.t("group.health"))
        self._kv_health = KV(i18n.t("label.reachable"), "—")
        self._kv_health_rtt = KV(i18n.t("label.avg_rtt"), "—")
        c_health.addWidget(self._kv_health)
        c_health.addWidget(self._kv_health_rtt)
        v.addWidget(c_health)

        # --- DNS recommendation (informational only) ---
        c_dns = Card(i18n.t("group.dns"))
        self._kv_dns_rec = KV(i18n.t("label.recommended"), "—")
        self._kv_dns_rtt = KV(i18n.t("label.rtt"), "—")
        c_dns.addWidget(self._kv_dns_rec)
        c_dns.addWidget(self._kv_dns_rtt)
        dns_hint = QLabel(i18n.t("label.dns_hint"))
        dns_hint.setProperty("role", "hint")
        dns_hint.setWordWrap(True)
        c_dns.addWidget(dns_hint)
        v.addWidget(c_dns)

        # --- last test ---
        c_last = Card(i18n.t("group.last_test"))
        self._kv_tested_at = KV(i18n.t("label.tested_at"), "—")
        self._kv_best = KV(i18n.t("label.best"), "—")
        c_last.addWidget(self._kv_tested_at)
        c_last.addWidget(self._kv_best)
        v.addWidget(c_last)

        v.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._tabs.addTab(tab, i18n.t("tab.status"))

    def _build_strategies_tab(self) -> None:
        tab = QWidget()
        v = QVBoxLayout(tab)

        filter_row = QHBoxLayout()
        self._strat_search = QLineEdit()
        self._strat_search.setPlaceholderText(i18n.t("label.search"))
        self._strat_search.textChanged.connect(self._apply_strategy_filter)
        self._strat_group = QComboBox()
        self._strat_group.addItem(i18n.t("label.all_groups"), "")
        self._strat_group.currentIndexChanged.connect(self._apply_strategy_filter)
        filter_row.addWidget(self._strat_search, 1)
        filter_row.addWidget(self._strat_group)
        self._strat_count = QLabel("")
        self._strat_count.setProperty("role", "sub")
        filter_row.addWidget(self._strat_count)
        v.addLayout(filter_row)

        self._strat_table = QTableWidget(0, 5)
        self._strat_table.setHorizontalHeaderLabels([
            i18n.t("label.name"), i18n.t("label.group"), i18n.t("label.modes"),
            i18n.t("label.source"), i18n.t("label.score"),
        ])
        self._strat_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._strat_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._strat_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._strat_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._strat_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._strat_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._strat_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._strat_table.setAlternatingRowColors(True)
        self._strat_table.doubleClicked.connect(self._on_strategy_double_click)
        v.addWidget(self._strat_table)

        row = QHBoxLayout()
        hint = QLabel(i18n.t("hint.switch_strategy"))
        hint.setProperty("role", "sub")
        row.addWidget(hint)
        row.addStretch()
        self._btn_update = QPushButton(i18n.t("btn.update"))
        self._btn_update.setProperty("variant", "ghost")
        row.addWidget(self._btn_update)
        v.addLayout(row)
        self._tabs.addTab(tab, i18n.t("tab.strategies"))

    def _build_diagnostics_tab(self) -> None:
        tab = QWidget()
        v = QVBoxLayout(tab)
        self._diag_verdict = QLabel(i18n.t("diag.idle"))
        self._diag_verdict.setWordWrap(True)
        self._diag_verdict.setProperty("role", "sub")
        v.addWidget(self._diag_verdict)

        row = QHBoxLayout()
        self._btn_diag = QPushButton(i18n.t("diag.btn.check"))
        self._btn_diag_copy = QPushButton(i18n.t("diag.btn.copy"))
        self._btn_diag_copy.setProperty("variant", "ghost")
        self._btn_diag_copy.setEnabled(False)
        row.addWidget(self._btn_diag)
        row.addStretch()
        row.addWidget(self._btn_diag_copy)
        v.addLayout(row)

        self._diag_table = QTableWidget(0, 3)
        self._diag_table.setHorizontalHeaderLabels([
            i18n.t("diag.col.status"), i18n.t("diag.col.check"), i18n.t("diag.col.detail"),
        ])
        self._diag_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._diag_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._diag_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._diag_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._diag_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._diag_table.setAlternatingRowColors(True)
        self._diag_table.verticalHeader().setVisible(False)
        v.addWidget(self._diag_table, 1)

        self._last_report = None
        self._tabs.addTab(tab, i18n.t("tab.diagnostics"))

    def _build_log_tab(self) -> None:
        tab = QWidget()
        v = QVBoxLayout(tab)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(self._log_buffer_max)
        # Show what already happened before the window was built.
        initial = logger.snapshot()
        if initial:
            self._log_view.setPlainText("\n".join(initial[-self._log_buffer_max:]))
        v.addWidget(self._log_view)
        self._tabs.addTab(tab, i18n.t("tab.log"))

    def _build_about_tab(self) -> None:
        tab = QWidget()
        v = QVBoxLayout(tab)
        title = QLabel(i18n.t("app.title"))
        title.setProperty("role", "hero")
        v.addWidget(title)
        from .. import __version__
        v.addWidget(QLabel(f"v{__version__}"))
        v.addSpacing(8)
        desc = QLabel(i18n.t("about.description"))
        desc.setWordWrap(True)
        v.addWidget(desc)
        v.addSpacing(8)
        from ..updater import current_tag
        upstream = QLabel(i18n.t("about.upstream") + f" · {current_tag()}")
        upstream.setProperty("role", "sub")
        v.addWidget(upstream)
        lic = QLabel(i18n.t("about.license"))
        lic.setProperty("role", "sub")
        v.addWidget(lic)
        v.addStretch()
        self._tabs.addTab(tab, i18n.t("tab.about"))

    # ---------- slots ----------

    def _on_snapshot(self, snap: AppSnapshot) -> None:
        state = snap.state if isinstance(snap.state, State) else State(snap.state)
        self._status_badge.setText(_status_label(state))
        good = state == State.RUNNING
        bad = state == State.ERROR
        role = "badge-good" if good else ("badge-bad" if bad else "badge")
        if self._status_badge.property("role") != role:
            self._status_badge.setProperty("role", role)
            self._status_badge.style().unpolish(self._status_badge)
            self._status_badge.style().polish(self._status_badge)

        self._lbl_strategy_big.setText(snap.strategy_display or "—")
        s = self._strat_index.get(snap.strategy_id)
        self._kv_group.set_value(s.group if s else "—")
        self._kv_modes.set_value(snap.strategy_modes or "—")

        self._kv_dns_rec.set_value(snap.dns_recommend_name or "—")
        self._kv_dns_rtt.set_value(f"{snap.dns_recommend_rtt_ms:.0f} ms" if snap.dns_recommend_rtt_ms else "—")

        self._kv_health.set_value(f"{snap.health_ok}/{snap.health_total}" if snap.health_total else "—")
        self._kv_health_rtt.set_value(f"{snap.health_rtt_ms:.0f} ms" if snap.health_rtt_ms else "—")

        if snap.last_test_at:
            self._kv_tested_at.set_value(datetime.fromtimestamp(snap.last_test_at).strftime("%Y-%m-%d %H:%M:%S"))
        if snap.last_test_best:
            self._kv_best.set_value(snap.last_test_best)

        # progress bar
        testing = state == State.TESTING
        self._progress.setVisible(testing)
        self._progress_label.setVisible(testing)
        if testing:
            i, n = snap.testing_progress
            self._progress.setRange(0, max(1, n))
            self._progress.setValue(i)

        # tray
        self.tray.set_state(state.value, snap.strategy_id)

        # button enable: Stop doubles as "cancel the running sweep"
        self._btn_start.setEnabled(state in (State.IDLE, State.ERROR))
        self._btn_stop.setEnabled(state in (State.RUNNING, State.TESTING, State.STARTING, State.DIAGNOSING))
        self._btn_retest.setEnabled(state in (State.IDLE, State.RUNNING, State.ERROR))
        busy = state in (State.STARTING, State.TESTING, State.DIAGNOSING, State.STOPPING)
        self._btn_update.setEnabled(not busy)
        self._btn_diag.setEnabled(not busy)

    def _on_log(self, lines: list[str]) -> None:
        if not lines:
            return
        sb = self._log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self._log_view.appendPlainText("\n".join(lines))
        if at_bottom:
            sb.setValue(sb.maximum())

    def _on_test_progress(self, i: int, n: int, name: str) -> None:
        self._progress.setVisible(True)
        self._progress.setRange(0, max(1, n))
        self._progress.setValue(i)
        if name:
            self._progress_label.setText(i18n.t("msg.testing_progress", i, n, name))

    def _on_strategy_found(self, strategy_id: str, score: float) -> None:
        """The sweep hit its first fully-working strategy and asks what to do.

        Shown as a modal dialog with a countdown: no answer = accept, so an
        unattended autopilot in the tray is never blocked.
        """
        self._show_and_raise()
        body = i18n.t("msg.found_body", strategy_id, f"{score:.0f}")
        remaining = max(5, int(self._ctrl.config.ask_found_timeout_seconds))

        box = QMessageBox(self)
        box.setWindowTitle(i18n.t("msg.found_title"))
        box.setIcon(QMessageBox.Icon.Question)
        accept_btn = box.addButton(i18n.t("msg.found_accept"), QMessageBox.ButtonRole.AcceptRole)
        cont_btn = box.addButton(i18n.t("msg.found_continue"), QMessageBox.ButtonRole.NoRole)
        cont_btn.setProperty("variant", "ghost")
        cont_btn.style().unpolish(cont_btn)
        cont_btn.style().polish(cont_btn)
        box.setDefaultButton(accept_btn)
        # Countdown: no answer within the timeout means "accept" — the same
        # default the controller uses when the window is not looked at.
        state = {"left": remaining}

        def _tick() -> None:
            state["left"] -= 1
            if state["left"] > 0:
                box.setText(f"{body}\n\n{i18n.t('msg.found_auto', state['left'])}")
            else:
                timer.stop()
                box.accept()  # timeout = accept the found strategy

        timer = QTimer(box)
        timer.setInterval(1000)
        timer.timeout.connect(_tick)
        box.setText(f"{body}\n\n{i18n.t('msg.found_auto', state['left'])}")
        timer.start()
        box.exec()
        timer.stop()
        accepted = box.clickedButton() is not cont_btn
        self._ctrl.answer_found(accepted)

    def _on_strategies_loaded(self, strategies) -> None:
        self._all_strategies = list(strategies)
        self._strat_index = {s.id: s for s in self._all_strategies}
        groups = sorted({s.group for s in self._all_strategies})
        current = self._strat_group.currentData()
        self._strat_group.blockSignals(True)
        self._strat_group.clear()
        self._strat_group.addItem(i18n.t("label.all_groups"), "")
        for g in groups:
            self._strat_group.addItem(g, g)
        idx = self._strat_group.findData(current)
        self._strat_group.setCurrentIndex(idx if idx >= 0 else 0)
        self._strat_group.blockSignals(False)
        self._apply_strategy_filter()

    def _apply_strategy_filter(self, *args) -> None:
        strategies = getattr(self, "_all_strategies", [])
        query = self._strat_search.text().strip().casefold()
        group = self._strat_group.currentData() or ""
        visible = []
        for s in strategies:
            if group and s.group != group:
                continue
            if query:
                haystack = " ".join((s.id, s.title, s.group, s.display_modes)).casefold()
                if query not in haystack:
                    continue
            visible.append(s)
        self._fill_strategy_table(visible)
        self._strat_count.setText(i18n.t("label.showing", len(visible), len(strategies)))

    def _fill_strategy_table(self, strategies) -> None:
        results = self._ctrl.last_results()
        scores = {r.strategy.id: r.score for r in results}
        self._strat_table.setRowCount(len(strategies))
        for row, s in enumerate(strategies):
            name_item = QTableWidgetItem(s.title)
            name_item.setData(STRATEGY_ID_ROLE, s.id)
            name_item.setToolTip(f"{s.id}\n{s.note}".strip())
            self._strat_table.setItem(row, 0, name_item)
            self._strat_table.setItem(row, 1, QTableWidgetItem(s.group))
            self._strat_table.setItem(row, 2, QTableWidgetItem(s.display_modes))
            source_key = "source.community" if s.source == "zapret-gui" else "source.base"
            self._strat_table.setItem(row, 3, QTableWidgetItem(i18n.t(source_key)))
            sc = scores.get(s.id, 0.0)
            self._strat_table.setItem(row, 4, QTableWidgetItem(f"{sc:.1f}" if sc else "—"))

    def _on_strategy_double_click(self, idx) -> None:
        item = self._strat_table.item(idx.row(), 0)
        strategy_id = item.data(STRATEGY_ID_ROLE) if item else None
        if strategy_id:
            self._ctrl.switch_strategy(strategy_id)

    def _on_update_finished(self, ok: bool, message: str) -> None:
        self._btn_update.setEnabled(True)
        self._btn_update.setText(i18n.t("btn.update"))
        if not message:
            return
        if ok:
            self.statusBar().showMessage(message, 8000)
        else:
            QMessageBox.warning(self, i18n.t("app.title"), message)

    def _retest(self) -> None:
        total = len(self._ctrl.strategies())
        if total > 60:
            reply = QMessageBox.question(
                self, i18n.t("btn.retest"), i18n.t("msg.retest_long", total),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._ctrl.retest()

    def _run_diagnostics(self) -> None:
        if self._ctrl.state == State.RUNNING:
            reply = QMessageBox.question(
                self, i18n.t("tab.diagnostics"), i18n.t("diag.need_stop"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._btn_diag.setEnabled(False)
        self._btn_diag.setText(i18n.t("diag.running"))
        self._diag_verdict.setText(i18n.t("diag.running"))
        self._diag_table.setRowCount(0)
        self._ctrl.run_diagnostics()

    def _on_diagnostics(self, report) -> None:
        from ..diagnostics import FAIL, INFO, OK, WARN
        self._last_report = report
        self._btn_diag.setEnabled(True)
        self._btn_diag.setText(i18n.t("diag.btn.check"))
        self._btn_diag_copy.setEnabled(True)

        colors = {
            OK: QColor("#57c785"),
            WARN: QColor("#e0b341"),
            FAIL: QColor("#e06c61"),
            INFO: QColor("#9aa0a6"),
        }
        labels = {
            OK: i18n.t("diag.status.ok"),
            WARN: i18n.t("diag.status.warn"),
            FAIL: i18n.t("diag.status.fail"),
            INFO: i18n.t("diag.status.info"),
        }
        self._diag_table.setRowCount(len(report.checks))
        for row, check in enumerate(report.checks):
            status_item = QTableWidgetItem(labels.get(check.status, check.status))
            status_item.setForeground(colors.get(check.status, colors[INFO]))
            self._diag_table.setItem(row, 0, status_item)
            self._diag_table.setItem(row, 1, QTableWidgetItem(check.title))
            detail = check.detail
            if check.hint:
                detail = f"{detail} — {check.hint}" if detail else check.hint
            self._diag_table.setItem(row, 2, QTableWidgetItem(detail))

        verdict = i18n.t("diag.verdict." + report.verdict)
        if report.recommended:
            verdict += " " + i18n.t("diag.recommend", ", ".join(report.recommended))
        elif report.verdict == "ok":
            verdict += " " + i18n.t("diag.no_recommend")
        self._diag_verdict.setText(f"{i18n.t('diag.verdict')}: {verdict}  ({i18n.t('diag.done', report.duration_s)})")

    def _copy_diagnostics(self) -> None:
        if not self._last_report:
            return
        from ..diagnostics import report_as_text
        QApplication.clipboard().setText(report_as_text(self._last_report))
        self.statusBar().showMessage(i18n.t("diag.copied"), 5000)

    def _update_strategies(self) -> None:
        self._btn_update.setEnabled(False)
        self._btn_update.setText("…")
        self._ctrl.apply_update()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._ctrl.config, self)
        if dlg.exec():
            self._ctrl.update_config(dlg.result_config())
            self._refresh_ui_text()

    def _refresh_ui_text(self) -> None:
        self.setWindowTitle(i18n.t("app.title"))
        self._btn_start.setText(i18n.t("btn.start"))
        self._btn_stop.setText(i18n.t("btn.stop"))
        self._btn_retest.setText(i18n.t("btn.retest"))
        self._btn_settings.setText(i18n.t("btn.settings"))
        self._btn_export.setText(i18n.t("btn.export_log"))
        self._btn_update.setText(i18n.t("btn.update"))
        self._btn_diag.setText(i18n.t("diag.btn.check"))
        self._btn_diag_copy.setText(i18n.t("diag.btn.copy"))
        self._strat_search.setPlaceholderText(i18n.t("label.search"))
        self._strat_table.setHorizontalHeaderLabels([
            i18n.t("label.name"), i18n.t("label.group"), i18n.t("label.modes"),
            i18n.t("label.source"), i18n.t("label.score"),
        ])
        if self._strat_group.count():
            self._strat_group.setItemText(0, i18n.t("label.all_groups"))
        for i in range(self._tabs.count()):
            tab_name = ["status", "strategies", "diagnostics", "log", "about"][i]
            self._tabs.setTabText(i, i18n.t(f"tab.{tab_name}"))
        self.tray.retranslate()

    def _export_log(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, i18n.t("btn.export_log"),
            str(paths.user_data_dir() / "zapret-log.txt"), "Text (*.txt)",
        )
        if target:
            p = self._ctrl.export_log(Path(target))
            QMessageBox.information(self, i18n.t("app.title"), f"Exported → {p}")

    def _show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------- close ----------

    def closeEvent(self, e) -> None:
        if self._exit_confirmed:
            e.accept()
            return
        # Minimize to tray instead of quitting
        if self.tray.available():
            e.ignore()
            self.hide()
            return
        e.accept()
        QApplication.quit()

    def _on_exit(self) -> None:
        reply = QMessageBox.question(
            self, i18n.t("app.title"), i18n.t("msg.confirm_exit"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._exit_confirmed = True
            self._ctrl.shutdown()
            QApplication.quit()

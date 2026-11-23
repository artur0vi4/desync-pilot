"""Settings dialog."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
    QSpinBox, QVBoxLayout,
)

from .. import i18n
from ..config import Config
from ..dns.servers import SERVERS


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(i18n.t("settings.title"))
        self.setMinimumWidth(480)
        self._cfg = Config(**config.__dict__)

        v = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        # language
        self._lang = QComboBox()
        self._lang.addItem("Русский", "ru")
        self._lang.addItem("English", "en")
        self._lang.setCurrentIndex(0 if config.language == "ru" else 1)
        form.addRow(i18n.t("label.language"), self._lang)

        # auto mode
        self._auto = QCheckBox(i18n.t("label.auto_mode_hint"))
        self._auto.setChecked(config.auto_mode)
        form.addRow(i18n.t("label.auto_mode"), self._auto)

        # quick pick
        self._quick = QCheckBox(i18n.t("label.quick_scan"))
        self._quick.setChecked(config.quick_scan)
        form.addRow(i18n.t("label.quick_scan"), self._quick)

        self._quick_limit = QSpinBox()
        self._quick_limit.setRange(5, 500)
        self._quick_limit.setValue(config.quick_scan_limit)
        form.addRow(i18n.t("label.quick_scan_limit"), self._quick_limit)

        # recheck interval
        self._recheck = QSpinBox()
        self._recheck.setRange(5, 240)
        self._recheck.setValue(config.recheck_every_minutes)
        form.addRow(i18n.t("label.recheck_every"), self._recheck)

        # result cache TTL
        self._cache_ttl = QSpinBox()
        self._cache_ttl.setRange(0, 24 * 60)
        self._cache_ttl.setValue(config.results_ttl_minutes)
        self._cache_ttl.setToolTip("0 = re-test every launch")
        form.addRow(i18n.t("label.results_ttl"), self._cache_ttl)

        # dns mode
        self._dns_mode = QComboBox()
        self._dns_mode.addItem(i18n.t("label.dns_mode_hybrid"), "hybrid")
        self._dns_mode.addItem(i18n.t("label.dns_mode_doh_only"), "doh_only")
        self._dns_mode.addItem(i18n.t("label.dns_mode_system"), "system")
        modes = ["hybrid", "doh_only", "system"]
        idx = modes.index(config.dns_mode) if config.dns_mode in modes else 0
        self._dns_mode.setCurrentIndex(idx)
        form.addRow(i18n.t("label.dns_strategy"), self._dns_mode)

        # doh provider
        self._doh = QComboBox()
        self._doh.addItem("auto", "auto")
        for s in SERVERS:
            self._doh.addItem(s.name, s.name)
        ix = self._doh.findData(config.doh_provider)
        self._doh.setCurrentIndex(ix if ix >= 0 else 0)
        form.addRow(i18n.t("label.doh_provider"), self._doh)

        # custom DoH endpoint
        self._doh_custom = QLineEdit(config.doh_custom_url or "")
        self._doh_custom.setPlaceholderText("https://dns.example/dns-query")
        self._doh_custom.setToolTip(i18n.t("label.custom_doh_hint"))
        form.addRow(i18n.t("label.custom_doh"), self._doh_custom)

        # dns listen port
        self._port = QSpinBox()
        self._port.setRange(1024, 65535)
        self._port.setValue(config.dns_listen_port)
        form.addRow(i18n.t("label.dns_port"), self._port)

        # toasts
        self._toasts = QCheckBox(i18n.t("label.show_toasts"))
        self._toasts.setChecked(config.show_toasts)
        form.addRow(i18n.t("label.show_toasts"), self._toasts)

        # start minimized
        self._min = QCheckBox(i18n.t("label.start_minimized"))
        self._min.setChecked(config.start_minimized)
        form.addRow(i18n.t("label.start_minimized"), self._min)

        # run at Windows logon
        self._autostart_win = QCheckBox(i18n.t("label.autostart_windows"))
        self._autostart_win.setChecked(config.autostart_windows)
        form.addRow(i18n.t("label.autostart_windows"), self._autostart_win)

        # auto start zapret on launch
        self._auto_start = QCheckBox(i18n.t("label.autostart_zapret"))
        self._auto_start.setChecked(config.autostart_on_launch)
        form.addRow(i18n.t("label.autostart_zapret"), self._auto_start)

        # test settings
        self._timeout = QSpinBox()
        self._timeout.setRange(2, 30)
        self._timeout.setValue(config.test_timeout_seconds)
        form.addRow(i18n.t("label.probe_timeout"), self._timeout)

        self._attempts = QSpinBox()
        self._attempts.setRange(1, 5)
        self._attempts.setValue(config.test_attempts)
        form.addRow(i18n.t("label.probe_attempts"), self._attempts)

        # export dir
        self._export = QLineEdit(config.log_export_dir or "")
        form.addRow(i18n.t("label.export_to"), self._export)

        v.addLayout(form)

        # buttons
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText(i18n.t("settings.save"))
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText(i18n.t("settings.cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def result_config(self) -> Config:
        c = self._cfg
        c.language = self._lang.currentData()
        c.auto_mode = self._auto.isChecked()
        c.quick_scan = self._quick.isChecked()
        c.quick_scan_limit = self._quick_limit.value()
        c.recheck_every_minutes = self._recheck.value()
        c.results_ttl_minutes = self._cache_ttl.value()
        c.dns_mode = self._dns_mode.currentData()
        c.doh_provider = self._doh.currentData()
        c.doh_custom_url = self._doh_custom.text().strip()
        c.dns_listen_port = self._port.value()
        c.show_toasts = self._toasts.isChecked()
        c.start_minimized = self._min.isChecked()
        c.autostart_windows = self._autostart_win.isChecked()
        c.autostart_on_launch = self._auto_start.isChecked()
        c.test_timeout_seconds = self._timeout.value()
        c.test_attempts = self._attempts.value()
        c.log_export_dir = self._export.text().strip()
        return c

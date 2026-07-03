# launcher.py
# Launcher UI for the RPL Log Analyzer.
# Features: mode selector, optional radio/timeline, recent files, drag-drop on window.

import sys
import os
import json
import subprocess
from typing import Optional, List

from PyQt6.QtCore import Qt, pyqtSignal, QSettings
from PyQt6.QtGui import QPalette, QColor, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QLabel, QPushButton, QFileDialog, QMessageBox,
    QGroupBox, QRadioButton, QButtonGroup, QFrame, QListWidget,
    QListWidgetItem, QSizePolicy, QSplitter,
)

_RECENT_KEY  = "recent_logs"
_MAX_RECENT  = 8
_SETTINGS_ORG  = "RPLAnalyzer"
_SETTINGS_APP  = "Launcher"


class FileDropLineEdit(QLineEdit):
    fileDropped = pyqtSignal(str)

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    event.acceptProposedAction(); return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            path = urls[0].toLocalFile()
            self.setText(path); self.fileDropped.emit(path)


class LauncherWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RPL Log Analyzer — Launcher")
        self.setAcceptDrops(True)   # allow dropping on the whole window
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── Mode selector ──────────────────────────────────────────────────
        mode_grp = QGroupBox("Log source")
        mode_grp.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12px; }")
        mode_row = QHBoxLayout(mode_grp); mode_row.setSpacing(20)
        self._mode_group = QButtonGroup(self)
        self.rb_auto   = QRadioButton("Auto-detect")
        self.rb_cooja  = QRadioButton("Cooja simulation")
        self.rb_iotlab = QRadioButton("IoT-LAB hardware")
        self.rb_auto.setChecked(True)
        for rb in (self.rb_auto, self.rb_cooja, self.rb_iotlab):
            self._mode_group.addButton(rb); mode_row.addWidget(rb)
        mode_row.addStretch()
        self.rb_cooja.toggled.connect(self._on_mode_changed)
        self.rb_iotlab.toggled.connect(self._on_mode_changed)
        self.rb_auto.toggled.connect(self._on_mode_changed)
        root.addWidget(mode_grp)

        # ── File inputs ────────────────────────────────────────────────────
        files_grp = QGroupBox("Select log files")
        files_grp.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12px; }")
        files_layout = QVBoxLayout(files_grp); files_layout.setSpacing(8)

        # 1) Main log
        self.log_label = QLabel("Log file — <b>REQUIRED</b>")
        self.log_edit  = FileDropLineEdit(
            "Drop log file here (loglistener.txt / IoT-LAB log) or click Browse…")
        self.log_edit.fileDropped.connect(self._on_log_dropped)
        log_browse = QPushButton("Browse…"); log_browse.clicked.connect(self.browse_log)
        row1 = QHBoxLayout()
        row1.addWidget(self.log_edit, 1); row1.addWidget(log_browse)
        files_layout.addWidget(self.log_label); files_layout.addLayout(row1)

        div = QFrame(); div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color:#ccc;"); files_layout.addWidget(div)

        # 2) Radio log (optional, Cooja only)
        self.radio_label = QLabel("Radio log (rm / radiolog.txt) — <i>optional, Cooja only</i>")
        self.radio_edit  = FileDropLineEdit(
            "Drop radio log here or click Browse… (leave blank to skip)")
        radio_browse = QPushButton("Browse…"); radio_browse.clicked.connect(self.browse_radio)
        self.radio_clear = QPushButton("✕"); self.radio_clear.setFixedWidth(28)
        self.radio_clear.setToolTip("Clear"); self.radio_clear.clicked.connect(self.radio_edit.clear)
        row2 = QHBoxLayout()
        row2.addWidget(self.radio_edit, 1); row2.addWidget(radio_browse); row2.addWidget(self.radio_clear)
        files_layout.addWidget(self.radio_label); files_layout.addLayout(row2)

        # 3) Timeline (optional, Cooja only)
        self.timeline_label = QLabel("Timeline / TimeDetail — <i>optional, Cooja only</i>")
        self.timeline_edit  = FileDropLineEdit(
            "Drop timeline log here or click Browse… (leave blank to skip)")
        timeline_browse = QPushButton("Browse…"); timeline_browse.clicked.connect(self.browse_timeline)
        self.timeline_clear = QPushButton("✕"); self.timeline_clear.setFixedWidth(28)
        self.timeline_clear.setToolTip("Clear"); self.timeline_clear.clicked.connect(self.timeline_edit.clear)
        row3 = QHBoxLayout()
        row3.addWidget(self.timeline_edit, 1); row3.addWidget(timeline_browse); row3.addWidget(self.timeline_clear)
        files_layout.addWidget(self.timeline_label); files_layout.addLayout(row3)

        root.addWidget(files_grp)

        # ── Info box ───────────────────────────────────────────────────────
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(
            "background:#f0f7ff; border:1px solid #b3d1f0; border-radius:5px;"
            "padding:8px; font-size:11px; color:#1a3a5c;")
        root.addWidget(self.info_label)

        # ── Recent files ───────────────────────────────────────────────────
        recent_grp = QGroupBox("Recent log files  (double-click to open)")
        recent_grp.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12px; }")
        recent_layout = QVBoxLayout(recent_grp); recent_layout.setContentsMargins(6, 6, 6, 6)
        self.recent_list = QListWidget()
        self.recent_list.setMaximumHeight(130)
        self.recent_list.setAlternatingRowColors(True)
        self.recent_list.itemDoubleClicked.connect(self._on_recent_double_clicked)
        self.recent_list.setStyleSheet("font-size: 11px;")
        recent_layout.addWidget(self.recent_list)

        recent_btn_row = QHBoxLayout()
        use_btn = QPushButton("Use selected"); use_btn.setFixedHeight(24)
        use_btn.clicked.connect(self._use_selected_recent)
        clr_btn = QPushButton("Clear history"); clr_btn.setFixedHeight(24)
        clr_btn.clicked.connect(self._clear_recent)
        recent_btn_row.addWidget(use_btn); recent_btn_row.addWidget(clr_btn); recent_btn_row.addStretch()
        recent_layout.addLayout(recent_btn_row)
        root.addWidget(recent_grp)

        # ── Launch button ──────────────────────────────────────────────────
        self.launch_button = QPushButton("▶  Open Analyzer")
        self.launch_button.setDefault(True)
        self.launch_button.setMinimumHeight(38)
        self.launch_button.setStyleSheet(
            "QPushButton { background:#01696f; color:white; font-size:13px; "
            "font-weight:bold; border-radius:5px; padding:6px 20px; }"
            "QPushButton:hover { background:#018a91; }"
            "QPushButton:pressed { background:#015558; }")
        self.launch_button.clicked.connect(self.run_main_script)
        root.addWidget(self.launch_button)

        self.resize(680, 560)
        self._on_mode_changed()
        self._load_recent()

    # ── Drag-drop on whole window ──────────────────────────────────────────
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.log_edit.setText(urls[0].toLocalFile())

    def _on_log_dropped(self, path: str):
        """Auto-detect mode when a file is dropped."""
        pass   # mode stays as selected; auto-detect handles it in main.py

    # ── Mode change ────────────────────────────────────────────────────────
    def _on_mode_changed(self):
        is_iotlab = self.rb_iotlab.isChecked()
        for w in (self.radio_label, self.radio_edit,
                  self.timeline_label, self.timeline_edit):
            w.setVisible(not is_iotlab)

        if is_iotlab:
            self.log_label.setText("IoT-LAB log file — <b>REQUIRED</b>")
            self.log_edit.setPlaceholderText(
                "Drop IoT-LAB log file here (e.g. rdf_clean.txt) or click Browse…")
            self.info_label.setText(
                "<b>IoT-LAB mode</b><br>"
                "Only the single IoT-LAB log file is needed.<br>"
                "Parses RDF events, builds radio topology from physical XY coordinates, "
                "and shows PDR / AoI summaries.")
        elif self.rb_cooja.isChecked():
            self.log_label.setText("Cooja loglistener.txt — <b>REQUIRED</b>")
            self.log_edit.setPlaceholderText(
                "Drop mote output log here (loglistener.txt) or click Browse…")
            self.info_label.setText(
                "<b>Cooja mode</b><br>"
                "Only <i>loglistener.txt</i> is required.<br>"
                "Radio log and timeline are <i>optional</i> — if left blank the analyzer "
                "will try to auto-discover them in the same directory.")
        else:
            self.log_label.setText("Log file — <b>REQUIRED</b>  (format auto-detected)")
            self.log_edit.setPlaceholderText(
                "Drop any log file here (Cooja or IoT-LAB) or click Browse…")
            self.info_label.setText(
                "<b>Auto-detect mode</b><br>"
                "Format is detected automatically from file content.<br>"
                "• Cooja: <code>MM:SS.mmm  ID:N  [LEVEL: Module] message</code><br>"
                "• IoT-LAB: <code>timestamp;m3-N;[LEVEL: Module] message</code>")

    # ── File pickers ───────────────────────────────────────────────────────
    def _last_dir(self) -> str:
        return self._settings.value("last_dir", os.path.expanduser("~"))

    def _save_last_dir(self, path: str):
        self._settings.setValue("last_dir", os.path.dirname(path))

    def browse_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select log file", self._last_dir(),
            "Text / log files (*.txt *.log *);;All files (*)")
        if path:
            self.log_edit.setText(path); self._save_last_dir(path)

    def browse_radio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select radio log", self._last_dir(),
            "Text / log files (*.txt *.log *);;All files (*)")
        if path:
            self.radio_edit.setText(path); self._save_last_dir(path)

    def browse_timeline(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select timeline log", self._last_dir(),
            "Text / log files (*.txt *.log *);;All files (*)")
        if path:
            self.timeline_edit.setText(path); self._save_last_dir(path)

    # ── Recent files ───────────────────────────────────────────────────────
    def _load_recent(self):
        recent: List[str] = self._settings.value(_RECENT_KEY, []) or []
        self.recent_list.clear()
        for path in recent:
            item = QListWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.recent_list.addItem(item)

    def _save_recent(self, path: str):
        recent: List[str] = self._settings.value(_RECENT_KEY, []) or []
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        recent = recent[:_MAX_RECENT]
        self._settings.setValue(_RECENT_KEY, recent)
        self._load_recent()

    def _on_recent_double_clicked(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.isfile(path):
            self.log_edit.setText(path)
        else:
            QMessageBox.warning(self, "File not found",
                                f"File no longer exists:\n{path}")

    def _use_selected_recent(self):
        item = self.recent_list.currentItem()
        if item:
            self._on_recent_double_clicked(item)

    def _clear_recent(self):
        self._settings.remove(_RECENT_KEY)
        self.recent_list.clear()

    # ── Launch ─────────────────────────────────────────────────────────────
    def run_main_script(self):
        log_path      = self.log_edit.text().strip()
        radio_path    = self.radio_edit.text().strip()
        timeline_path = self.timeline_edit.text().strip()

        if not log_path:
            QMessageBox.warning(self, "Missing file",
                                "Please select a log file before continuing.")
            return
        if not os.path.isfile(log_path):
            QMessageBox.critical(self, "File not found",
                                 f"Log file does not exist:\n{log_path}")
            return
        for label, path in [("radio log", radio_path), ("timeline log", timeline_path)]:
            if path and not os.path.isfile(path):
                QMessageBox.critical(self, "File not found",
                                     f"The {label} path does not exist:\n{path}")
                return

        script_dir = os.path.dirname(os.path.abspath(__file__))
        main_py    = os.path.join(script_dir, "main.py")
        if not os.path.isfile(main_py):
            QMessageBox.critical(self, "main.py not found",
                                 f"Could not find main.py:\n{main_py}")
            return

        cmd = [sys.executable, main_py, log_path]
        if radio_path:
            cmd.append(radio_path)
            if timeline_path:
                cmd.append(timeline_path)
        elif timeline_path:
            cmd += ["", timeline_path]

        try:
            subprocess.Popen(cmd)
            self._save_recent(log_path)
        except Exception as e:
            QMessageBox.critical(self, "Failed to start",
                                 f"Command:\n{' '.join(cmd)}\n\nError:\n{e}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,     QColor("white"))
    pal.setColor(QPalette.ColorRole.Base,       QColor("white"))
    pal.setColor(QPalette.ColorRole.Text,       QColor("black"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("black"))
    app.setPalette(pal)
    win = LauncherWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

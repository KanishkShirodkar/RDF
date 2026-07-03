# main.py
# Unified RPL Log Analyzer — Cooja simulation + IoT-LAB hardware logs.
#
# New features vs previous version:
#   • Numeric sort on all numeric columns (via UserRole+1 sort key)
#   • Per-column inline search row (Excel-style, below header)
#   • Excel-style dropdown column filters (right-click header)
#   • Row bookmarking — double-click any row to highlight it orange
#   • Bookmark panel — jump between bookmarks, clear all
#   • Copy selected rows to clipboard (Ctrl+C)
#   • Export any table to CSV (right-click → Export to CSV)
#   • Time-range filter bar (min/max time slider + spin boxes)
#   • "Find in log" quick-search with Ctrl+F, highlights matches
#   • Status bar shows live row count after filtering
#   • Topology: node colour by PDR quality (IoT-LAB)
#   • Topology: right-click node → "Show only this node's log"
#   • Tab badge shows row count

import sys, os, re, math, csv
from typing import Optional, List, Dict, Set, Tuple

from PyQt6.QtCore import (
    Qt, QSortFilterProxyModel, QModelIndex, pyqtSignal,
    QRectF, QPoint, QTimer, QAbstractTableModel,
)
from PyQt6.QtGui import (
    QPalette, QColor, QMouseEvent, QPen, QBrush, QFont,
    QAction, QPainter, QKeySequence, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QLabel, QTabWidget, QTableView, QMessageBox, QGraphicsScene,
    QGraphicsView, QHeaderView, QPushButton, QMenu, QListWidget, QListWidgetItem,
    QWidgetAction, QAbstractItemView, QFrame, QSplitter, QSizePolicy,
    QGroupBox, QDoubleSpinBox, QScrollArea, QStatusBar, QToolBar,
    QDialog, QDialogButtonBox, QCheckBox,
)

from parser import (
    parse_log, build_node_infos, build_udp_flows,
    parse_radio_log, parse_timeline,
    build_dodag_events, get_parent_map, get_path_to_root,
    get_intermediate_parents, NodeInfo, STATIC_PARENT_MAP,
    parse_rdf_events, build_iotlab_radio_graph_from_nodes,
    build_iotlab_pdr_summary, build_iotlab_aoi_summary,
    RdfEvent,
)
from models import (
    LogTableModel, NodeTableModel, FlowTableModel, RadioTableModel,
    DodagJoinModel, IntermediateParentModel,
    RdfEventTableModel, IotlabNodeTableModel, PdrTableModel, AoiTableModel,
    _SORT_ROLE,
)

# ── Colours ───────────────────────────────────────────────────────────────────
C_ROOT     = QColor("#ffd54f")
C_PARENT   = QColor("#66bb6a")
C_LEAF     = QColor("#7ec8ff")
C_SELECTED = QColor("#ff8c42")
C_PATH     = QColor("#e91e63")
C_NEIGHBOR = QColor("#ffe082")
C_PDR_BAD  = QColor("#ef9a9a")   # red tint for low-PDR nodes (IoT-LAB)
C_PDR_MED  = QColor("#fff59d")   # yellow tint

# ── Numeric-aware sort proxy ──────────────────────────────────────────────────
class NumericSortProxy(QSortFilterProxyModel):
    """Uses _SORT_ROLE for sorting so numeric columns sort as numbers."""

    def __init__(self):
        super().__init__()
        self.setSortRole(_SORT_ROLE)
        # per-column text filters (substring, case-insensitive)
        self._col_text: Dict[int, str] = {}
        # node-id set filter
        self.node_filter: Optional[Set[int]] = None
        # global text filter
        self.global_text: str = ""
        # time range filter
        self.time_min: Optional[float] = None
        self.time_max: Optional[float] = None
        # column value (dropdown) filters
        self.col_value_filters: Dict[int, Optional[Set[str]]] = {}

    # ── setters ───────────────────────────────────────────────────────────
    def set_col_text(self, col: int, text: str):
        self._col_text[col] = text.lower().strip()
        self.invalidateFilter()

    def set_node_filter(self, ids: Optional[Set[int]]):
        self.node_filter = ids; self.invalidateFilter()

    def set_global_text(self, text: str):
        self.global_text = text.lower().strip(); self.invalidateFilter()

    def set_time_range(self, tmin: Optional[float], tmax: Optional[float]):
        self.time_min = tmin; self.time_max = tmax; self.invalidateFilter()

    def set_col_value_filter(self, col: int, allowed: Optional[Set[str]]):
        self.col_value_filters[col] = allowed; self.invalidateFilter()

    def clear_col_value_filter(self, col: int):
        self.col_value_filters.pop(col, None); self.invalidateFilter()

    def clear_all_filters(self):
        self._col_text.clear()
        self.node_filter = None
        self.global_text = ""
        self.time_min = self.time_max = None
        self.col_value_filters.clear()
        self.invalidateFilter()

    # ── helpers ───────────────────────────────────────────────────────────
    def _cell(self, model, row, col, parent=QModelIndex()) -> str:
        v = model.data(model.index(row, col, parent), Qt.ItemDataRole.DisplayRole)
        return "" if v is None else str(v)

    def filterAcceptsRow(self, src_row: int, src_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True

        # ── node-id filter (column 0 or 1 depending on table) ─────────────
        if self.node_filter is not None:
            # try col 0 first, then col 1
            for nc in (0, 1):
                try:
                    v = int(self._cell(model, src_row, nc, src_parent))
                    if v in self.node_filter:
                        break
                except ValueError:
                    pass
            else:
                return False

        # ── time range filter (col 0 assumed to be time) ──────────────────
        if self.time_min is not None or self.time_max is not None:
            try:
                t = float(self._cell(model, src_row, 0, src_parent).replace(":", ""))
            except ValueError:
                t = None
            # Try to get raw sort value for time
            raw_t = model.data(model.index(src_row, 0, src_parent), _SORT_ROLE)
            if isinstance(raw_t, (int, float)):
                t = float(raw_t)
            if t is not None:
                if self.time_min is not None and t < self.time_min:
                    return False
                if self.time_max is not None and t > self.time_max:
                    return False

        # ── per-column text filters ────────────────────────────────────────
        for col, text in self._col_text.items():
            if text and text not in self._cell(model, src_row, col, src_parent).lower():
                return False

        # ── dropdown value filters ─────────────────────────────────────────
        for col, allowed in self.col_value_filters.items():
            if allowed is None:
                continue
            if self._cell(model, src_row, col, src_parent) not in allowed:
                return False

        # ── global text filter (any column) ───────────────────────────────
        if self.global_text:
            n_cols = model.columnCount()
            if not any(
                self.global_text in self._cell(model, src_row, c, src_parent).lower()
                for c in range(n_cols)
            ):
                return False

        return True


# ── Column-filter header (right-click dropdown) ───────────────────────────────
class FilterHeader(QHeaderView):
    filterRequested = pyqtSignal(int, QPoint)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)

    def mousePressEvent(self, event):
        section = self.logicalIndexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton and section >= 0:
            self.filterRequested.emit(section, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)


# ── Inline column-search row ──────────────────────────────────────────────────
class ColumnSearchBar(QWidget):
    """A row of QLineEdits that mirror the table columns for per-column search."""

    searchChanged = pyqtSignal(int, str)   # col, text

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._edits: List[QLineEdit] = []

    def rebuild(self, column_widths: List[int], headers: List[str]):
        # Remove old widgets
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._edits.clear()

        for i, (w, h) in enumerate(zip(column_widths, headers)):
            edit = QLineEdit()
            edit.setPlaceholderText(f"🔍 {h}")
            edit.setFixedWidth(max(w, 60))
            edit.setStyleSheet(
                "QLineEdit { border: 1px solid #b0c4de; border-radius: 2px; "
                "padding: 1px 4px; font-size: 11px; background: #f0f8ff; }"
                "QLineEdit:focus { border-color: #01696f; background: white; }"
            )
            col = i
            edit.textChanged.connect(lambda text, c=col: self.searchChanged.emit(c, text))
            self._layout.addWidget(edit)
            self._edits.append(edit)

    def clear_all(self):
        for e in self._edits:
            e.blockSignals(True)
            e.clear()
            e.blockSignals(False)


# ── Enhanced table widget (table + search bar + status) ──────────────────────
class SmartTableWidget(QWidget):
    """
    Wraps a QTableView with:
      - per-column inline search bar
      - right-click header dropdown filter
      - bookmark on double-click
      - Ctrl+C copy
      - right-click → Export CSV
      - live row-count label
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proxy: Optional[NumericSortProxy] = None
        self._source_model = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Search bar
        self.search_bar = ColumnSearchBar()
        layout.addWidget(self.search_bar)

        # Table
        self.table = QTableView()
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

        # Status row
        status_row = QHBoxLayout()
        self._row_count_label = QLabel("0 rows")
        self._row_count_label.setStyleSheet("font-size: 11px; color: #555; padding: 2px 4px;")
        self._bookmark_label = QLabel("")
        self._bookmark_label.setStyleSheet("font-size: 11px; color: #c0392b; padding: 2px 4px;")
        clr_btn = QPushButton("Clear filters")
        clr_btn.setFixedHeight(22)
        clr_btn.setStyleSheet("font-size: 11px; padding: 0 8px;")
        clr_btn.clicked.connect(self.clear_all_filters)
        clr_bm_btn = QPushButton("Clear bookmarks")
        clr_bm_btn.setFixedHeight(22)
        clr_bm_btn.setStyleSheet("font-size: 11px; padding: 0 8px;")
        clr_bm_btn.clicked.connect(self.clear_bookmarks)
        status_row.addWidget(self._row_count_label)
        status_row.addWidget(self._bookmark_label)
        status_row.addStretch()
        status_row.addWidget(clr_btn)
        status_row.addWidget(clr_bm_btn)
        layout.addLayout(status_row)

        # Ctrl+C shortcut
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        copy_sc.activated.connect(self._copy_selection)

        # Filter header
        self._filter_header = FilterHeader(Qt.Orientation.Horizontal, self.table)
        self.table.setHorizontalHeader(self._filter_header)
        self._filter_header.filterRequested.connect(self._show_dropdown_filter)

    def set_model(self, source_model):
        self._source_model = source_model
        proxy = NumericSortProxy()
        proxy.setSourceModel(source_model)
        self._proxy = proxy
        self.table.setModel(proxy)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.resizeColumnsToContents()
        self._rebuild_search_bar()
        proxy.rowsInserted.connect(self._update_row_count)
        proxy.rowsRemoved.connect(self._update_row_count)
        proxy.layoutChanged.connect(self._update_row_count)
        self._update_row_count()
        # Connect search bar
        self.search_bar.searchChanged.connect(self._on_col_search)

    def _rebuild_search_bar(self):
        if self._source_model is None:
            return
        n = self._source_model.columnCount()
        widths = [self.table.columnWidth(c) for c in range(n)]
        headers = [
            str(self._source_model.headerData(c, Qt.Orientation.Horizontal) or "")
            for c in range(n)
        ]
        self.search_bar.rebuild(widths, headers)

    def _on_col_search(self, col: int, text: str):
        if self._proxy:
            self._proxy.set_col_text(col, text)
            self._update_row_count()

    def _update_row_count(self):
        if self._proxy is None:
            return
        shown = self._proxy.rowCount()
        total = self._source_model.rowCount() if self._source_model else 0
        self._row_count_label.setText(
            f"{shown} / {total} rows" if shown != total else f"{total} rows"
        )
        bm_count = len(self._source_model._bookmarked) if hasattr(self._source_model, "_bookmarked") else 0
        self._bookmark_label.setText(f"🔖 {bm_count} bookmarks" if bm_count else "")

    def _on_double_click(self, proxy_index: QModelIndex):
        if self._proxy is None or self._source_model is None:
            return
        src_index = self._proxy.mapToSource(proxy_index)
        row = src_index.row()
        if hasattr(self._source_model, "toggle_bookmark"):
            self._source_model.toggle_bookmark(row)
            self._update_row_count()

    def _show_dropdown_filter(self, col: int, global_pos: QPoint):
        if self._proxy is None or self._source_model is None:
            return
        values = [
            str(self._source_model.data(
                self._source_model.index(r, col), Qt.ItemDataRole.DisplayRole) or "")
            for r in range(self._source_model.rowCount())
        ]
        unique = sorted(set(values), key=lambda x: (x == "", x.lower()))
        current = self._proxy.col_value_filters.get(col)

        menu = QMenu(self)
        title = QAction(
            f"Filter: {self._source_model.headerData(col, Qt.Orientation.Horizontal)}", menu)
        title.setEnabled(False); menu.addAction(title); menu.addSeparator()

        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        lw.setMinimumWidth(260)
        lw.setMinimumHeight(min(340, max(120, 26 * min(len(unique) + 1, 12))))
        for val in unique:
            item = QListWidgetItem(val if val != "" else "(blank)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if current is None or val in current
                else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, val)
            lw.addItem(item)

        wa = QWidgetAction(menu); wa.setDefaultWidget(lw); menu.addAction(wa)
        menu.addSeparator()
        sel_all   = menu.addAction("✔ Select all")
        clr_filt  = menu.addAction("✖ Clear filter")
        apply_act = menu.addAction("▶ Apply")

        chosen = menu.exec(global_pos)
        if chosen in (sel_all, clr_filt):
            self._proxy.clear_col_value_filter(col)
        elif chosen == apply_act:
            allowed = {
                lw.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(lw.count())
                if lw.item(i).checkState() == Qt.CheckState.Checked
            }
            if len(allowed) == len(unique):
                self._proxy.clear_col_value_filter(col)
            else:
                self._proxy.set_col_value_filter(col, allowed)
        self._update_row_count()

    def _on_context_menu(self, pos: QPoint):
        menu = QMenu(self)
        copy_act   = menu.addAction("📋 Copy selected rows")
        export_act = menu.addAction("💾 Export table to CSV…")
        menu.addSeparator()
        bm_act     = menu.addAction("🔖 Bookmark selected rows")
        clr_bm_act = menu.addAction("🗑 Clear all bookmarks")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == copy_act:    self._copy_selection()
        elif chosen == export_act: self._export_csv()
        elif chosen == bm_act:    self._bookmark_selected()
        elif chosen == clr_bm_act: self.clear_bookmarks()

    def _copy_selection(self):
        if self._proxy is None:
            return
        indexes = self.table.selectedIndexes()
        if not indexes:
            return
        rows = sorted(set(i.row() for i in indexes))
        cols = sorted(set(i.column() for i in indexes))
        lines = []
        # Header
        lines.append("\t".join(
            str(self._proxy.headerData(c, Qt.Orientation.Horizontal) or "") for c in cols))
        for r in rows:
            lines.append("\t".join(
                str(self._proxy.data(self._proxy.index(r, c)) or "") for c in cols))
        QApplication.clipboard().setText("\n".join(lines))

    def _export_csv(self):
        if self._proxy is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to CSV", os.path.expanduser("~/export.csv"),
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        n_rows = self._proxy.rowCount()
        n_cols = self._proxy.columnCount()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                str(self._proxy.headerData(c, Qt.Orientation.Horizontal) or "")
                for c in range(n_cols)
            ])
            for r in range(n_rows):
                writer.writerow([
                    str(self._proxy.data(self._proxy.index(r, c)) or "")
                    for c in range(n_cols)
                ])
        QMessageBox.information(self, "Export complete",
                                f"Exported {n_rows} rows to:\n{path}")

    def _bookmark_selected(self):
        if self._proxy is None or self._source_model is None:
            return
        for proxy_index in self.table.selectedIndexes():
            src = self._proxy.mapToSource(proxy_index)
            if hasattr(self._source_model, "toggle_bookmark"):
                if not self._source_model.is_bookmarked(src.row()):
                    self._source_model.toggle_bookmark(src.row())
        self._update_row_count()

    def clear_all_filters(self):
        if self._proxy:
            self._proxy.clear_all_filters()
        self.search_bar.clear_all()
        self._update_row_count()

    def clear_bookmarks(self):
        if self._source_model and hasattr(self._source_model, "clear_bookmarks"):
            self._source_model.clear_bookmarks()
        self._update_row_count()

    def set_node_filter(self, ids):
        if self._proxy: self._proxy.set_node_filter(ids); self._update_row_count()

    def set_global_text(self, text):
        if self._proxy: self._proxy.set_global_text(text); self._update_row_count()

    def set_time_range(self, tmin, tmax):
        if self._proxy: self._proxy.set_time_range(tmin, tmax); self._update_row_count()

    def sort_by(self, col, order=Qt.SortOrder.AscendingOrder):
        self.table.sortByColumn(col, order)

    def resize_columns(self):
        self.table.resizeColumnsToContents()
        self._rebuild_search_bar()


# ── Topology view ─────────────────────────────────────────────────────────────
class TopologyView(QGraphicsView):
    nodeClicked      = pyqtSignal(int)
    nodeRightClicked = pyqtSignal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node_infos: Dict[int, NodeInfo] = {}
        self._positions: Dict[int, Tuple[float, float]] = {}
        self._edges: Dict[Tuple[int, int], int] = {}
        self._neighbors_by_node: Dict[int, Set[int]] = {}
        self._selected_node: Optional[int] = None
        self._parent_map: Dict[int, Optional[int]] = {}
        self._intermediate_parents: Set[int] = set()
        self._pdr_map: Dict[int, float] = {}   # node_id → avg PDR %
        self._legend_pos = (15.0, 15.0)
        self._legend_item = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

    def set_parent_map(self, pm):
        self._parent_map = pm
        self._intermediate_parents = {p for p in pm.values() if p is not None}

    def set_pdr_map(self, pdr_map: Dict[int, float]):
        self._pdr_map = pdr_map

    # ── layout helpers ────────────────────────────────────────────────────
    def _build_radio_graph(self, radio_entries):
        directed: Dict[Tuple[int, int], int] = {}
        for entry in radio_entries:
            for rx in entry.receivers:
                if rx != entry.src_node:
                    directed[(entry.src_node, rx)] = directed.get((entry.src_node, rx), 0) + 1
        undirected: Dict[Tuple[int, int], int] = {}
        seen: set = set()
        for (a, b), w1 in directed.items():
            pair = tuple(sorted((a, b)))
            if pair in seen: continue
            undirected[pair] = w1 + directed.get((b, a), 0)
            seen.add(pair)
        return undirected

    def _build_neighbors(self, node_ids, edges):
        nb = {nid: set() for nid in node_ids}
        for (a, b) in edges:
            nb[a].add(b); nb[b].add(a)
        return nb

    def _force_layout(self, node_ids, edges, W=1000, H=700):
        n = len(node_ids)
        if n == 0: return {}
        if n == 1: return {node_ids[0]: (W / 2, H / 2)}
        cx, cy = W / 2, H / 2
        r = min(W, H) * 0.34
        pos: Dict[int, List[float]] = {
            nid: [cx + r * math.cos(2 * math.pi * i / n),
                  cy + r * math.sin(2 * math.pi * i / n)]
            for i, nid in enumerate(sorted(node_ids))
        }
        max_w = max(edges.values()) if edges else 1
        k = math.sqrt(W * H / max(1, n)) * 0.58
        for step in range(260):
            disp = {nid: [0.0, 0.0] for nid in node_ids}
            for i, v in enumerate(node_ids):
                for u in node_ids[i + 1:]:
                    dx = pos[v][0] - pos[u][0]; dy = pos[v][1] - pos[u][1]
                    d = math.hypot(dx, dy) + 0.01
                    f = k * k / d
                    disp[v][0] += dx / d * f; disp[v][1] += dy / d * f
                    disp[u][0] -= dx / d * f; disp[u][1] -= dy / d * f
            for (a, b), w in edges.items():
                dx = pos[a][0] - pos[b][0]; dy = pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) + 0.01
                f = d * d / k * (0.4 + 1.35 * w / max_w)
                ax = dx / d * f; ay = dy / d * f
                disp[a][0] -= ax; disp[a][1] -= ay
                disp[b][0] += ax; disp[b][1] += ay
            temp = max(2.0, 20.0 * (1 - step / 260))
            mg = 70
            for nid in node_ids:
                dx, dy = disp[nid]
                d = math.hypot(dx, dy)
                if d > 0:
                    s = min(temp, d) / d
                    pos[nid][0] += dx * s; pos[nid][1] += dy * s
                pos[nid][0] = min(W - mg, max(mg, pos[nid][0]))
                pos[nid][1] = min(H - mg, max(mg, pos[nid][1]))
        return {nid: (p[0], p[1]) for nid, p in pos.items()}

    def _xy_layout(self, node_ids, node_infos, W=1000, H=700):
        xy_nodes = [(nid, node_infos[nid].xy) for nid in node_ids
                    if nid in node_infos and node_infos[nid].xy is not None]
        if not xy_nodes: return {}
        xs = [xy[0] for _, xy in xy_nodes]; ys = [xy[1] for _, xy in xy_nodes]
        xr = max(max(xs) - min(xs), 1); yr = max(max(ys) - min(ys), 1)
        mg = 80
        positions = {
            nid: (mg + (xy[0] - min(xs)) / xr * (W - 2 * mg),
                  mg + (xy[1] - min(ys)) / yr * (H - 2 * mg))
            for nid, xy in xy_nodes
        }
        no_xy = [nid for nid in node_ids if nid not in positions]
        for i, nid in enumerate(no_xy):
            a = 2 * math.pi * i / max(1, len(no_xy))
            positions[nid] = (W / 2 + 120 * math.cos(a), H / 2 + 120 * math.sin(a))
        return positions

    # ── public draw ───────────────────────────────────────────────────────
    def draw_topology(self, node_infos: List[NodeInfo], radio_entries=None,
                      edge_map=None, use_xy_layout=False):
        self._node_infos = {n.node_id: n for n in node_infos}
        self._selected_node = None
        node_ids = sorted(self._node_infos.keys())

        self._edges = (edge_map if edge_map is not None
                       else self._build_radio_graph(radio_entries or []))
        self._neighbors_by_node = self._build_neighbors(node_ids, self._edges)

        if use_xy_layout:
            self._positions = self._xy_layout(node_ids, self._node_infos) or \
                              self._force_layout(node_ids, self._edges)
        elif self._edges:
            self._positions = self._force_layout(node_ids, self._edges)
        else:
            root_id = 1 if 1 in node_ids else (node_ids[0] if node_ids else 1)
            cx, cy, r = 500, 350, 250
            self._positions = {root_id: (cx, cy)}
            others = [nid for nid in node_ids if nid != root_id]
            for idx, nid in enumerate(others):
                a = 2 * math.pi * idx / max(1, len(others))
                self._positions[nid] = (cx + r * math.cos(a), cy + r * math.sin(a))

        self._render_scene()

    def _node_fill(self, nid, is_root, is_parent, selected, path_to_root, selected_neighbors):
        if selected is None:
            # PDR-based colouring for IoT-LAB
            if self._pdr_map:
                pdr = self._pdr_map.get(nid)
                if pdr is not None:
                    if pdr < 75:   return C_PDR_BAD,  17, QColor("#7a0000"), 1.4
                    elif pdr < 95: return C_PDR_MED,  17, QColor("#7a5c00"), 1.4
            if is_root:   return C_ROOT,     22, QColor("#7a5c00"), 2.0
            elif is_parent: return C_PARENT, 19, QColor("#1a5c2a"), 2.0
            else:           return C_LEAF,   17, QColor("#2f3b45"), 1.4
        else:
            if nid == selected:
                return C_SELECTED, 24, QColor("#7a2e00"), 2.6
            elif nid in path_to_root and nid != selected:
                return C_PATH,     21, QColor("#880e4f"), 2.2
            elif nid in selected_neighbors:
                return C_NEIGHBOR, 20, QColor("#a06a00"), 2.0
            else:
                return QColor(210, 220, 230, 110), 15 if not is_root else 18, \
                       QColor(130, 130, 130, 100), 1.0

    def _render_scene(self):
        if self._legend_item is not None:
            try:
                p = self._legend_item.pos()
                self._legend_pos = (p.x(), p.y())
            except Exception:
                pass

        scene = QGraphicsScene(self)
        self.setScene(scene)
        node_ids = sorted(self._node_infos.keys())
        if not node_ids: return
        root_id = 1 if 1 in node_ids else node_ids[0]
        sel = self._selected_node
        sel_nb = self._neighbors_by_node.get(sel, set()) if sel is not None else set()

        path_to_root: List[int] = []
        path_edges: Set[Tuple[int, int]] = set()
        if sel is not None and self._parent_map:
            path_to_root = get_path_to_root(sel, self._parent_map)
            for i in range(len(path_to_root) - 1):
                a, b = path_to_root[i], path_to_root[i + 1]
                path_edges.add((min(a, b), max(a, b)))

        # Radio range circle
        if sel is not None and sel in self._positions:
            sx, sy = self._positions[sel]
            scene.addEllipse(QRectF(sx - 135, sy - 135, 270, 270),
                             QPen(QColor(255, 140, 0, 120), 2, Qt.PenStyle.DashLine),
                             QBrush(QColor(255, 200, 120, 45)))

        # Edges
        if self._edges:
            max_w = max(self._edges.values())
            for (a, b), w in sorted(self._edges.items(), key=lambda x: x[1]):
                if a not in self._positions or b not in self._positions: continue
                x1, y1 = self._positions[a]; x2, y2 = self._positions[b]
                pair = (min(a, b), max(a, b))
                if sel is None:
                    alpha = 40 + int(110 * w / max_w)
                    lw = 1.0 + 2.6 * w / max_w
                    color = QColor(120, 140, 165, alpha)
                elif pair in path_edges:
                    alpha, lw, color = 255, 4.5, C_PATH
                elif a == sel or b == sel:
                    alpha, lw, color = 230, 3.8, QColor(255, 120, 0, 230)
                elif a in sel_nb and b in sel_nb:
                    alpha, lw, color = 90, 1.6, QColor(140, 170, 210, 90)
                else:
                    alpha, lw, color = 20, 1.0, QColor(180, 180, 180, 20)
                line = scene.addLine(x1, y1, x2, y2, QPen(color, lw))
                line.setToolTip(f"Radio link {a} ↔ {b}  ({w}×)")

        # DODAG parent edges
        if self._parent_map:
            for child, parent in self._parent_map.items():
                if parent is None: continue
                if child not in self._positions or parent not in self._positions: continue
                x1, y1 = self._positions[child]; x2, y2 = self._positions[parent]
                pair = (min(child, parent), max(child, parent))
                if pair in path_edges and sel is not None: continue
                scene.addLine(x1, y1, x2, y2,
                              QPen(QColor(30, 180, 80, 140), 1.5, Qt.PenStyle.DotLine))

        # Nodes
        for nid in node_ids:
            if nid not in self._positions: continue
            x, y = self._positions[nid]
            is_root   = (nid == root_id)
            is_parent = (nid in self._intermediate_parents)
            info      = self._node_infos.get(nid)
            nb        = self._neighbors_by_node.get(nid, set())

            fill, node_r, border, bw = self._node_fill(
                nid, is_root, is_parent, sel, path_to_root, sel_nb)
            text_color = (Qt.GlobalColor.white
                          if fill in (C_PATH,) else Qt.GlobalColor.black)
            if sel is not None and nid not in path_to_root and nid not in sel_nb and nid != sel:
                text_color = Qt.GlobalColor.darkGray

            circle = scene.addEllipse(
                QRectF(x - node_r, y - node_r, node_r * 2, node_r * 2),
                QPen(border, bw), QBrush(fill))
            circle.setData(0, nid)

            display_label = (info.label if (info and info.label and info.label != str(nid))
                             else str(nid))
            txt = scene.addText(display_label)
            font = QFont(); font.setPointSize(9 if len(display_label) > 3 else 10)
            font.setBold(nid == sel or is_root or is_parent)
            txt.setFont(font); txt.setDefaultTextColor(text_color)
            tr = txt.boundingRect()
            txt.setPos(x - tr.width() / 2, y - tr.height() / 2)
            txt.setData(0, nid)

            p = self._parent_map.get(nid)
            role_str = ("Root" if is_root else "Intermediate Parent" if is_parent else "Leaf")
            pdr_str  = (f"  PDR: {self._pdr_map[nid]:.1f}%" if nid in self._pdr_map else "")
            xy_str   = (f"  XY: {info.xy}" if info and info.xy else "")
            tip = (f"Node {nid} [{role_str}]{pdr_str}{xy_str}\n"
                   f"Label: {info.label if info else '-'}\n"
                   f"Parent: {p if p is not None else '-'}\n"
                   f"IPv6: {info.link_local if info else '-'}\n"
                   f"Radio neighbors: {len(nb)}: {', '.join(map(str, sorted(nb))) or '-'}")
            circle.setToolTip(tip); txt.setToolTip(tip)

        # Legend
        lx, ly = self._legend_pos
        legend_bg = scene.addRect(QRectF(lx, ly, 10, 10),
                                  QPen(Qt.PenStyle.NoPen), QBrush(Qt.BrushStyle.NoBrush))
        legend_bg.setFlag(legend_bg.GraphicsItemFlag.ItemIsMovable, True)
        legend_bg.setCursor(Qt.CursorShape.SizeAllCursor)
        legend_bg.setToolTip("Drag to move legend")

        legend = scene.addText("")
        legend.setDefaultTextColor(Qt.GlobalColor.black)
        lf = QFont(); lf.setPointSize(9); legend.setFont(lf)
        legend.setParentItem(legend_bg)

        if sel is None:
            if self._pdr_map:
                legend.setPlainText(
                    "RDF Topology  (IoT-LAB)\n"
                    "🟡 Yellow = Root\n"
                    "🟢 Green  = Intermediate Parent\n"
                    "🔵 Blue   = Leaf\n"
                    "🔴 Red    = PDR < 75%\n"
                    "🟡 Yellow = PDR 75–94%\n"
                    "── Dashed green = DODAG parent\n"
                    "Click node · Right-click for options"
                )
            else:
                legend.setPlainText(
                    "RPL Topology  (Cooja)\n"
                    "🟡 Yellow = Root (Node 1)\n"
                    "🟢 Green  = Intermediate Parent\n"
                    "🔵 Blue   = Leaf\n"
                    "── Dashed green = DODAG parent\n"
                    "Click node · Right-click for options"
                )
        else:
            path_str = " → ".join(map(str, path_to_root))
            legend.setPlainText(
                f"Selected: Node {sel}\n"
                f"Path: {path_str}\n"
                f"Neighbors: {len(sel_nb)}\n"
                "🔴 Pink = path to root\n"
                "Click same node to deselect"
            )
        legend.setPos(8, 6)
        br = legend.boundingRect()
        legend_bg.setRect(QRectF(lx, ly, br.width() + 16, br.height() + 12))
        legend_bg.setZValue(100); legend.setZValue(101)
        self._legend_item = legend_bg

        self.setSceneRect(scene.itemsBoundingRect().adjusted(-30, -30, 30, 30))
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scene() and not self.sceneRect().isNull():
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, event: QMouseEvent):
        item = self.itemAt(event.position().toPoint())
        nid = item.data(0) if item else None
        if isinstance(nid, int):
            if event.button() == Qt.MouseButton.LeftButton:
                self._selected_node = None if self._selected_node == nid else nid
                self._render_scene()
                self.nodeClicked.emit(nid)
            elif event.button() == Qt.MouseButton.RightButton:
                self.nodeRightClicked.emit(nid, event.globalPosition().toPoint())
                return
        super().mousePressEvent(event)


# ── DODAG tab ─────────────────────────────────────────────────────────────────
class DodagAnalysisWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8); layout.setSpacing(8)

        grp1 = QGroupBox("1  DODAG Formation Timeline")
        grp1.setStyleSheet("QGroupBox { font-weight: bold; font-size: 13px; padding-top: 8px; }")
        g1l = QVBoxLayout(grp1)
        self.join_widget = SmartTableWidget()
        g1l.addWidget(self.join_widget)
        layout.addWidget(grp1, 1)

        grp2 = QGroupBox("2  Nodes Acting as Intermediate Parents")
        grp2.setStyleSheet("QGroupBox { font-weight: bold; font-size: 13px; padding-top: 8px; }")
        g2l = QVBoxLayout(grp2)
        self.parent_widget = SmartTableWidget()
        g2l.addWidget(self.parent_widget)
        layout.addWidget(grp2, 1)

        legend_frame = QFrame()
        legend_frame.setFrameShape(QFrame.Shape.StyledPanel)
        legend_frame.setStyleSheet("background:#f9f9f9; border:1px solid #ddd; border-radius:4px;")
        ll = QHBoxLayout(legend_frame); ll.setContentsMargins(12, 6, 12, 6)
        for color_hex, label in [
            ("#1a5276", "Root / Server"),
            ("#1e8449", "Intermediate Parent"),
            ("#e8e8e8", "Leaf (client only)"),
        ]:
            dot = QLabel("  ")
            dot.setStyleSheet(f"background:{color_hex}; border-radius:8px; "
                              f"min-width:16px; max-width:16px; min-height:16px; max-height:16px;")
            lbl = QLabel(label); lbl.setStyleSheet("font-size:11px; color:#333;")
            ll.addWidget(dot); ll.addWidget(lbl); ll.addSpacing(16)
        ll.addStretch()
        layout.addWidget(legend_frame, 0)

    def load_data(self, join_events, parent_map):
        self.join_widget.set_model(DodagJoinModel(join_events, parent_map))
        self.join_widget.sort_by(1)
        self.parent_widget.set_model(IntermediateParentModel(parent_map, join_events))


# ── IoT-LAB analytics tab ─────────────────────────────────────────────────────
class IotlabAnalyticsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8); layout.setSpacing(8)

        inner = QTabWidget(); layout.addWidget(inner)

        # PDR
        pdr_w = QWidget(); pdr_l = QVBoxLayout(pdr_w); pdr_l.setContentsMargins(4, 4, 4, 4)
        pdr_l.addWidget(QLabel(
            "Packet Delivery Ratio per (receiver, source) — latest PDR log line per pair.\n"
            "🟢 ≥95%   🟡 75–94%   🔴 <75%"))
        self.pdr_widget = SmartTableWidget(); pdr_l.addWidget(self.pdr_widget)
        inner.addTab(pdr_w, "PDR Summary")

        # AoI
        aoi_w = QWidget(); aoi_l = QVBoxLayout(aoi_w); aoi_l.setContentsMargins(4, 4, 4, 4)
        aoi_l.addWidget(QLabel(
            "Age of Information per (receiver, source) — from AOI_OK events.\n"
            "🟢 avg ≤50% TAoI   🟡 50–90%   🔴 >90%"))
        self.aoi_widget = SmartTableWidget(); aoi_l.addWidget(self.aoi_widget)
        inner.addTab(aoi_w, "AoI Summary")

        # RDF events
        rdf_w = QWidget(); rdf_l = QVBoxLayout(rdf_w); rdf_l.setContentsMargins(4, 4, 4, 4)
        self.rdf_widget = SmartTableWidget(); rdf_l.addWidget(self.rdf_widget)
        inner.addTab(rdf_w, "RDF Events")

    def load_data(self, rdf_events, pdr_rows, aoi_rows):
        self.pdr_widget.set_model(PdrTableModel(pdr_rows))
        self.pdr_widget.sort_by(5, Qt.SortOrder.DescendingOrder)

        self.aoi_widget.set_model(AoiTableModel(aoi_rows))
        self.aoi_widget.sort_by(6)

        rdf_model = RdfEventTableModel(rdf_events)
        self.rdf_widget.set_model(rdf_model)
        self.rdf_widget.sort_by(0)
        if self.rdf_widget.table.model() and self.rdf_widget.table.model().columnCount() >= 14:
            self.rdf_widget.table.setColumnWidth(13, 320)


# ── Main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, log_path=None, radio_path=None, timeline_path=None):
        super().__init__()
        self.setWindowTitle("RPL Log Analyzer")
        self._log_type = "cooja"
        self._parent_map: Dict[int, Optional[int]] = {}
        self.nodes: List[NodeInfo] = []

        central = QWidget(); self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(6, 6, 6, 6); root_layout.setSpacing(4)

        # ── Global filter toolbar ──────────────────────────────────────────
        toolbar = QFrame()
        toolbar.setStyleSheet("QFrame { background: #f5f8fa; border-bottom: 1px solid #dde3ea; }")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4); tb_layout.setSpacing(8)

        tb_layout.addWidget(QLabel("🔍 Global search:"))
        self.global_search = QLineEdit()
        self.global_search.setPlaceholderText("Search across all tabs…")
        self.global_search.setMinimumWidth(200)
        tb_layout.addWidget(self.global_search)

        tb_layout.addWidget(QLabel("Node IDs:"))
        self.node_filter_edit = QLineEdit()
        self.node_filter_edit.setPlaceholderText("e.g. 1,3,5")
        self.node_filter_edit.setMaximumWidth(140)
        tb_layout.addWidget(self.node_filter_edit)

        tb_layout.addWidget(QLabel("Time ≥"))
        self.time_min_spin = QDoubleSpinBox()
        self.time_min_spin.setRange(0, 999999); self.time_min_spin.setDecimals(3)
        self.time_min_spin.setSpecialValueText("—"); self.time_min_spin.setValue(0)
        self.time_min_spin.setMaximumWidth(100)
        tb_layout.addWidget(self.time_min_spin)

        tb_layout.addWidget(QLabel("≤"))
        self.time_max_spin = QDoubleSpinBox()
        self.time_max_spin.setRange(0, 999999); self.time_max_spin.setDecimals(3)
        self.time_max_spin.setSpecialValueText("—"); self.time_max_spin.setValue(0)
        self.time_max_spin.setMaximumWidth(100)
        tb_layout.addWidget(self.time_max_spin)

        apply_time_btn = QPushButton("Apply time")
        apply_time_btn.setFixedHeight(26)
        apply_time_btn.clicked.connect(self._apply_time_filter)
        tb_layout.addWidget(apply_time_btn)

        clear_time_btn = QPushButton("Clear time")
        clear_time_btn.setFixedHeight(26)
        clear_time_btn.clicked.connect(self._clear_time_filter)
        tb_layout.addWidget(clear_time_btn)

        tb_layout.addStretch()

        reset_btn = QPushButton("Reset all filters")
        reset_btn.setFixedHeight(26)
        reset_btn.clicked.connect(self._reset_all_filters)
        tb_layout.addWidget(reset_btn)

        root_layout.addWidget(toolbar)

        # ── Tabs ───────────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        # Smart table widgets
        self.nodes_widget    = SmartTableWidget()
        self.log_widget      = SmartTableWidget()
        self.flows_widget    = SmartTableWidget()
        self.radio_widget    = SmartTableWidget()

        # Topology tab
        self.topology_tab = QWidget()
        topo_layout = QHBoxLayout(self.topology_tab)
        topo_layout.setContentsMargins(8, 8, 8, 8); topo_layout.setSpacing(8)

        self.topology_info_box = QFrame()
        self.topology_info_box.setFixedWidth(290)
        self.topology_info_box.setFrameShape(QFrame.Shape.StyledPanel)
        self.topology_info_box.setStyleSheet(
            "QFrame { background:#fff; border:1px solid #ccc; border-radius:6px; }")
        info_layout = QVBoxLayout(self.topology_info_box)
        info_layout.setContentsMargins(12, 12, 12, 12); info_layout.setSpacing(8)
        info_title = QLabel("Node Details")
        info_title.setStyleSheet("font-weight:bold; font-size:14px; color:#1a1a1a; background:transparent;")
        info_layout.addWidget(info_title)
        self.topology_info_label = QLabel("Click a node to view details.")
        self.topology_info_label.setWordWrap(True)
        self.topology_info_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.topology_info_label.setStyleSheet(
            "font-size:12px; color:#2c2c2c; background:transparent; line-height:1.6;")
        info_layout.addWidget(self.topology_info_label, 1)

        key_frame = QFrame()
        key_frame.setStyleSheet("background:#f0f4f8; border:1px solid #dde3ea; border-radius:4px;")
        kl = QVBoxLayout(key_frame); kl.setContentsMargins(8, 6, 8, 6); kl.setSpacing(3)
        kl.addWidget(QLabel("<b style='font-size:11px;'>Node Color Key</b>"))
        for color_hex, label in [
            ("#ffd54f", "Root (Node 1)"),
            ("#66bb6a", "Intermediate Parent"),
            ("#7ec8ff", "Leaf (client only)"),
            ("#ff8c42", "Selected node"),
            ("#e91e63", "Path to root"),
            ("#ef9a9a", "PDR < 75% (IoT-LAB)"),
            ("#fff59d", "PDR 75–94% (IoT-LAB)"),
        ]:
            row = QHBoxLayout(); row.setSpacing(6)
            dot = QLabel("  ")
            dot.setStyleSheet(f"background:{color_hex}; border-radius:7px; "
                              f"min-width:14px; max-width:14px; min-height:14px; max-height:14px; "
                              f"border:1px solid #aaa;")
            lbl = QLabel(label); lbl.setStyleSheet("font-size:11px; color:#2c2c2c;")
            row.addWidget(dot); row.addWidget(lbl); row.addStretch()
            kl.addLayout(row)
        info_layout.addWidget(key_frame)

        # "Show only this node" button
        self.topo_filter_btn = QPushButton("Show only this node in tables")
        self.topo_filter_btn.setEnabled(False)
        self.topo_filter_btn.clicked.connect(self._topo_filter_selected)
        info_layout.addWidget(self.topo_filter_btn)

        self.topology_view = TopologyView()
        topo_layout.addWidget(self.topology_info_box, 0)
        topo_layout.addWidget(self.topology_view, 1)

        # DODAG + IoT-LAB tabs
        self.dodag_widget   = DodagAnalysisWidget()
        self.iotlab_widget  = IotlabAnalyticsWidget()

        self.tabs.addTab(self.nodes_widget,   "Nodes")
        self.tabs.addTab(self.log_widget,     "Raw Log")
        self.tabs.addTab(self.flows_widget,   "Flows")
        self.tabs.addTab(self.radio_widget,   "Radio")
        self.tabs.addTab(self.topology_tab,   "Topology")
        self.tabs.addTab(self.dodag_widget,   "DODAG Analysis")
        self.tabs.addTab(self.iotlab_widget,  "IoT-LAB Analytics")

        # ── Connections ────────────────────────────────────────────────────
        self.global_search.textChanged.connect(self._on_global_search)
        self.node_filter_edit.textChanged.connect(self._on_node_filter)
        self.topology_view.nodeClicked.connect(self._on_topo_node_clicked)
        self.topology_view.nodeRightClicked.connect(self._on_topo_node_right_clicked)

        # Ctrl+F → focus global search
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.global_search.setFocus())

        self._all_smart_widgets: List[SmartTableWidget] = [
            self.nodes_widget, self.log_widget,
            self.flows_widget, self.radio_widget,
        ]
        self._selected_topo_node: Optional[int] = None

        if log_path is None:
            log_path, _ = QFileDialog.getOpenFileName(
                self, "Open log file", "",
                "Text / log files (*.txt *.log *);;All files (*)")
            if not log_path:
                QMessageBox.warning(self, "No file", "No log file selected.")
                sys.exit(0)

        self.load_log(log_path, radio_path, timeline_path)

    # ── Filter helpers ─────────────────────────────────────────────────────
    def _parse_node_ids(self) -> Optional[Set[int]]:
        text = self.node_filter_edit.text().strip()
        if not text: return None
        ids: Set[int] = set()
        for p in re.split(r"[,\s]+", text):
            try: ids.add(int(p))
            except ValueError: pass
        return ids or None

    def _on_global_search(self, text):
        for w in self._all_smart_widgets:
            w.set_global_text(text)
        self._update_tab_badges()

    def _on_node_filter(self, _=None):
        ids = self._parse_node_ids()
        for w in self._all_smart_widgets:
            w.set_node_filter(ids)
        self._update_tab_badges()

    def _apply_time_filter(self):
        tmin = self.time_min_spin.value() if self.time_min_spin.value() > 0 else None
        tmax = self.time_max_spin.value() if self.time_max_spin.value() > 0 else None
        for w in self._all_smart_widgets:
            w.set_time_range(tmin, tmax)
        self._update_tab_badges()

    def _clear_time_filter(self):
        self.time_min_spin.setValue(0); self.time_max_spin.setValue(0)
        for w in self._all_smart_widgets:
            w.set_time_range(None, None)
        self._update_tab_badges()

    def _reset_all_filters(self):
        self.global_search.clear()
        self.node_filter_edit.clear()
        self.time_min_spin.setValue(0); self.time_max_spin.setValue(0)
        for w in self._all_smart_widgets:
            w.clear_all_filters()
        self._update_tab_badges()

    def _update_tab_badges(self):
        pairs = [
            (0, self.nodes_widget),
            (1, self.log_widget),
            (2, self.flows_widget),
            (3, self.radio_widget),
        ]
        names = ["Nodes", "Raw Log", "Flows", "Radio"]
        for idx, (tab_idx, w) in enumerate(pairs):
            if w._proxy is None: continue
            shown = w._proxy.rowCount()
            total = w._source_model.rowCount() if w._source_model else 0
            label = names[idx]
            if shown != total:
                self.tabs.setTabText(tab_idx, f"{label} ({shown}/{total})")
            else:
                self.tabs.setTabText(tab_idx, f"{label} ({total})")

    # ── Topology interactions ──────────────────────────────────────────────
    def _on_topo_node_clicked(self, node_id: int):
        self._selected_topo_node = node_id
        self.topo_filter_btn.setEnabled(True)
        info = next((n for n in self.nodes if n.node_id == node_id), None)
        nb_ids = sorted(self.topology_view._neighbors_by_node.get(node_id, set()))
        parent_id = self._parent_map.get(node_id)
        path = get_path_to_root(node_id, self._parent_map)
        path_str = " → ".join(map(str, path)) if len(path) > 1 else "Direct to root"
        is_parent = node_id in self.topology_view._intermediate_parents
        role_str = ("Root (Server)" if node_id == 1
                    else "Intermediate Parent" if is_parent else "Leaf (client only)")
        children = [c for c, p in self._parent_map.items() if p == node_id]

        lines = [
            f"Node: {node_id}",
            f"Label: {info.label if info else '-'}",
            f"Role: {role_str}",
            f"Depth: {len(path) - 1}",
            f"Parent: {parent_id if parent_id is not None else '— (root)'}",
            f"Path: {path_str}",
        ]
        if children:
            lines.append(f"Children ({len(children)}): {', '.join(map(str, sorted(children)))}")
        lines.append("──────────────────")
        if info:
            if info.xy:   lines.append(f"XY: {info.xy}")
            if info.hw_node_id: lines.append(f"HW Node ID: {info.hw_node_id}")
            if info.hw_idx:     lines.append(f"HW Idx: {info.hw_idx}")
            lines += [
                f"PANID: {info.panid or '-'}",
                f"Channel: {info.channel or '-'}",
                f"MAC: {info.mac or '-'}",
                f"IPv6: {info.link_local or '-'}",
                f"First: {f'{info.first_time:.3f}' if info.first_time is not None else '-'} s",
                f"Last:  {f'{info.last_time:.3f}'  if info.last_time  is not None else '-'} s",
            ]
        if node_id in self.topology_view._pdr_map:
            lines.append(f"Avg PDR: {self.topology_view._pdr_map[node_id]:.1f}%")
        lines += [f"Radio neighbors: {len(nb_ids)}",
                  f"Neighbor IDs: {', '.join(map(str, nb_ids)) or '-'}"]
        self.topology_info_label.setText("\n".join(lines))
        self.statusBar().showMessage(
            f"Node {node_id} [{role_str}] | Depth: {len(path)-1} | Parent: {parent_id}")

    def _on_topo_node_right_clicked(self, node_id: int, global_pos: QPoint):
        menu = QMenu(self)
        filter_act  = menu.addAction(f"🔍 Filter tables to Node {node_id}")
        add_act     = menu.addAction(f"➕ Add Node {node_id} to filter")
        clear_act   = menu.addAction("✖ Clear node filter")
        chosen = menu.exec(global_pos)
        if chosen == filter_act:
            self.node_filter_edit.setText(str(node_id))
        elif chosen == add_act:
            existing = self.node_filter_edit.text().strip()
            self.node_filter_edit.setText(
                f"{existing},{node_id}" if existing else str(node_id))
        elif chosen == clear_act:
            self.node_filter_edit.clear()

    def _topo_filter_selected(self):
        if self._selected_topo_node is not None:
            self.node_filter_edit.setText(str(self._selected_topo_node))
            self.tabs.setCurrentIndex(1)   # jump to Raw Log

    # ── Data loading ───────────────────────────────────────────────────────
    def load_log(self, log_path: str, radio_path=None, timeline_path=None):
        try:
            entries, log_type, label_to_id = parse_log(log_path)
        except Exception as e:
            QMessageBox.critical(self, "Parse error", f"Failed to parse log:\n{e}")
            return

        self._log_type = log_type
        if not entries:
            QMessageBox.warning(self, "Empty", "No log entries parsed."); return

        self.setWindowTitle(
            f"RPL Log Analyzer  —  "
            f"{'IoT-LAB Hardware' if log_type == 'iotlab' else 'Cooja Simulation'}  "
            f"[{os.path.basename(log_path)}]")

        nodes_map = build_node_infos(entries, label_to_id)
        self.flows = build_udp_flows(entries)

        # Tx/Rx counts from flows
        counts: Dict[int, Dict[str, int]] = {}
        for f in self.flows:
            c = counts.setdefault(f.src_node, {"tx": 0, "rx": 0, "missed": 0})
            c["tx"] += 1
            if f.resp_time is not None: c["rx"] += 1
            else: c["missed"] += 1
        for nid, c in counts.items():
            n = nodes_map.setdefault(nid, NodeInfo(node_id=nid))
            n.tx = c["tx"]; n.rx = c["rx"]; n.missed_tx = c["missed"]

        self.nodes = list(nodes_map.values())
        radio_entries: List = []
        timeline_entries: List = []

        # ── Cooja optional files ───────────────────────────────────────────
        if log_type == "cooja":
            if radio_path and os.path.isfile(radio_path):
                try: radio_entries = parse_radio_log(radio_path)
                except Exception as e: self.statusBar().showMessage(f"Radio log error: {e}")
            else:
                for cand in ["rm.txt", "rm", "radiolog.txt", "radio.txt"]:
                    p = os.path.join(os.path.dirname(log_path), cand)
                    if os.path.isfile(p):
                        try: radio_entries = parse_radio_log(p); break
                        except Exception: pass

            if timeline_path and os.path.isfile(timeline_path):
                try: timeline_entries = parse_timeline(timeline_path)
                except Exception as e: self.statusBar().showMessage(f"Timeline error: {e}")
            else:
                for cand in ["timedetail", "timeline", "timeline1", "timedetail.txt"]:
                    p = os.path.join(os.path.dirname(log_path), cand)
                    if os.path.isfile(p):
                        try: timeline_entries = parse_timeline(p); break
                        except Exception: pass

        # ── DODAG ─────────────────────────────────────────────────────────
        self._parent_map = get_parent_map(entries)
        self.topology_view.set_parent_map(self._parent_map)
        join_events = build_dodag_events(entries)

        # ── IoT-LAB ───────────────────────────────────────────────────────
        rdf_events: List[RdfEvent] = []
        iotlab_edge_map: Dict = {}
        pdr_rows: List[Dict] = []
        aoi_rows: List[Dict] = []
        pdr_map_for_topo: Dict[int, float] = {}

        if log_type == "iotlab":
            rdf_events    = parse_rdf_events(entries)
            iotlab_edge_map = build_iotlab_radio_graph_from_nodes(rdf_events, nodes_map)
            pdr_rows      = build_iotlab_pdr_summary(rdf_events, nodes_map)
            aoi_rows      = build_iotlab_aoi_summary(rdf_events, nodes_map)
            # Build per-node avg PDR for topology colouring
            pdr_buckets: Dict[int, List[float]] = {}
            for r in pdr_rows:
                if r["pdr_pct"] is not None:
                    pdr_buckets.setdefault(r["node_id"], []).append(r["pdr_pct"])
            pdr_map_for_topo = {
                nid: round(sum(v) / len(v), 1) for nid, v in pdr_buckets.items()
            }
        self.topology_view.set_pdr_map(pdr_map_for_topo)

        # ── Models ────────────────────────────────────────────────────────
        show_label = (log_type == "iotlab")
        log_model   = LogTableModel(entries, show_label=show_label)
        nodes_model = (IotlabNodeTableModel(self.nodes, pdr_rows)
                       if log_type == "iotlab"
                       else NodeTableModel(self.nodes))
        flows_model = FlowTableModel(self.flows)
        radio_model = RadioTableModel(radio_entries)

        self.nodes_widget.set_model(nodes_model)
        self.log_widget.set_model(log_model)
        self.flows_widget.set_model(flows_model)
        self.radio_widget.set_model(radio_model)

        self.nodes_widget.sort_by(0)
        self.log_widget.sort_by(0)
        self.flows_widget.sort_by(3)
        self.radio_widget.sort_by(0)

        if radio_model.columnCount() >= 5:
            self.radio_widget.table.setColumnWidth(4, 280)

        # ── Topology ──────────────────────────────────────────────────────
        self.topology_view.draw_topology(
            self.nodes,
            radio_entries=radio_entries if log_type == "cooja" else None,
            edge_map=iotlab_edge_map   if log_type == "iotlab" else None,
            use_xy_layout=(log_type == "iotlab"),
        )

        # ── DODAG / IoT-LAB tabs ──────────────────────────────────────────
        self.dodag_widget.load_data(join_events, self._parent_map)
        if log_type == "iotlab":
            self.iotlab_widget.load_data(rdf_events, pdr_rows, aoi_rows)

        # ── Tab visibility ─────────────────────────────────────────────────
        dodag_idx  = self.tabs.indexOf(self.dodag_widget)
        iotlab_idx = self.tabs.indexOf(self.iotlab_widget)
        flows_idx  = self.tabs.indexOf(self.flows_widget)
        radio_idx  = self.tabs.indexOf(self.radio_widget)

        if log_type == "iotlab":
            self.tabs.setTabVisible(dodag_idx,  False)
            self.tabs.setTabVisible(iotlab_idx, True)
            if not self.flows: self.tabs.setTabVisible(flows_idx, False)
            if not radio_entries: self.tabs.setTabVisible(radio_idx, False)
        else:
            self.tabs.setTabVisible(iotlab_idx, False)
            self.tabs.setTabVisible(dodag_idx,  True)

        self.tabs.setCurrentIndex(0)

        # ── Status bar ─────────────────────────────────────────────────────
        parts = [
            f"{'IoT-LAB' if log_type == 'iotlab' else 'Cooja'}",
            f"{len(entries)} lines",
            f"{len(self.nodes)} nodes",
            f"{len(self.flows)} flows",
        ]
        if log_type == "cooja":
            parts += [f"{len(radio_entries)} radio frames",
                      f"{len(timeline_entries)} timeline events"]
        else:
            parts.append(f"{len(rdf_events)} RDF events")
        self.statusBar().showMessage("  |  ".join(parts))
        self._update_tab_badges()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,     QColor("white"))
    pal.setColor(QPalette.ColorRole.Base,       QColor("white"))
    pal.setColor(QPalette.ColorRole.Text,       QColor("black"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("black"))
    app.setPalette(pal)
    app.setStyleSheet("""
        QTabBar::tab { color:#222; padding:6px 14px; font-size:13px; }
        QTabBar::tab:selected { font-weight:bold; color:#01696f; border-bottom:2px solid #01696f; }
        QGroupBox { border:1px solid #ccc; border-radius:6px; margin-top:8px; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 4px; }
        QScrollBar:vertical { background:#f0f0f0; width:12px; }
        QScrollBar::handle:vertical { background:#d0d0d0; min-height:20px; border-radius:4px; }
        QScrollBar::handle:vertical:hover { background:#b0b0b0; }
        QScrollBar:horizontal { background:#f0f0f0; height:12px; }
        QScrollBar::handle:horizontal { background:#d0d0d0; min-width:20px; border-radius:4px; }
        QScrollBar::handle:horizontal:hover { background:#b0b0b0; }
        QHeaderView::section { background:#f0f4f8; border:1px solid #dde3ea;
                               padding:4px 6px; font-size:12px; }
        QHeaderView::section:hover { background:#e0eaf4; }
    """)

    log_path      = sys.argv[1] if len(sys.argv) > 1 else None
    radio_path    = sys.argv[2] if len(sys.argv) > 2 else None
    timeline_path = sys.argv[3] if len(sys.argv) > 3 else None

    win = MainWindow(log_path, radio_path, timeline_path)
    win.resize(1500, 900)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

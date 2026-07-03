# models.py
# Table models for Cooja and IoT-LAB log viewer.
# All numeric columns sort numerically (not lexicographically).

from typing import List, Dict, Optional
from PyQt6.QtCore import QAbstractTableModel, Qt, QModelIndex, QVariant
from PyQt6.QtGui import QColor, QBrush

from parser import LogEntry, NodeInfo, UdpFlow, RadioEntry, DodagJoinEvent, RdfEvent

# Sentinel role used to return raw numeric values for proper sorting
_SORT_ROLE = Qt.ItemDataRole.UserRole + 1


def _sort_val(display_val):
    """Try to convert display value to float for numeric sort."""
    try:
        return float(str(display_val).replace(",", ""))
    except (ValueError, TypeError):
        return display_val


# ── Bookmark support mixin ────────────────────────────────────────────────────

class BookmarkMixin:
    """Mixin that adds per-row bookmark (highlight) toggling."""

    def _init_bookmarks(self):
        self._bookmarked: set = set()

    def toggle_bookmark(self, row: int):
        if row in self._bookmarked:
            self._bookmarked.discard(row)
        else:
            self._bookmarked.add(row)
        idx_a = self.index(row, 0)
        idx_b = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(idx_a, idx_b, [Qt.ItemDataRole.BackgroundRole])

    def is_bookmarked(self, row: int) -> bool:
        return row in self._bookmarked

    def bookmarked_rows(self) -> list:
        return sorted(self._bookmarked)

    def clear_bookmarks(self):
        self._bookmarked.clear()
        self.layoutChanged.emit()


# ── Log table ─────────────────────────────────────────────────────────────────

class LogTableModel(BookmarkMixin, QAbstractTableModel):
    _LEVEL_COLORS = {
        "WARN":    QColor("#fff3cd"),
        "WARNING": QColor("#fff3cd"),
        "ERR":     QColor("#f8d7da"),
        "ERROR":   QColor("#f8d7da"),
        "DEBUG":   QColor("#e8f4fd"),
        "RAW":     QColor("#f5f5f5"),
    }

    def __init__(self, entries: List[LogEntry], show_label: bool = False):
        super().__init__()
        self._init_bookmarks()
        self._entries = entries
        self._show_label = show_label
        if show_label:
            self._headers = ["Time", "Node", "Label", "Level", "Module", "Message"]
        else:
            self._headers = ["Time", "Node", "Level", "Module", "Message"]

    def rowCount(self, parent=QModelIndex()): return len(self._entries)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        e = self._entries[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row):
                return QColor("#ffe0b2")   # orange bookmark
            return self._LEVEL_COLORS.get(e.level.upper(), QVariant())

        if role == Qt.ItemDataRole.ForegroundRole:
            if e.level.upper() in ("ERR", "ERROR"):
                return QColor("#721c24")
            return QVariant()

        if role == Qt.ItemDataRole.ToolTipRole:
            return f"[{e.time_str}] Node {e.node_id} ({e.raw_node_label})\n{e.level}:{e.module}\n{e.message}"

        if role not in (Qt.ItemDataRole.DisplayRole, _SORT_ROLE):
            return QVariant()

        if self._show_label:
            vals = [e.time_str, e.node_id, e.raw_node_label, e.level, e.module, e.message]
        else:
            vals = [e.time_str, e.node_id, e.level, e.module, e.message]

        v = vals[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 1) else v
        return v

    def entry_at(self, row): return self._entries[row]


# ── Node table ────────────────────────────────────────────────────────────────

class NodeTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, nodes: List[NodeInfo]):
        super().__init__()
        self._init_bookmarks()
        self._nodes = nodes
        self._headers = ["Node", "PANID", "Channel", "MAC", "Link-local IPv6",
                         "First (s)", "Last (s)", "Tx", "Rx", "MissedTx"]

    def rowCount(self, parent=QModelIndex()): return len(self._nodes)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        n = self._nodes[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        raw = [
            n.node_id, n.panid or "", n.channel or "", n.mac or "",
            n.link_local or "",
            f"{n.first_time:.3f}" if n.first_time is not None else "",
            f"{n.last_time:.3f}"  if n.last_time  is not None else "",
            n.tx, n.rx, n.missed_tx,
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 5, 6, 7, 8, 9) else v
        return v


# ── IoT-LAB node table ────────────────────────────────────────────────────────

class IotlabNodeTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, nodes: List[NodeInfo], pdr_rows: List[Dict]):
        super().__init__()
        self._init_bookmarks()
        self._nodes = nodes
        pdr_map: Dict[int, List[float]] = {}
        for r in pdr_rows:
            if r["pdr_pct"] is not None:
                pdr_map.setdefault(r["node_id"], []).append(r["pdr_pct"])
        self._avg_pdr: Dict[int, float] = {
            nid: round(sum(v) / len(v), 1) for nid, v in pdr_map.items()
        }
        self._headers = [
            "Node ID", "Label", "HW Node ID", "HW Idx", "PANID", "Channel",
            "MAC", "Link-local IPv6", "XY Position",
            "First (s)", "Last (s)", "Avg PDR %",
        ]

    def rowCount(self, parent=QModelIndex()): return len(self._nodes)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        n = self._nodes[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            pdr = self._avg_pdr.get(n.node_id)
            if pdr is not None:
                if pdr >= 95:   return QColor("#e8f5e9")
                elif pdr >= 75: return QColor("#fff9c4")
                else:           return QColor("#ffebee")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        pdr = self._avg_pdr.get(n.node_id)
        raw = [
            n.node_id,
            n.label or "",
            str(n.hw_node_id) if n.hw_node_id is not None else "",
            str(n.hw_idx)     if n.hw_idx     is not None else "",
            n.panid or "", n.channel or "", n.mac or "", n.link_local or "",
            f"({n.xy[0]},{n.xy[1]})" if n.xy else "",
            f"{n.first_time:.3f}" if n.first_time is not None else "",
            f"{n.last_time:.3f}"  if n.last_time  is not None else "",
            f"{pdr:.1f}" if pdr is not None else "-",
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 2, 3, 9, 10, 11) else v
        return v


# ── Flow table ────────────────────────────────────────────────────────────────

class FlowTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, flows: List[UdpFlow]):
        super().__init__()
        self._init_bookmarks()
        self._flows = flows
        self._headers = ["Src node", "Dst addr", "Seq",
                         "Send time (s)", "Resp time (s)", "RTT (s)", "Success"]

    def rowCount(self, parent=QModelIndex()): return len(self._flows)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        f = self._flows[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            if col == 6:
                return QColor("#e8f5e9") if f.resp_time is not None else QColor("#ffebee")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        raw = [
            f.src_node, f.dst_addr, f.seq,
            f"{f.send_time:.3f}",
            f"{f.resp_time:.3f}" if f.resp_time is not None else "",
            f"{f.rtt:.3f}"       if f.rtt       is not None else "",
            "Yes" if f.resp_time is not None else "No",
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 2, 3, 4, 5) else v
        return v

    def flow_at(self, row): return self._flows[row]


# ── Radio table ───────────────────────────────────────────────────────────────

class RadioTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, entries: List[RadioEntry]):
        super().__init__()
        self._init_bookmarks()
        self._entries = entries
        self._headers = ["Time (s)", "Src node", "Receivers", "Length", "Payload (hex)"]

    def rowCount(self, parent=QModelIndex()): return len(self._entries)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        e = self._entries[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        raw = [
            f"{e.time_s:.3f}",
            e.src_node,
            ",".join(str(r) for r in e.receivers),
            e.length,
            e.payload_hex,
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 1, 3) else v
        return v


# ── DODAG join table ──────────────────────────────────────────────────────────

class DodagJoinModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, events: List[DodagJoinEvent], parent_map: Dict):
        super().__init__()
        self._init_bookmarks()
        self._events = events
        self._parent_map = parent_map
        self._headers = ["Node", "Join Time (s)", "RPL Parent", "RPL Depth", "Role"]

    def rowCount(self, parent=QModelIndex()): return len(self._events)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def _depth(self, node_id):
        depth = 0; current = node_id; visited: set = set()
        while current is not None and current not in visited:
            visited.add(current); current = self._parent_map.get(current)
            if current is not None: depth += 1
        return depth

    def _role_str(self, node_id):
        is_parent = any(p == node_id for p in self._parent_map.values() if p is not None)
        if node_id == 1:      return "Root (Server)"
        elif is_parent:       return "Intermediate Parent"
        return "Leaf (Client only)"

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        e = self._events[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            r = self._role_str(e.node_id)
            if r == "Root (Server)":       return QColor("#1a5276")
            elif r == "Intermediate Parent": return QColor("#1e8449")
            return QColor("#e8e8e8")

        if role == Qt.ItemDataRole.ForegroundRole:
            r = self._role_str(e.node_id)
            if r in ("Root (Server)", "Intermediate Parent"): return QColor("white")
            return QColor("#2c2c2c")

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        p = self._parent_map.get(e.node_id)
        raw = [
            e.node_id,
            f"{e.join_time:.3f}",
            str(p) if p is not None else "-",
            self._depth(e.node_id),
            self._role_str(e.node_id),
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 1, 3) else v
        return v


# ── Intermediate parent table ─────────────────────────────────────────────────

class IntermediateParentModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, parent_map: Dict, join_events: List[DodagJoinEvent]):
        super().__init__()
        self._init_bookmarks()
        from parser import get_intermediate_parents
        children_map   = get_intermediate_parents(parent_map)
        join_time_map  = {e.node_id: e.join_time for e in join_events}
        self._rows = []
        for parent_id in sorted(children_map.keys()):
            children = children_map[parent_id]
            depth = 0; current = parent_id; visited: set = set()
            while current is not None and current not in visited:
                visited.add(current); current = parent_map.get(current)
                if current is not None: depth += 1
            self._rows.append({
                "node": parent_id, "children": children,
                "depth": depth, "join_time": join_time_map.get(parent_id),
                "is_root": parent_id == 1,
            })
        self._headers = ["Parent Node", "Role", "RPL Depth",
                         "Children", "Children IDs", "Join Time (s)"]

    def rowCount(self, parent=QModelIndex()): return len(self._rows)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        r = self._rows[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            return QColor("#1a5276") if r["is_root"] else QColor("#1e8449")
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor("white")
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        jt = r["join_time"]
        raw = [
            r["node"],
            "Root (Server)" if r["is_root"] else "Intermediate Parent",
            r["depth"],
            len(r["children"]),
            ", ".join(map(str, r["children"])),
            f"{jt:.3f}" if jt is not None else ("0.000" if r["is_root"] else "-"),
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 2, 3, 5) else v
        return v


# ── RDF event table ───────────────────────────────────────────────────────────

class RdfEventTableModel(BookmarkMixin, QAbstractTableModel):
    _EVENT_COLORS = {
        "TX":           QColor("#fff9c4"),
        "RX":           QColor("#e8f5e9"),
        "ARRIVE":       QColor("#e3f2fd"),
        "FWD":          QColor("#fce4ec"),
        "PDR":          QColor("#f3e5f5"),
        "DISSEM":       QColor("#e0f7fa"),
        "AOI_OK":       QColor("#c8e6c9"),
        "AOI_MISS":     QColor("#ffcdd2"),
        "CBF_TIMER":    QColor("#fff3e0"),
        "CBF_SUPPRESS": QColor("#f5f5f5"),
        "TX_ORIGIN":    QColor("#fffde7"),
        "SUPPRESS_MISS":QColor("#fafafa"),
    }

    def __init__(self, events: List[RdfEvent]):
        super().__init__()
        self._init_bookmarks()
        self._events = events
        self._headers = [
            "Time (s)", "Node ID", "Label", "Event",
            "Src HW ID", "Seq", "Hop",
            "PDR recv", "PDR total", "PDR %",
            "AoI (ms)", "TAoI (ms)", "XY", "Details",
        ]

    def rowCount(self, parent=QModelIndex()): return len(self._events)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        ev = self._events[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            return self._EVENT_COLORS.get(ev.event_type, QColor("white"))

        if role == Qt.ItemDataRole.ToolTipRole:
            return ev.extra

        if role not in (Qt.ItemDataRole.DisplayRole, _SORT_ROLE):
            return QVariant()

        raw = [
            f"{ev.time_s:.4f}", ev.node_id, ev.node_label, ev.event_type,
            str(ev.src_hw_id)  if ev.src_hw_id  is not None else "",
            str(ev.seq)        if ev.seq         is not None else "",
            str(ev.hop)        if ev.hop         is not None else "",
            str(ev.pdr_recv)   if ev.pdr_recv    is not None else "",
            str(ev.pdr_total)  if ev.pdr_total   is not None else "",
            f"{ev.pdr_pct:.1f}" if ev.pdr_pct   is not None else "",
            str(ev.aoi_ms)     if ev.aoi_ms      is not None else "",
            str(ev.taoi_ms)    if ev.taoi_ms     is not None else "",
            f"({ev.xy[0]},{ev.xy[1]})" if ev.xy else "",
            ev.extra,
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 1, 4, 5, 6, 7, 8, 9, 10, 11) else v
        return v

    def event_at(self, row): return self._events[row]


# ── PDR summary table ─────────────────────────────────────────────────────────

class PdrTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, rows: List[Dict]):
        super().__init__()
        self._init_bookmarks()
        self._rows = rows
        self._headers = ["Node ID", "Label", "Src HW ID", "Received", "Total", "PDR %"]

    def rowCount(self, parent=QModelIndex()): return len(self._rows)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        r = self._rows[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            pdr = r.get("pdr_pct")
            if pdr is not None:
                if pdr >= 95:   return QColor("#e8f5e9")
                elif pdr >= 75: return QColor("#fff9c4")
                else:           return QColor("#ffebee")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        raw = [
            r["node_id"], r["label"], str(r["src_hw_id"]),
            str(r["recv"])    if r["recv"]    is not None else "",
            str(r["total"])   if r["total"]   is not None else "",
            f"{r['pdr_pct']:.1f}" if r["pdr_pct"] is not None else "",
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 2, 3, 4, 5) else v
        return v


# ── AoI summary table ─────────────────────────────────────────────────────────

class AoiTableModel(BookmarkMixin, QAbstractTableModel):
    def __init__(self, rows: List[Dict]):
        super().__init__()
        self._init_bookmarks()
        self._rows = rows
        self._headers = [
            "Node ID", "Label", "Src HW ID",
            "Count", "Min AoI (ms)", "Max AoI (ms)", "Avg AoI (ms)", "TAoI (ms)",
        ]

    def rowCount(self, parent=QModelIndex()): return len(self._rows)
    def columnCount(self, parent=QModelIndex()): return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole: return QVariant()
        if orientation == Qt.Orientation.Horizontal: return self._headers[section]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return QVariant()
        row = index.row(); col = index.column()
        r = self._rows[row]

        if role == Qt.ItemDataRole.BackgroundRole:
            if self.is_bookmarked(row): return QColor("#ffe0b2")
            taoi = r.get("taoi_ms"); avg = r.get("avg_aoi")
            if taoi and avg is not None:
                ratio = avg / taoi
                if ratio <= 0.5:   return QColor("#e8f5e9")
                elif ratio <= 0.9: return QColor("#fff9c4")
                else:              return QColor("#ffebee")
            return QVariant()

        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, _SORT_ROLE):
            return QVariant()

        raw = [
            r["node_id"], r["label"], str(r["src_hw_id"]),
            r["count"], str(r["min_aoi"]), str(r["max_aoi"]),
            str(r["avg_aoi"]),
            str(r["taoi_ms"]) if r["taoi_ms"] is not None else "",
        ]
        v = raw[col]
        if role == _SORT_ROLE:
            return _sort_val(v) if col in (0, 2, 3, 4, 5, 6, 7) else v
        return v

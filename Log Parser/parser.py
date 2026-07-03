# parser.py
# Handles both Cooja simulation logs and IoT-LAB hardware logs.

import re
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

# ── Shared dataclasses ────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    time: float
    time_str: str
    node_id: int
    level: str
    module: str
    message: str
    raw_node_label: str = ""

@dataclass
class NodeInfo:
    node_id: int
    label: str = ""
    panid: Optional[str] = None
    channel: Optional[str] = None
    mac: Optional[str] = None
    link_local: Optional[str] = None
    hw_node_id: Optional[int] = None
    hw_idx: Optional[int] = None
    xy: Optional[Tuple[int, int]] = None
    first_time: Optional[float] = None
    last_time: Optional[float] = None
    tx: int = 0
    rx: int = 0
    missed_tx: int = 0

@dataclass
class UdpFlow:
    src_node: int
    dst_addr: str
    seq: int
    send_time: float
    resp_time: Optional[float] = None

    @property
    def rtt(self) -> Optional[float]:
        if self.resp_time is None:
            return None
        return self.resp_time - self.send_time

@dataclass
class RadioEntry:
    time_s: float
    src_node: int
    receivers: List[int]
    length: int
    payload_hex: str
    raw_line: str

@dataclass
class DodagJoinEvent:
    node_id: int
    join_time: float
    parent_id: Optional[int]
    rank: Optional[int]

@dataclass
class TimelineEntry:
    time_us: int
    node_id: int
    event_type: str
    channel: Optional[int] = None
    extra: str = ""

@dataclass
class RdfEvent:
    time_s: float
    node_id: int
    node_label: str
    event_type: str
    src_hw_id: Optional[int] = None
    seq: Optional[int] = None
    hop: Optional[int] = None
    pdr_recv: Optional[int] = None
    pdr_total: Optional[int] = None
    pdr_pct: Optional[float] = None
    aoi_ms: Optional[int] = None
    taoi_ms: Optional[int] = None
    xy: Optional[Tuple[int, int]] = None
    extra: str = ""

# ── Log format detection ──────────────────────────────────────────────────────

# Cooja: "00:01.234\tID:3\t[INFO: ...]" or space-separated
_COOJA_SNIFF  = re.compile(r"^\d{2}:\d{2}\.\d{3}[\t ]+ID:\d+")
# IoT-LAB: "1782311110.429356;m3-32;..."
_IOTLAB_SNIFF = re.compile(r"^\d{7,}\.\d+;m\d+-\d+;")

def detect_log_type(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if _COOJA_SNIFF.match(line):
                return "cooja"
            if _IOTLAB_SNIFF.match(line):
                return "iotlab"
    return "unknown"

# ── Cooja parser ──────────────────────────────────────────────────────────────

# Handles tab-separated AND space-separated Cooja output
_COOJA_LINE_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}\.\d{3})[\t ]+ID:(?P<id>\d+)[\t ]+\[(?P<level>[^:]+):\s*(?P<module>[^\]]+)\]\s*(?P<msg>.*)$"
)

def _parse_cooja_time(t: str) -> float:
    minutes, rest = t.split(":")
    return int(minutes) * 60 + float(rest)

def parse_log_cooja(path: str) -> List[LogEntry]:
    entries: List[LogEntry] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = _COOJA_LINE_RE.match(line)
            if not m:
                continue
            time_str = m.group("time")
            t        = _parse_cooja_time(time_str)
            node_id  = int(m.group("id"))
            entries.append(LogEntry(
                time=t, time_str=time_str,
                node_id=node_id,
                level=m.group("level").strip(),
                module=m.group("module").strip(),
                message=m.group("msg").strip(),
                raw_node_label=f"ID:{node_id}",
            ))
    return entries

# ── IoT-LAB parser ────────────────────────────────────────────────────────────

_IOTLAB_LINE_RE      = re.compile(r"^(?P<ts>\d+\.\d+);(?P<label>m\d+-\d+);(?P<rest>.*)$")
_IOTLAB_STRUCT_RE    = re.compile(r"^\[(?P<level>[^:]+):\s*(?P<module>[^\]]+)\]\s*(?P<msg>.*)$")
_IOTLAB_CLOCKDBG_RE  = re.compile(r"^\[in\s+(?P<func>[^\]]+)\]\s*(?P<msg>.*)$")

def parse_log_iotlab(path: str) -> Tuple[List[LogEntry], Dict[str, int]]:
    label_order: Dict[str, int] = {}
    raw_entries = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = _IOTLAB_LINE_RE.match(line)
            if not m:
                continue
            ts    = float(m.group("ts"))
            label = m.group("label")
            rest  = m.group("rest")

            if label not in label_order:
                label_order[label] = len(label_order) + 1

            sm = _IOTLAB_STRUCT_RE.match(rest)
            if sm:
                level  = sm.group("level").strip()
                module = sm.group("module").strip()
                msg    = sm.group("msg").strip()
            else:
                am = _IOTLAB_CLOCKDBG_RE.match(rest)
                if am:
                    level  = "DEBUG"
                    module = am.group("func").strip()
                    msg    = am.group("msg").strip()
                else:
                    level  = "RAW"
                    module = "Boot"
                    msg    = rest.strip()

            raw_entries.append((ts, label, level, module, msg))

    if not raw_entries:
        return [], label_order

    t0 = raw_entries[0][0]
    entries: List[LogEntry] = []
    for (ts, label, level, module, msg) in raw_entries:
        t_rel    = ts - t0
        node_id  = label_order[label]
        minutes  = int(t_rel) // 60
        seconds  = t_rel - minutes * 60
        time_str = f"{minutes:02d}:{seconds:06.3f}"
        entries.append(LogEntry(
            time=t_rel, time_str=time_str,
            node_id=node_id,
            level=level, module=module, message=msg,
            raw_node_label=label,
        ))
    return entries, label_order

# ── Unified entry point ───────────────────────────────────────────────────────

def parse_log(path: str) -> Tuple[List[LogEntry], str, Dict[str, int]]:
    log_type = detect_log_type(path)
    if log_type == "iotlab":
        entries, label_to_id = parse_log_iotlab(path)
        return entries, "iotlab", label_to_id
    else:
        entries = parse_log_cooja(path)
        return entries, "cooja", {}

# ── Node info builder ─────────────────────────────────────────────────────────

_PANID_RE   = re.compile(r"PANID:\s*(\S+)")
_CHAN_RE    = re.compile(r"Default channel:\s*(\S+)")
_MAC_RE    = re.compile(r"Link-layer address:\s*(\S+)")
_LL_RE     = re.compile(r"Tentative link-local IPv6 address:\s*(\S+)")
_HWID_RE   = re.compile(r"Node ID:\s*(\d+)")
_HWIDX_RE  = re.compile(r"hw_idx=(\d+)")
_XY_INT_RE = re.compile(r"(?:xy|sxy|mxy|sender_xy|src_xy|my_xy)=\((\d+),(\d+)\)")
_XY_M_RE   = re.compile(r"(?:live_xy|fallback_xy)=\((\d+)\s*m,\s*(\d+)\s*m\)")

def build_node_infos(
    entries: List[LogEntry],
    label_to_id: Dict[str, int] = None,
) -> Dict[int, NodeInfo]:
    nodes: Dict[int, NodeInfo] = {}
    id_to_label = {v: k for k, v in (label_to_id or {}).items()}

    for e in entries:
        n = nodes.setdefault(e.node_id, NodeInfo(
            node_id=e.node_id,
            label=id_to_label.get(e.node_id, e.raw_node_label),
        ))
        if n.first_time is None or e.time < n.first_time:
            n.first_time = e.time
        if n.last_time is None or e.time > n.last_time:
            n.last_time = e.time

        msg = e.message
        if e.module == "Main":
            m = _PANID_RE.search(msg);  n.panid      = m.group(1) if m else n.panid
            m = _CHAN_RE.search(msg);   n.channel    = m.group(1) if m else n.channel
            m = _MAC_RE.search(msg);    n.mac        = m.group(1) if m else n.mac
            m = _LL_RE.search(msg);     n.link_local = m.group(1) if m else n.link_local
            m = _HWID_RE.search(msg);   n.hw_node_id = int(m.group(1)) if m else n.hw_node_id

        if e.module == "RDF":
            m = _HWIDX_RE.search(msg)
            if m and n.hw_idx is None:
                n.hw_idx = int(m.group(1))
            if n.xy is None:
                m = _XY_INT_RE.search(msg)
                if m:
                    n.xy = (int(m.group(1)), int(m.group(2)))
                else:
                    m = _XY_M_RE.search(msg)
                    if m:
                        n.xy = (int(m.group(1)), int(m.group(2)))
    return nodes

# ── UDP flow builder ──────────────────────────────────────────────────────────

def build_udp_flows(entries: List[LogEntry]) -> List[UdpFlow]:
    flows: List[UdpFlow] = []
    pending: Dict[Tuple[int, int], UdpFlow] = {}
    send_re = re.compile(r"Sending request (\d+) to (\S+)")
    resp_re = re.compile(r"Received response 'hello (\d+)' from (\S+)")

    for e in entries:
        if e.module != "App":
            continue
        m = send_re.match(e.message)
        if m:
            seq = int(m.group(1)); dst = m.group(2)
            pending[(e.node_id, seq)] = UdpFlow(
                src_node=e.node_id, dst_addr=dst, seq=seq, send_time=e.time)
            continue
        m = resp_re.match(e.message)
        if m:
            seq = int(m.group(1))
            flow = pending.get((e.node_id, seq))
            if flow is not None:
                flow.resp_time = e.time
                flows.append(flow)
                del pending[(e.node_id, seq)]

    flows.extend(pending.values())
    return flows

# ── Radio log parser (optional) ───────────────────────────────────────────────

def parse_radio_log(path: str) -> List[RadioEntry]:
    if not path or not path.strip():
        return []
    entries: List[RadioEntry] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw_line = raw.rstrip("\n")
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                time_ms = float(parts[0]); src = int(parts[1])
            except ValueError:
                continue
            receivers: List[int] = []
            if parts[2] not in ("-", "none", "NONE"):
                for r in parts[2].split(","):
                    r = r.strip()
                    if r:
                        try: receivers.append(int(r))
                        except ValueError: pass
            try:   length = int(parts[3].rstrip(":"))
            except ValueError: length = 0
            payload_hex = "".join(
                t.replace("0x", "").replace("0X", "") for t in parts[4:])
            entries.append(RadioEntry(
                time_s=time_ms / 1000.0, src_node=src,
                receivers=receivers, length=length,
                payload_hex=payload_hex, raw_line=raw_line,
            ))
    return entries

# ── Timeline parser (optional) ────────────────────────────────────────────────

def parse_timeline(path: str) -> List[TimelineEntry]:
    if not path or not path.strip():
        return []
    entries: List[TimelineEntry] = []
    chan_re = re.compile(r"channel[=:\s]+(\d+)", re.IGNORECASE)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n").strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";") if ";" in line else line.split()
            if len(parts) < 3:
                continue
            try:
                time_us = int(float(parts[0])); node_id = int(parts[1])
            except ValueError:
                continue
            event_type = parts[2].strip().upper()
            extra = ";".join(parts[3:]) if len(parts) > 3 else ""
            channel = None
            m = chan_re.search(extra)
            if m: channel = int(m.group(1))
            entries.append(TimelineEntry(
                time_us=time_us, node_id=node_id,
                event_type=event_type, channel=channel, extra=extra,
            ))
    return entries

# ── IoT-LAB RDF event parser ──────────────────────────────────────────────────

_RDF_SRC_RE  = re.compile(r"\bsrc=(\d+)")
_RDF_SEQ_RE  = re.compile(r"\bseq=(\d+)")
_RDF_HOP_RE  = re.compile(r"\bhop=(\d+)")
_RDF_PDR_RE  = re.compile(r"recv=(\d+)/(\d+)\s*=\s*([\d.]+)%")
_RDF_AOI_RE  = re.compile(r"\baoi_ms=(\d+)")
_RDF_TAOI_RE = re.compile(r"\btaoi_ms=(\d+)")

_RDF_EVENT_KEYWORDS = {
    "TX", "RX", "ARRIVE", "FWD", "PDR", "DISSEM",
    "AOI_OK", "AOI_MISS", "CBF_TIMER", "CBF_SUPPRESS",
    "SUPPRESS_MISS", "TX_ORIGIN", "INIT", "POS",
}

def parse_rdf_events(entries: List[LogEntry]) -> List[RdfEvent]:
    events: List[RdfEvent] = []
    for e in entries:
        if e.module != "RDF":
            continue
        msg = e.message.strip()
        first_token = msg.split()[0].upper() if msg else ""
        event_type  = first_token if first_token in _RDF_EVENT_KEYWORDS else "OTHER"

        src_m  = _RDF_SRC_RE.search(msg)
        seq_m  = _RDF_SEQ_RE.search(msg)
        hop_m  = _RDF_HOP_RE.search(msg)
        pdr_m  = _RDF_PDR_RE.search(msg)
        aoi_m  = _RDF_AOI_RE.search(msg)
        taoi_m = _RDF_TAOI_RE.search(msg)
        xy_m   = _XY_INT_RE.search(msg)

        events.append(RdfEvent(
            time_s=e.time, node_id=e.node_id, node_label=e.raw_node_label,
            event_type=event_type,
            src_hw_id=int(src_m.group(1))   if src_m  else None,
            seq=int(seq_m.group(1))          if seq_m  else None,
            hop=int(hop_m.group(1))          if hop_m  else None,
            pdr_recv=int(pdr_m.group(1))     if pdr_m  else None,
            pdr_total=int(pdr_m.group(2))    if pdr_m  else None,
            pdr_pct=float(pdr_m.group(3))    if pdr_m  else None,
            aoi_ms=int(aoi_m.group(1))       if aoi_m  else None,
            taoi_ms=int(taoi_m.group(1))     if taoi_m else None,
            xy=(int(xy_m.group(1)), int(xy_m.group(2))) if xy_m else None,
            extra=msg,
        ))
    return events

def build_iotlab_radio_graph_from_nodes(
    rdf_events: List[RdfEvent],
    nodes: Dict[int, NodeInfo],
) -> Dict[Tuple[int, int], int]:
    hw_to_node: Dict[int, int] = {
        info.hw_node_id: nid
        for nid, info in nodes.items()
        if info.hw_node_id is not None
    }
    directed: Dict[Tuple[int, int], int] = {}
    for ev in rdf_events:
        if ev.event_type != "RX" or ev.src_hw_id is None:
            continue
        sender = hw_to_node.get(ev.src_hw_id)
        if sender is None or sender == ev.node_id:
            continue
        directed[(sender, ev.node_id)] = directed.get((sender, ev.node_id), 0) + 1

    undirected: Dict[Tuple[int, int], int] = {}
    seen: set = set()
    for (a, b), w1 in directed.items():
        pair = tuple(sorted((a, b)))
        if pair in seen: continue
        undirected[pair] = w1 + directed.get((b, a), 0)
        seen.add(pair)
    return undirected

def build_iotlab_pdr_summary(
    rdf_events: List[RdfEvent],
    nodes: Dict[int, NodeInfo],
) -> List[Dict]:
    latest: Dict[Tuple[int, int], RdfEvent] = {}
    for ev in rdf_events:
        if ev.event_type == "PDR" and ev.src_hw_id is not None:
            latest[(ev.node_id, ev.src_hw_id)] = ev
    rows = []
    for (node_id, src_hw_id), ev in sorted(latest.items()):
        info = nodes.get(node_id)
        rows.append({
            "node_id": node_id,
            "label":   info.label if info else str(node_id),
            "src_hw_id": src_hw_id,
            "recv":    ev.pdr_recv,
            "total":   ev.pdr_total,
            "pdr_pct": ev.pdr_pct,
        })
    return rows

def build_iotlab_aoi_summary(
    rdf_events: List[RdfEvent],
    nodes: Dict[int, NodeInfo],
) -> List[Dict]:
    from collections import defaultdict
    buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    taoi_map: Dict[Tuple[int, int], int] = {}
    for ev in rdf_events:
        if ev.event_type == "AOI_OK" and ev.src_hw_id is not None and ev.aoi_ms is not None:
            key = (ev.node_id, ev.src_hw_id)
            buckets[key].append(ev.aoi_ms)
            if ev.taoi_ms is not None:
                taoi_map[key] = ev.taoi_ms
    rows = []
    for (node_id, src_hw_id), vals in sorted(buckets.items()):
        info = nodes.get(node_id)
        rows.append({
            "node_id":  node_id,
            "label":    info.label if info else str(node_id),
            "src_hw_id": src_hw_id,
            "count":    len(vals),
            "min_aoi":  min(vals),
            "max_aoi":  max(vals),
            "avg_aoi":  round(sum(vals) / len(vals), 1),
            "taoi_ms":  taoi_map.get((node_id, src_hw_id)),
        })
    return rows

# ── RPL / DODAG ───────────────────────────────────────────────────────────────

_PARENT_RE  = re.compile(r"(?:preferred parent|parent)[:\s]+(?:node\s*)?(\S+)", re.IGNORECASE)
_RANK_RE    = re.compile(r"rank[:\s=]+(\d+)", re.IGNORECASE)
_SENDING_RE = re.compile(r"Sending request (\d+) to", re.IGNORECASE)

def build_dodag_events(entries: List[LogEntry]) -> List[DodagJoinEvent]:
    root_id = 1
    first_send: Dict[int, float] = {}
    parent_from_log: Dict[int, int] = {}
    rank_from_log: Dict[int, int] = {}
    for e in entries:
        if e.node_id == root_id: continue
        if e.module == "App":
            m = _SENDING_RE.match(e.message)
            if m and e.node_id not in first_send:
                first_send[e.node_id] = e.time
        mp = _PARENT_RE.search(e.message)
        if mp:
            addr = mp.group(1).strip(".,;")
            try:
                last = int(addr.split(":")[-1], 16)
                if 1 <= last <= 100: parent_from_log[e.node_id] = last
            except Exception: pass
        mr = _RANK_RE.search(e.message)
        if mr: rank_from_log[e.node_id] = int(mr.group(1))

    return [
        DodagJoinEvent(
            node_id=nid, join_time=jt,
            parent_id=parent_from_log.get(nid),
            rank=rank_from_log.get(nid),
        )
        for nid, jt in sorted(first_send.items(), key=lambda x: x[1])
    ]

STATIC_PARENT_MAP: Dict[int, Optional[int]] = {
    1: None,
    2: 1, 3: 1, 4: 1, 5: 1,
    6: 4, 7: 5, 8: 5, 9: 7,
    10: 8, 11: 8, 12: 9,
    13: 10, 14: 10, 15: 13, 16: 13,
}

def get_parent_map(entries: List[LogEntry]) -> Dict[int, Optional[int]]:
    parent_from_log: Dict[int, Optional[int]] = {}
    for e in entries:
        mp = _PARENT_RE.search(e.message)
        if mp:
            addr = mp.group(1).strip(".,;")
            try:
                last = int(addr.split(":")[-1], 16)
                if 1 <= last <= 100: parent_from_log[e.node_id] = last
            except Exception: pass
    all_nodes = set(e.node_id for e in entries) - {1}
    if len(parent_from_log) >= len(all_nodes) * 0.6:
        result = {1: None}; result.update(parent_from_log); return result
    return STATIC_PARENT_MAP

def get_path_to_root(node_id: int, parent_map: Dict[int, Optional[int]]) -> List[int]:
    path: List[int] = []; visited: set = set(); current = node_id
    while current is not None and current not in visited:
        path.append(current); visited.add(current)
        current = parent_map.get(current)
    return path

def get_intermediate_parents(parent_map: Dict[int, Optional[int]]) -> Dict[int, List[int]]:
    children_map: Dict[int, List[int]] = {}
    for child, parent in parent_map.items():
        if parent is not None:
            children_map.setdefault(parent, []).append(child)
    return {p: sorted(c) for p, c in children_map.items()}

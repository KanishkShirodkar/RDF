"""
rdf_analyzer.py  —  RDF Flood v4 Log Analyzer for Google Colab
================================================================
Upload your loglistener.txt (Cooja) or IoT-LAB log file and run
each cell to get full per-node statistics.

Supports:
  • Cooja format:   "MM:SS.mmm  ID:N  [INFO: RDF ] ..."
  • IoT-LAB format: "unix_ts;m3-N;[INFO: RDF ] ..."

AoI origin time:
  The TX log line's loglistener system timestamp IS the origin time.
  TX_ORIGIN was removed from rdf-flood-v4.c as redundant.
  AoI(src, seq, node) = t_RX_log - t_TX_log   (both from loglistener)

Metrics computed per node:
  1. CBF suppress count          (CBF_SUPPRESS lines)
  2. Pending update count        (PENDING_UPDATE lines)
  3. Stale drop count            (PENDING_DROP_STALE lines)
  4. Dissemination rate P_D      (as receiver + as sender)
  5. Excess probability P_EX     (overall + 1-hop only)
  6. Successful RX               (packets received from others)
  7. Successful TX               (my packets received by others)

Logic:
  P_D  (receiver) = unique (src,seq) received / total sent by src
  P_D  (sender)   = avg over my seqs of (receivers / total_other_nodes)
  P_EX (overall)  = RX events with AoI > TAOI_MS / total RX events
  P_EX (1-hop)    = same but only for hop <= 1 RX events
  AoI             = t_RX - t_TX  (ms)  — both are loglistener timestamps
  T_AOI           = 1125 ms  (1.125 s)
  T_AM            = 45 s  — warm-up window
"""

# ═══════════════════════════════════════════════════════════════════════════
# CELL 1 — Install / imports
# ═══════════════════════════════════════════════════════════════════════════
import re
import os
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

# For Google Colab file upload
try:
    from google.colab import files as colab_files
    IN_COLAB = True
except ImportError:
    IN_COLAB = False

# ═══════════════════════════════════════════════════════════════════════════
# CELL 2 — Constants
# ═══════════════════════════════════════════════════════════════════════════

TAOI_MS  = 1125.0    # T_AOI threshold in ms = 1.125 s
TAOI_S   = 1.125     # T_AOI in seconds
TAM_S    = 45.0      # T_AM window size in s = 45 s (user can override)

# ═══════════════════════════════════════════════════════════════════════════
# CELL 3 — Log format detection & parsing
# ═══════════════════════════════════════════════════════════════════════════

# Cooja:   "00:01.234\tID:3\t[INFO: RDF ] message"
_COOJA_RE  = re.compile(
    r'^(\d{2}):(\d{2}\.\d+)[\t ]+ID:(\d+)[\t ]+\[INFO:\s+RDF\s*\]\s+(.+)$'
)
# IoT-LAB: "1782311110.429;m3-32;[INFO: RDF ] message"
_IOTLAB_RE = re.compile(
    r'^(\d{7,}\.\d+);m(\d+);(?:s?\[INFO:\s+RDF\s*\]\s+)?(.+)$'
)
_IOTLAB_SNIFF = re.compile(r'^\d{7,}\.\d+;m\d+-\d+;')

# Field extractors
_SRC_RE      = re.compile(r'\bsrc=(\d+)')
_SEQ_RE      = re.compile(r'\bseq=(\d+)')
_HOP_RE      = re.compile(r'\bhop=(\d+)')
_OLD_SEQ_RE  = re.compile(r'\bold_seq=(\d+)')
_NEW_SEQ_RE  = re.compile(r'\bnew_seq=(\d+)')
_BUF_SEQ_RE  = re.compile(r'\bbuffered_seq=(\d+)')
_STALE_RE    = re.compile(r'\bstale_seq=(\d+)')
_TIME_MS_RE  = re.compile(r'\btime_ms=(\d+)')


def detect_format(text: str) -> str:
    """Return 'cooja' or 'iotlab'."""
    for line in text.splitlines()[:50]:
        line = line.strip()
        if not line:
            continue
        if _COOJA_RE.match(line):
            return 'cooja'
        if re.match(r'^\d{7,}\.\d+;m\d+-\d+;', line):
            return 'iotlab'
    return 'cooja'


def parse_time_cooja(mm: str, ss: str) -> float:
    return int(mm) * 60 + float(ss)


def parse_log(filepath: str) -> dict:
    """
    Parse a Cooja or IoT-LAB RDF log file.

    Returns a dict with all raw events and computed per-node statistics.
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

    fmt = detect_format(text)
    lines = text.replace('\r\n', '\n').replace('\r', '\n').splitlines()

    # ── Raw event storage ─────────────────────────────────────────────────
    # tx_origin[(src, seq)]        = origin_time_s
    tx_origin: Dict[Tuple, float] = {}

    # rx_events[(node, src, seq)]  = list of (time_s, hop)
    rx_events: Dict[Tuple, List]  = defaultdict(list)

    # cbf_suppress[node][src]      = count (CBF wait: arriving_hop > pending_hop → cancelled)
    cbf_suppress: Dict[int, Dict] = defaultdict(lambda: defaultdict(int))

    # rdf_suppress[node][src]      = count (RDF decay wait: same logic, different period)
    rdf_suppress: Dict[int, Dict] = defaultdict(lambda: defaultdict(int))

    # cbf_hop_ignore[node][src]    = count (CBF wait: arriving_hop <= pending_hop → kept)
    cbf_hop_ignore: Dict[int, Dict] = defaultdict(lambda: defaultdict(int))

    # rdf_hop_ignore[node][src]    = count (RDF decay wait: same logic, different period)
    rdf_hop_ignore: Dict[int, Dict] = defaultdict(lambda: defaultdict(int))

    # pending_update[node][src]    = count (fresher seq replaced old seq)
    pending_update: Dict[int, Dict] = defaultdict(lambda: defaultdict(int))

    # stale_drop[node][src]        = count
    stale_drop: Dict[int, Dict]   = defaultdict(lambda: defaultdict(int))

    # fwd_count[node][src]         = count
    fwd_count: Dict[int, Dict]    = defaultdict(lambda: defaultdict(int))

    # tx_seqs[node]                = set of seqs sent
    tx_seqs: Dict[int, set]       = defaultdict(set)

    nodes: set = set()
    t0: Optional[float] = None   # IoT-LAB timestamp normalisation
    log_duration: float = 0.0

    # ── Parse lines ───────────────────────────────────────────────────────
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue

        t: Optional[float] = None
        node: Optional[int] = None
        msg: Optional[str]  = None

        if fmt == 'cooja':
            m = _COOJA_RE.match(raw)
            if not m:
                continue
            t    = parse_time_cooja(m.group(1), m.group(2))
            node = int(m.group(3))
            msg  = m.group(4).strip()

        else:  # iotlab
            # Format: "unix_ts;m3-N;[INFO: RDF ] msg"
            # Also handle split lines merged
            parts = raw.split(';', 2)
            if len(parts) < 3:
                continue
            try:
                unix_ts = float(parts[0])
            except ValueError:
                continue
            # Extract node number from "m3-N" or "mN"
            node_m = re.match(r'm3?-?(\d+)', parts[1])
            if not node_m:
                continue
            node = int(node_m.group(1))
            rest = parts[2].strip()
            # Must contain RDF log line
            rdf_m = re.match(r's?\[INFO:\s+RDF\s*\]\s+(.*)', rest)
            if not rdf_m:
                continue
            msg = rdf_m.group(1).strip()
            if t0 is None:
                t0 = unix_ts
            t = unix_ts - t0

        if t is None or node is None or msg is None:
            continue

        nodes.add(node)
        if t > log_duration:
            log_duration = t

        word = msg.split()[0]

        # ── TX ────────────────────────────────────────────────────────────
        # "TX seq=S xy=(...)"
        # The loglistener system timestamp on this line IS the origin time.
        # TX_ORIGIN was removed from rdf-flood-v4.c as redundant.
        # node = source (the node that sent this packet)
        if word == 'TX':
            seq_m = _SEQ_RE.search(msg)
            if seq_m:
                seq = int(seq_m.group(1))
                tx_seqs[node].add(seq)
                # Loglistener timestamp = origin time for AoI
                tx_origin[(node, seq)] = t

        # ── RX ────────────────────────────────────────────────────────────
        # "RX src=N seq=S hop=H src_xy=(...) sender_xy=(...) my_xy=(...)"
        # Logged ONLY on first receive (is_new=1 path in C code)
        # node = receiver, src = original source, hop = relay count
        elif word == 'RX':
            src_m = _SRC_RE.search(msg)
            seq_m = _SEQ_RE.search(msg)
            hop_m = _HOP_RE.search(msg)
            if src_m and seq_m:
                src = int(src_m.group(1))
                seq = int(seq_m.group(1))
                hop = int(hop_m.group(1)) if hop_m else -1
                rx_events[(node, src, seq)].append((t, hop))
                nodes.add(src)

        # ── CBF_SUPPRESS ──────────────────────────────────────────────────
        # "CBF_SUPPRESS src=N seq=S arriving_hop=A pending_hop=P"
        # CASE 1 during CBF wait: arriving_hop > pending_hop → cancelled
        elif word == 'CBF_SUPPRESS':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                cbf_suppress[node][src] += 1

        # ── RDF_SUPPRESS ──────────────────────────────────────────────────
        # "RDF_SUPPRESS src=N seq=S arriving_hop=A pending_hop=P period=P"
        # CASE 1 during RDF rate-decay wait: same logic, different period
        elif word == 'RDF_SUPPRESS':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                rdf_suppress[node][src] += 1

        # ── CBF_HOP_IGNORE ────────────────────────────────────────────────
        # "CBF_HOP_IGNORE src=N seq=S arriving_hop=A pending_hop=P"
        # CASE 2 during CBF wait: arriving_hop <= pending_hop → keep timer
        elif word == 'CBF_HOP_IGNORE':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                cbf_hop_ignore[node][src] += 1

        # ── RDF_HOP_IGNORE ────────────────────────────────────────────────
        # "RDF_HOP_IGNORE src=N seq=S arriving_hop=A pending_hop=P period=P"
        # CASE 2 during RDF rate-decay wait: same logic, different period
        elif word == 'RDF_HOP_IGNORE':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                rdf_hop_ignore[node][src] += 1

        # ── PENDING_UPDATE ────────────────────────────────────────────────
        # "PENDING_UPDATE src=N old_seq=O new_seq=W old_hop=OH new_hop=NH timer_kept=1"
        # ── PENDING_UPDATE ────────────────────────────────────────────────
        # "PENDING_UPDATE src=N old_seq=O new_seq=W old_hop=OH new_hop=NH timer_kept=1"
        # A fresher seq W arrived while timer was running for old_seq O
        # Buffer updated to W, timer kept running (not reset)
        # old_seq O will NEVER be forwarded by this node
        elif word == 'PENDING_UPDATE':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                pending_update[node][src] += 1

        # ── PENDING_DROP_STALE ────────────────────────────────────────────
        # "PENDING_DROP_STALE src=N buffered_seq=B stale_seq=S"
        # Arrived seq S < buffered seq B → incoming packet is older
        # Dropped immediately, existing pending forward kept intact
        elif word == 'PENDING_DROP_STALE':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                stale_drop[node][src] += 1

        # ── FWD ───────────────────────────────────────────────────────────
        # "FWD src=N seq=S hop=H sender_xy=(...)"
        # This node forwarded the packet (CBF timer fired)
        elif word == 'FWD':
            src_m = _SRC_RE.search(msg)
            if src_m:
                src = int(src_m.group(1))
                fwd_count[node][src] += 1

    # ── Build per-node statistics ─────────────────────────────────────────
    sorted_nodes = sorted(nodes)
    total_nodes  = len(sorted_nodes)

    stats = {}

    for node in sorted_nodes:
        other_nodes = [n for n in sorted_nodes if n != node]

        # ── 1. CBF suppress (CBF wait period) ────────────────────────────
        cbf_total    = sum(cbf_suppress[node].values())
        cbf_by_src   = dict(cbf_suppress[node])

        # ── 1b. RDF suppress (RDF rate-decay wait period) ─────────────────
        rdf_sup_total  = sum(rdf_suppress[node].values())
        rdf_sup_by_src = dict(rdf_suppress[node])

        # ── 1c. CBF hop ignore (CBF wait: equal/better copy kept) ─────────
        cbf_ign_total  = sum(cbf_hop_ignore[node].values())
        cbf_ign_by_src = dict(cbf_hop_ignore[node])

        # ── 1d. RDF hop ignore (RDF decay wait: equal/better copy kept) ───
        rdf_ign_total  = sum(rdf_hop_ignore[node].values())
        rdf_ign_by_src = dict(rdf_hop_ignore[node])

        # ── 2. Pending updates (total + per source) ───────────────────────
        upd_total  = sum(pending_update[node].values())
        upd_by_src = dict(pending_update[node])

        # ── 3. Stale drops (total + per source) ───────────────────────────
        stale_total  = sum(stale_drop[node].values())
        stale_by_src = dict(stale_drop[node])

        # ── 4a. Successful RX — packets received FROM others ──────────────
        # Count unique (src, seq) pairs received at this node
        # Group by source for per-source breakdown
        rx_by_src: Dict[int, set] = defaultdict(set)
        for (recv, src, seq), events in rx_events.items():
            if recv == node:
                rx_by_src[src].add(seq)

        rx_total = sum(len(seqs) for seqs in rx_by_src.values())

        # ── 4b. Successful TX — my packets received by others ─────────────
        # For each seq I sent, count how many other nodes received it
        my_seqs = tx_seqs.get(node, set())
        tx_reach: Dict[int, int] = {}   # seq -> receiver count
        for seq in my_seqs:
            receivers = set()
            for other in other_nodes:
                if (other, node, seq) in rx_events:
                    receivers.add(other)
            tx_reach[seq] = len(receivers)

        tx_total_sent     = len(my_seqs)
        tx_total_received = sum(tx_reach.values())   # sum of all receiver counts
        # Unique seqs that reached at least 1 other node
        tx_seqs_delivered = sum(1 for c in tx_reach.values() if c > 0)

        # ── 5. Dissemination rate P_D as RECEIVER ─────────────────────────
        # Paper eq.(1): P_D = (1/N) * Σ_i [ N_D,i / (N-1) ]
        # N_D,i = number of DISTINCT sources from which node i received
        #         AT LEAST ONE packet within the TAM window (post warm-up).
        # Binary per source: 1.0 if ≥1 post-TAM packet received, else 0.0.
        #
        # t_start = time when the LAST node first transmitted
        #           (= all nodes are active and warm-up is over)
        first_tx_times: Dict[int, float] = {}
        for (s, sq), t_tx in tx_origin.items():
            if s not in first_tx_times or t_tx < first_tx_times[s]:
                first_tx_times[s] = t_tx
        t_start = max(first_tx_times.values()) if first_tx_times else 0.0

        pd_as_receiver: Dict[int, float] = {}
        pd_as_receiver_1hop: Dict[int, float] = {}   # P_D counting only hop≤1 receptions
        for src in other_nodes:
            # seqs sent by src after warm-up
            sent_post_tam = {sq for sq in tx_seqs.get(src, set())
                             if tx_origin.get((src, sq), 0) >= t_start}
            if not sent_post_tam:
                sent_post_tam = tx_seqs.get(src, set())
            if not sent_post_tam:
                continue
            received_post_tam = rx_by_src.get(src, set()) & sent_post_tam
            # Paper: binary — did we receive ≥1 packet from this source?
            pd_as_receiver[src] = 1.0 if len(received_post_tam) > 0 else 0.0

            # P_D(hop≤1): did we receive ≥1 packet at hop=0 or hop=1?
            heard_1hop = False
            for sq in sent_post_tam:
                events = rx_events.get((node, src, sq), [])
                if any(hop <= 1 for (_, hop) in events):
                    heard_1hop = True
                    break
            pd_as_receiver_1hop[src] = 1.0 if heard_1hop else 0.0

        # Paper P_D (receiver view) = N_D,i / (N-1)
        N_D_i      = sum(1 for v in pd_as_receiver.values() if v > 0)
        N_D_i_1hop = sum(1 for v in pd_as_receiver_1hop.values() if v > 0)
        pd_receiver_avg      = N_D_i      / max(len(other_nodes), 1)
        pd_receiver_avg_1hop = N_D_i_1hop / max(len(other_nodes), 1)

        # ── 6. Dissemination rate P_D as SENDER ───────────────────────────
        # Complementary sender view: for each of my post-TAM seqs,
        # what fraction of other nodes received it?
        # Average across all post-TAM seqs → per-packet delivery fraction.
        pd_per_seq: Dict[int, float] = {}
        for seq in my_seqs:
            n_recv = tx_reach.get(seq, 0)
            pd_per_seq[seq] = n_recv / max(total_nodes - 1, 1)

        # Post-TAM seqs only (exclude warm-up using t_start)
        post_tam_seqs = [
            seq for seq in my_seqs
            if tx_origin.get((node, seq), 0) >= t_start
        ]
        if post_tam_seqs:
            pd_sender_avg = sum(pd_per_seq[s] for s in post_tam_seqs) / len(post_tam_seqs)
        elif my_seqs:
            pd_sender_avg = sum(pd_per_seq.values()) / len(pd_per_seq)
        else:
            pd_sender_avg = 0.0

        # ── 5. P_EX — Excess probability as RECEIVER ──────────────────────
        # AoI(src, seq, node) = t_RX_log - t_TX_log  (ms)
        # t_TX_log = loglistener timestamp on the TX line (origin time)
        # P_EX = count(AoI > TAOI_MS) / total_RX_events
        #
        # We use ALL packets (no TAM exclusion) because AoI violations
        # can happen at any time, not just during warm-up
        #
        # For each (src, seq) received at this node:
        #   - Look up origin time from tx_origin[(src, seq)]  ← set by TX line
        #   - Compute AoI for each receive event
        pex_total   = 0    # total RX events with known origin
        pex_excess  = 0    # RX events with AoI > TAOI_MS
        pex_1hop_total  = 0
        pex_1hop_excess = 0

        aoi_list: List[float] = []   # all AoI values in ms

        for (recv, src, seq), events in rx_events.items():
            if recv != node:
                continue
            origin_t = tx_origin.get((src, seq))
            if origin_t is None:
                continue
            for (rx_t, hop) in events:
                aoi_ms = (rx_t - origin_t) * 1000.0
                if aoi_ms < 0:
                    continue   # clock anomaly, skip
                aoi_list.append(aoi_ms)
                pex_total += 1
                if aoi_ms > TAOI_MS:
                    pex_excess += 1

                # 1-hop P_EX: only direct neighbours (hop=0) or
                # one-relay packets (hop=1)
                # These should ALWAYS be within TAOI — violations here
                # indicate a real problem (not just multi-hop delay)
                if hop <= 1:
                    pex_1hop_total += 1
                    if aoi_ms > TAOI_MS:
                        pex_1hop_excess += 1

        pex_overall = pex_excess / pex_total if pex_total > 0 else 0.0
        pex_1hop    = pex_1hop_excess / pex_1hop_total if pex_1hop_total > 0 else 0.0

        aoi_min = min(aoi_list) if aoi_list else 0.0
        aoi_max = max(aoi_list) if aoi_list else 0.0
        aoi_avg = sum(aoi_list) / len(aoi_list) if aoi_list else 0.0

        # ── 8. Forward count ──────────────────────────────────────────────
        fwd_total = sum(fwd_count[node].values())

        stats[node] = {
            # Identity
            'node':              node,
            'fmt':               fmt,

            # ── Counts ────────────────────────────────────────────────────
            # CBF wait period (dist_wait + jitter, ~5-1000ms)
            'cbf_suppress_total':    cbf_total,
            'cbf_suppress_by_src':   cbf_by_src,
            'cbf_hop_ignore_total':  cbf_ign_total,
            'cbf_hop_ignore_by_src': cbf_ign_by_src,

            # RDF rate-decay wait period (next_allowed_time, can be seconds)
            # Same 4-case logic as CBF but during the longer decay window
            'rdf_suppress_total':    rdf_sup_total,
            'rdf_suppress_by_src':   rdf_sup_by_src,
            'rdf_hop_ignore_total':  rdf_ign_total,
            'rdf_hop_ignore_by_src': rdf_ign_by_src,

            'pending_update_total':  upd_total,
            'pending_update_by_src': upd_by_src,

            'stale_drop_total':     stale_total,
            'stale_drop_by_src':    stale_by_src,

            # ── Successful RX (packets received FROM others) ──────────────
            # = unique (src, seq) pairs where RX log line exists at this node
            'rx_total':             rx_total,
            'rx_by_src':            {s: len(seqs) for s, seqs in rx_by_src.items()},

            # ── Successful TX (my packets received by others) ─────────────
            # tx_total_sent     = how many unique seqs I sent
            # tx_seqs_delivered = seqs that reached at least 1 other node
            # tx_total_received = sum of all (seq × receivers) — total delivery events
            'tx_seqs_sent':         tx_total_sent,
            'tx_seqs_delivered':    tx_seqs_delivered,
            'tx_total_received':    tx_total_received,
            'tx_reach_per_seq':     tx_reach,   # seq -> receiver count

            # ── P_D ───────────────────────────────────────────────────────
            # as_receiver: fraction of sources heard ≥1 time (paper binary)
            # as_receiver_1hop: same but only counting hop≤1 receptions
            # as_sender:   fraction of other nodes that received my packets
            'pd_as_receiver_avg':        pd_receiver_avg,
            'pd_as_receiver_avg_1hop':   pd_receiver_avg_1hop,
            'pd_as_receiver_by_src':     pd_as_receiver,
            'pd_as_receiver_1hop_by_src':pd_as_receiver_1hop,
            'pd_as_sender_avg':          pd_sender_avg,
            'pd_per_seq':                pd_per_seq,

            # ── P_EX ──────────────────────────────────────────────────────
            # overall: all hops
            # 1hop:    only hop <= 1 (direct neighbours + 1-relay)
            'pex_overall':          pex_overall,
            'pex_1hop':             pex_1hop,
            'pex_total_rx':         pex_total,
            'pex_excess_count':     pex_excess,
            'pex_1hop_total':       pex_1hop_total,
            'pex_1hop_excess':      pex_1hop_excess,

            # ── AoI ───────────────────────────────────────────────────────
            'aoi_min_ms':           aoi_min,
            'aoi_max_ms':           aoi_max,
            'aoi_avg_ms':           aoi_avg,

            # ── Forwarding ────────────────────────────────────────────────
            'fwd_total':            fwd_total,
            'fwd_by_src':           dict(fwd_count[node]),
        }

    return {
        'nodes':        sorted_nodes,
        'total_nodes':  total_nodes,
        'fmt':          fmt,
        'log_duration': log_duration,
        'stats':        stats,
        'tx_origin':    tx_origin,
        'rx_events':    rx_events,
        'tx_seqs':      tx_seqs,
        'TAOI_MS':      TAOI_MS,
        'TAM_S':        TAM_S,
    }



# ═══════════════════════════════════════════════════════════════════════════
# CELL 3b — Windowed P_D computation
# ═══════════════════════════════════════════════════════════════════════════

def compute_pd_windows(result: dict, tam_s: float = TAM_S) -> dict:
    """
    Compute P_D per TAM window — paper-correct formula (eq.1, Fuger & Timm-Giel 2023).
    Also computes P_D(hop=0) — Direct Dissemination Rate (new KPI).

    PAPER P_D:
    P_D = (1/N) * Σ_i [ N_D,i / (N-1) ]
    Binary per (receiver, source): 1 if ≥1 packet received in window, else 0.

    P_D(hop=0) — Direct Dissemination Rate:
    ─────────────────────────────────────────
    K_i          = direct neighbourhood of node i (sources ever heard at hop=0)
    W(i,src)     = hop0_RX(i,src) / hop0_RX(i,all)   ← link weight
    heard(i,s,w) = 1 if ≥1 hop=0 RX from src in window w, else 0
    P_D0(i,w)    = Σ_{src∈K_i} [ W(i,src) × heard(i,src,w) ]
    P_D0(w)      = (1/N) × Σ_i P_D0(i,w)
    P_D0         = mean over all windows
    """
    nodes       = result['nodes']
    total_nodes = result['total_nodes']
    tx_origin   = result['tx_origin']
    rx_events   = result['rx_events']
    log_dur     = result['log_duration']

    # ── Step 1: t_start ──────────────────────────────────────────────────
    first_tx: Dict[int, float] = {}
    for (src, seq), t in tx_origin.items():
        if src not in first_tx or t < first_tx[src]:
            first_tx[src] = t
    if not first_tx:
        print("⚠️  No TX events found — cannot compute windowed P_D")
        return {}
    t_start     = max(first_tx.values())
    warmup_node = max(first_tx, key=lambda n: first_tx[n])

    # ── Step 2: pre-build rx_info and hop=0 counts ───────────────────────
    # rx_info[(src,seq)][recv] = min hop seen
    rx_info: Dict[tuple, Dict[int, int]] = defaultdict(dict)
    # hop0_counts[recv][src] = total hop=0 RX events (over full log)
    hop0_counts: Dict[int, Dict[int, int]] = {n: defaultdict(int) for n in nodes}

    for (recv, src, seq), events in rx_events.items():
        for (t_rx, hop) in events:
            if recv not in rx_info[(src, seq)]:
                rx_info[(src, seq)][recv] = hop
            else:
                rx_info[(src, seq)][recv] = min(rx_info[(src, seq)][recv], hop)
            if hop == 0:
                hop0_counts[recv][src] += 1

    # ── Step 3: build K_i (direct neighbourhood) and W(i,src) ───────────
    # K_i = sources ever heard at hop=0 by node i
    K: Dict[int, set] = {n: set() for n in nodes}
    for recv in nodes:
        for src, cnt in hop0_counts[recv].items():
            if cnt > 0:
                K[recv].add(src)

    # W(i,src) = hop0_RX(i,src) / hop0_RX(i,all)
    W: Dict[int, Dict[int, float]] = {}
    for recv in nodes:
        total_hop0 = sum(hop0_counts[recv].values())
        W[recv] = {}
        for src in K[recv]:
            W[recv][src] = (hop0_counts[recv][src] / total_hop0
                            if total_hop0 > 0 else 0.0)

    # ── Step 4: build windows ────────────────────────────────────────────
    n_windows = max(1, int((log_dur - t_start) / tam_s))
    windows   = []
    warmup_pkts = 0

    # hop=0 RX per window: hop0_in_window[(recv,src,w)] = True if heard
    # We need per-window hop=0 reception indicator
    # Build: for each (src,seq), t_tx and per-recv hop=0 flag
    for w in range(n_windows):
        t_from = t_start + w * tam_s
        t_to   = t_start + (w + 1) * tam_s

        window_pkts = []
        for (src, seq), t_tx in tx_origin.items():
            if t_from <= t_tx < t_to:
                window_pkts.append((src, seq, t_tx))
            elif t_tx < t_start and w == 0:
                warmup_pkts += 1

        if not window_pkts:
            continue

        # Paper P_D: binary source sets per receiver
        per_node_sources_all  = {n: set() for n in nodes}
        per_node_sources_1hop = {n: set() for n in nodes}
        # P_D(hop=0): heard(i,src,w) indicator
        heard_hop0 = {n: set() for n in nodes}  # sources heard at hop=0 in window

        for (src, seq, _) in window_pkts:
            receivers = rx_info.get((src, seq), {})
            for recv, min_hop in receivers.items():
                if recv == src:
                    continue
                per_node_sources_all[recv].add(src)
                if min_hop <= 1:
                    per_node_sources_1hop[recv].add(src)
                # Check hop=0 specifically from rx_events
                for (t_rx, hop) in rx_events.get((recv, src, seq), []):
                    if hop == 0:
                        heard_hop0[recv].add(src)
                        break

        # Compute per-node metrics
        expected = total_nodes - 1
        per_node = {}
        pd_all_list  = []
        pd_1hop_list = []
        pd0_list     = []

        for n in nodes:
            n_d_all  = len(per_node_sources_all[n])
            n_d_1hop = len(per_node_sources_1hop[n])
            pd_all   = n_d_all  / expected if expected > 0 else 0.0
            pd_1hop  = n_d_1hop / expected if expected > 0 else 0.0

            # P_D(hop=0): weighted sum over direct neighbours
            pd0 = sum(W[n].get(src, 0.0)
                      for src in K[n]
                      if src in heard_hop0[n])
            # If K_i is empty (no direct neighbours), pd0 = N/A → use 0
            k_size = len(K[n])

            per_node[n] = {
                'rx_all'   : n_d_all,
                'rx_1hop'  : n_d_1hop,
                'pd_all'   : pd_all,
                'pd_1hop'  : pd_1hop,
                'pd0'      : pd0,
                'k_size'   : k_size,
                'heard_hop0': len(heard_hop0[n]),
            }
            pd_all_list.append(pd_all)
            pd_1hop_list.append(pd_1hop)
            pd0_list.append(pd0)

        window_pd_all  = sum(pd_all_list)  / len(pd_all_list)  if pd_all_list  else 0.0
        window_pd_1hop = sum(pd_1hop_list) / len(pd_1hop_list) if pd_1hop_list else 0.0
        window_pd0     = sum(pd0_list)     / len(pd0_list)     if pd0_list     else 0.0

        windows.append({
            'window_idx'  : w,
            't_from'      : t_from,
            't_to'        : t_to,
            'n_packets'   : len(window_pkts),
            'pd_all_avg'  : window_pd_all,
            'pd_1hop_avg' : window_pd_1hop,
            'pd0_avg'     : window_pd0,
            'per_node'    : per_node,
        })

    overall_pd_all  = (sum(w['pd_all_avg']  for w in windows) / len(windows) if windows else 0.0)
    overall_pd_1hop = (sum(w['pd_1hop_avg'] for w in windows) / len(windows) if windows else 0.0)
    overall_pd0     = (sum(w['pd0_avg']     for w in windows) / len(windows) if windows else 0.0)

    return {
        't_start'         : t_start,
        'warmup_node'     : warmup_node,
        'tam_s'           : tam_s,
        'n_windows'       : len(windows),
        'warmup_pkts'     : warmup_pkts,
        'windows'         : windows,
        'overall_pd_all'  : overall_pd_all,
        'overall_pd_1hop' : overall_pd_1hop,
        'overall_pd0'     : overall_pd0,
        'first_tx'        : first_tx,
        'K'               : K,
        'W'               : W,
        'hop0_counts'     : hop0_counts,
    }


def print_pd_windows(result: dict, tam_s: float = TAM_S):
    """Print windowed P_D analysis including P_D(hop=0)."""
    wd = compute_pd_windows(result, tam_s)
    if not wd:
        return

    nodes = result['nodes']
    K     = wd.get('K', {})

    print("\n" + "=" * 90)
    print(f"  WINDOWED P_D ANALYSIS  (T_AM window = {tam_s}s)")
    print(f"  t_start = {wd['t_start']:.3f}s  "
          f"(last node to boot: Node {wd['warmup_node']}, "
          f"first TX at {wd['first_tx'][wd['warmup_node']]:.3f}s)")
    print(f"  Warm-up packets excluded: {wd['warmup_pkts']}")
    print(f"  Windows: {wd['n_windows']}  |  "
          f"P_D(all hops): {wd['overall_pd_all']*100:.1f}%  |  "
          f"P_D(hop≤1): {wd['overall_pd_1hop']*100:.1f}%  |  "
          f"P_D(hop=0 direct): {wd['overall_pd0']*100:.1f}%  |  "
          f"T_AOI={TAOI_MS}ms  T_AM={wd['tam_s']}s")
    print("=" * 90)

    # Print direct neighbourhood info
    print("\n  Direct Neighbourhood K_i (sources ever heard at hop=0):")
    for n in nodes:
        neighbours = sorted(K.get(n, set()))
        print(f"    Node {n}: K_{n} = {neighbours}  (|K|={len(neighbours)})")

    for w in wd['windows']:
        print(f"\n  ┌─ Window {w['window_idx']}:  "
              f"t={w['t_from']:.2f}s → {w['t_to']:.2f}s  "
              f"| {w['n_packets']} packets  "
              f"| P_D(all)={w['pd_all_avg']*100:.1f}%  "
              f"| P_D(hop≤1)={w['pd_1hop_avg']*100:.1f}%  "
              f"| P_D(hop=0)={w['pd0_avg']*100:.1f}% ─┐")

        hdr = (f"  {'Node':>6} {'Src_all':>8} {'Src_1hop':>9} {'Src_hop0':>9} "
               f"{'|K_i|':>6} {'P_D(all)':>9} {'P_D(≤1)':>8} {'P_D(0)':>8}  {'Status'}")
        print(hdr)
        print("  " + "─" * 80)

        for node in nodes:
            pn = w['per_node'][node]
            k_size = pn['k_size']
            status = ("✅" if pn['pd_all'] >= 0.99 and pn['pd0'] >= 0.99
                      else "⚠️ " if pn['pd_all'] >= 0.80
                      else "❌")
            print(f"  {node:>6} {pn['rx_all']:>8} {pn['rx_1hop']:>9} "
                  f"{pn['heard_hop0']:>9} {k_size:>6} "
                  f"{pn['pd_all']*100:>8.1f}% {pn['pd_1hop']*100:>7.1f}% "
                  f"{pn['pd0']*100:>7.1f}%  {status}")

    # Summary across all windows
    print(f"\n  ┌─ SUMMARY: Per-node P_D averaged across all {wd['n_windows']} windows ─┐")
    print(f"  {'Node':>6} {'P_D(all)':>10} {'P_D(hop≤1)':>12} {'P_D(hop=0)':>12}  {'Status'}")
    print("  " + "─" * 55)
    for node in nodes:
        avg_all  = (sum(w['per_node'][node]['pd_all']  for w in wd['windows'])
                    / len(wd['windows']) if wd['windows'] else 0)
        avg_1hop = (sum(w['per_node'][node]['pd_1hop'] for w in wd['windows'])
                    / len(wd['windows']) if wd['windows'] else 0)
        avg_pd0  = (sum(w['per_node'][node]['pd0']     for w in wd['windows'])
                    / len(wd['windows']) if wd['windows'] else 0)
        status = ("✅ PASS" if avg_all >= 0.99 and avg_pd0 >= 0.99
                  else "⚠️  WARN" if avg_all >= 0.80
                  else "❌ FAIL")
        print(f"  {node:>6} {avg_all*100:>9.1f}% {avg_1hop*100:>11.1f}% "
              f"{avg_pd0*100:>11.1f}%  {status}")


# ═══════════════════════════════════════════════════════════════════════════
# CELL 4 — Pretty print per-node summary
# ═══════════════════════════════════════════════════════════════════════════

def print_node_summary(result: dict):
    """Print a clean per-node summary table."""
    nodes = result['nodes']
    stats = result['stats']
    fmt   = result['fmt']
    dur   = result['log_duration']

    print("=" * 90)
    print(f"  RDF Flood v4 — Log Analysis")
    print(f"  Format: {'IoT-LAB' if fmt == 'iotlab' else 'Cooja'}  |  "
          f"Nodes: {result['total_nodes']}  |  "
          f"Duration: {dur:.1f}s  |  "
          f"T_AOI: {TAOI_MS}ms (1.125s)  |  T_AM: {TAM_S}s")
    print("=" * 90)

    # ── Table 1: Main counts ───────────────────────────────────────────────
    print("\n┌─ TABLE 1: Per-Node Packet Fate Counts ─────────────────────────────────────────┐")
    hdr = (f"{'Node':>6} {'CBF_Sup':>9} {'CBF_Ign':>9} {'RDF_Sup':>9} {'RDF_Ign':>9} "
           f"{'Pend_Upd':>10} {'Stale':>7} "
           f"{'RX_Tot':>8} {'TX_Snt':>7} {'TX_Del':>7} {'FWD':>6}")
    print(hdr)
    print("─" * 95)
    for node in nodes:
        s = stats[node]
        print(f"  {node:>4} "
              f"{s['cbf_suppress_total']:>9} "
              f"{s['cbf_hop_ignore_total']:>9} "
              f"{s['rdf_suppress_total']:>9} "
              f"{s['rdf_hop_ignore_total']:>9} "
              f"{s['pending_update_total']:>10} "
              f"{s['stale_drop_total']:>7} "
              f"{s['rx_total']:>8} "
              f"{s['tx_seqs_sent']:>7} "
              f"{s['tx_seqs_delivered']:>7} "
              f"{s['fwd_total']:>6}")
    print()
    print("  CBF_Sup  = CBF_SUPPRESS:   same seq, arriving_hop > pending_hop during CBF wait → cancelled")
    print("  CBF_Ign  = CBF_HOP_IGNORE: same seq, arriving_hop ≤ pending_hop during CBF wait → kept")
    print("  RDF_Sup  = RDF_SUPPRESS:   same seq, arriving_hop > pending_hop during RDF decay → cancelled")
    print("  RDF_Ign  = RDF_HOP_IGNORE: same seq, arriving_hop ≤ pending_hop during RDF decay → kept")
    print("  Pend_Upd = PENDING_UPDATE: fresher seq (seq > pending) → buffer updated, timer kept")
    print("  Stale    = PENDING_DROP_STALE: stale seq (seq < pending) → dropped")
    print("  RX_Tot   = unique (src,seq) packets received from all other nodes")
    print("  TX_Snt   = unique seqs this node sent")
    print("  TX_Del   = my seqs that reached ≥1 other node")
    print("  FWD      = times this node forwarded a packet")

    # ── Table 2: P_D ──────────────────────────────────────────────────────
    print("\n┌─ TABLE 2: Dissemination Rate P_D ──────────────────────────────────────────────┐")
    hdr2 = (f"{'Node':>6} {'P_D_all':>10} {'P_D_hop≤1':>11} {'P_D_TX_avg':>12} "
            f"{'TX_reach':>10}  {'Status'}")
    print(hdr2)
    print("─" * 70)
    for node in nodes:
        s = stats[node]
        pd_rx      = s['pd_as_receiver_avg']
        pd_rx_1hop = s['pd_as_receiver_avg_1hop']
        pd_tx      = s['pd_as_sender_avg']
        reach_vals = list(s['tx_reach_per_seq'].values())
        reach_avg  = sum(reach_vals) / len(reach_vals) if reach_vals else 0
        status = "✅ PASS" if pd_rx >= 0.99 and pd_rx_1hop >= 0.99 else \
                 "⚠️  WARN" if pd_rx >= 0.80 else "❌ FAIL"
        print(f"  {node:>4} "
              f"{pd_rx*100:>9.1f}% "
              f"{pd_rx_1hop*100:>10.1f}% "
              f"{pd_tx*100:>11.1f}% "
              f"{reach_avg:>9.1f}  "
              f"{status}")
    print()
    print("  P_D_all    = paper P_D (all hops): N_D,i/(N-1) — sources heard ≥1 time post T_AM")
    print("  P_D_hop≤1  = same but only counting hop=0 or hop=1 receptions (direct neighbours)")
    print("  P_D_TX_avg = sender view: avg fraction of nodes that received each of my packets")
    print("  TX_reach   = avg number of nodes that received each of my packets")

    # ── Table 3: P_EX ─────────────────────────────────────────────────────
    print("\n┌─ TABLE 3: Excess Probability P_EX (AoI > T_AOI=1125ms) ────────────────────────┐")
    hdr3 = (f"{'Node':>6} {'P_EX_all':>10} {'Excess/Total':>14} "
            f"{'P_EX_1hop':>11} {'1hop_exc/tot':>14} "
            f"{'AoI_avg':>9} {'AoI_max':>9}  {'Status'}")
    print(hdr3)
    print("─" * 90)
    for node in nodes:
        s = stats[node]
        pex_all  = s['pex_overall']
        pex_1h   = s['pex_1hop']
        status = "✅ PASS" if pex_all <= 0.01 else \
                 "⚠️  WARN" if pex_all <= 0.05 else "❌ FAIL"
        print(f"  {node:>4} "
              f"{pex_all*100:>9.2f}% "
              f"{s['pex_excess_count']:>6}/{s['pex_total_rx']:<6} "
              f"{pex_1h*100:>10.2f}% "
              f"{s['pex_1hop_excess']:>5}/{s['pex_1hop_total']:<7} "
              f"{s['aoi_avg_ms']:>8.0f}ms "
              f"{s['aoi_max_ms']:>8.0f}ms  "
              f"{status}")
    print()
    print("  P_EX_all  = fraction of all RX events where AoI > T_AOI (1125ms)")
    print("  P_EX_1hop = same but only for hop≤1 (direct/1-relay neighbours) — violations here are more serious")
    print("              1-hop violations are more serious — these nodes are close")

    # ── Table 4: Per-source breakdown ─────────────────────────────────────
    print("\n┌─ TABLE 4: Per-Node × Per-Source Breakdown ─────────────────────────────────────┐")
    for node in nodes:
        s = stats[node]
        print(f"\n  Node {node}:")
        print(f"  {'Src':>5} {'RX':>6} {'P_D':>7} {'CBF_s':>7} {'CBF_i':>7} "
              f"{'RDF_s':>7} {'RDF_i':>7} {'P_upd':>7} {'Stale':>6} {'FWD':>6}")
        print(f"  {'─'*5} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*6} {'─'*6}")
        all_srcs = sorted(set(
            list(s['rx_by_src'].keys()) +
            list(s['cbf_suppress_by_src'].keys()) +
            list(s['cbf_hop_ignore_by_src'].keys()) +
            list(s['rdf_suppress_by_src'].keys()) +
            list(s['rdf_hop_ignore_by_src'].keys()) +
            list(s['pending_update_by_src'].keys()) +
            list(s['stale_drop_by_src'].keys()) +
            list(s['fwd_by_src'].keys())
        ))
        for src in all_srcs:
            if src == node:
                continue
            rx_cnt  = s['rx_by_src'].get(src, 0)
            pd_rx   = s['pd_as_receiver_by_src'].get(src, 0.0)
            cbf_s   = s['cbf_suppress_by_src'].get(src, 0)
            cbf_i   = s['cbf_hop_ignore_by_src'].get(src, 0)
            rdf_s   = s['rdf_suppress_by_src'].get(src, 0)
            rdf_i   = s['rdf_hop_ignore_by_src'].get(src, 0)
            upd_cnt = s['pending_update_by_src'].get(src, 0)
            stl_cnt = s['stale_drop_by_src'].get(src, 0)
            fwd_cnt = s['fwd_by_src'].get(src, 0)
            print(f"  {src:>5} {rx_cnt:>6} {pd_rx*100:>6.1f}% "
                  f"{cbf_s:>7} {cbf_i:>7} {rdf_s:>7} {rdf_i:>7} "
                  f"{upd_cnt:>7} {stl_cnt:>6} {fwd_cnt:>6}")


# ═══════════════════════════════════════════════════════════════════════════
# CELL 5 — Export to CSV
# ═══════════════════════════════════════════════════════════════════════════

def export_csv(result: dict, out_path: str = 'rdf_analysis.csv'):
    """Export per-node summary to CSV."""
    import csv
    nodes = result['nodes']
    stats = result['stats']

    rows = []
    for node in nodes:
        s = stats[node]
        rows.append({
            'Node':              node,
            'CBF_Suppress':      s['cbf_suppress_total'],
            'CBF_Hop_Ignore':    s['cbf_hop_ignore_total'],
            'RDF_Suppress':      s['rdf_suppress_total'],
            'RDF_Hop_Ignore':    s['rdf_hop_ignore_total'],
            'Pending_Update':    s['pending_update_total'],
            'Stale_Drop':        s['stale_drop_total'],
            'RX_Total':          s['rx_total'],
            'TX_Seqs_Sent':      s['tx_seqs_sent'],
            'TX_Seqs_Delivered': s['tx_seqs_delivered'],
            'TX_Total_Received': s['tx_total_received'],
            'FWD_Total':         s['fwd_total'],
            'PD_all_%':           round(s['pd_as_receiver_avg']      * 100, 2),
            'PD_hop1_%':          round(s['pd_as_receiver_avg_1hop'] * 100, 2),
            'PD_TX_avg_%':        round(s['pd_as_sender_avg']        * 100, 2),
            'PEX_overall_%':     round(s['pex_overall']        * 100, 4),
            'PEX_1hop_%':        round(s['pex_1hop']           * 100, 4),
            'PEX_excess_count':  s['pex_excess_count'],
            'PEX_total_rx':      s['pex_total_rx'],
            'AoI_min_ms':        round(s['aoi_min_ms'], 1),
            'AoI_avg_ms':        round(s['aoi_avg_ms'], 1),
            'AoI_max_ms':        round(s['aoi_max_ms'], 1),
        })

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Exported {len(rows)} rows to: {out_path}")
    return out_path


# ═══════════════════════════════════════════════════════════════════════════
# CELL 6 — Per-source detail CSV
# ═══════════════════════════════════════════════════════════════════════════

def export_per_src_csv(result: dict, out_path: str = 'rdf_per_src.csv'):
    """Export per (node, source) breakdown to CSV."""
    import csv
    nodes = result['nodes']
    stats = result['stats']

    rows = []
    for node in nodes:
        s = stats[node]
        all_srcs = sorted(set(
            list(s['rx_by_src'].keys()) +
            list(s['cbf_suppress_by_src'].keys()) +
            list(s['pending_update_by_src'].keys()) +
            list(s['stale_drop_by_src'].keys())
        ))
        for src in all_srcs:
            if src == node:
                continue
            rows.append({
                'Receiver_Node':  node,
                'Source_Node':    src,
                'RX_Count':       s['rx_by_src'].get(src, 0),
                'PD_RX_%':        round(s['pd_as_receiver_by_src'].get(src, 0) * 100, 2),
                'CBF_Suppress':   s['cbf_suppress_by_src'].get(src, 0),
                'CBF_Hop_Ignore': s['cbf_hop_ignore_by_src'].get(src, 0),
                'RDF_Suppress':   s['rdf_suppress_by_src'].get(src, 0),
                'RDF_Hop_Ignore': s['rdf_hop_ignore_by_src'].get(src, 0),
                'Pending_Update': s['pending_update_by_src'].get(src, 0),
                'Stale_Drop':     s['stale_drop_by_src'].get(src, 0),
                'FWD_Count':      s['fwd_by_src'].get(src, 0),
            })

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Exported {len(rows)} rows to: {out_path}")
    return out_path


# ═══════════════════════════════════════════════════════════════════════════
# CELL 7 — AoI distribution
# ═══════════════════════════════════════════════════════════════════════════

def print_aoi_distribution(result: dict, bucket_ms: int = 100):
    """Print AoI histogram across all nodes."""
    rx_events  = result['rx_events']
    tx_origin  = result['tx_origin']
    n_buckets  = 25

    buckets = [0] * n_buckets
    total = excess = 0

    for (node, src, seq), events in rx_events.items():
        origin_t = tx_origin.get((src, seq))
        if origin_t is None:
            continue
        for (rx_t, hop) in events:
            aoi_ms = (rx_t - origin_t) * 1000.0
            if aoi_ms < 0:
                continue
            idx = min(int(aoi_ms / bucket_ms), n_buckets - 1)
            buckets[idx] += 1
            total += 1
            if aoi_ms > TAOI_MS:
                excess += 1

    print(f"\n┌─ AoI Distribution (bucket={bucket_ms}ms, T_AOI={TAOI_MS}ms = 1.125s) ──────────────┐")
    print(f"  Total RX events: {total}  |  Excess (AoI>T_AOI={TAOI_MS}ms): {excess}  "
          f"|  P_EX: {excess/total*100:.2f}%" if total else "  No data")
    print()
    for i, count in enumerate(buckets):
        lo = i * bucket_ms
        hi = (i + 1) * bucket_ms
        bar = '█' * min(int(count / max(buckets) * 40), 40) if max(buckets) > 0 else ''
        marker = ' ← TAOI threshold' if lo <= TAOI_MS < hi else ''
        over   = ' [EXCESS]' if lo >= TAOI_MS else ''
        print(f"  {lo:>5}–{hi:<5}ms │{bar:<40}│ {count:>5}{over}{marker}")


# ═══════════════════════════════════════════════════════════════════════════
# CELL 8 — Main: upload + run
# ═══════════════════════════════════════════════════════════════════════════

def run_analysis(filepath: str = None, tam_s: float = TAM_S):
    """
    Main entry point.
    - In Google Colab: call with no args → prompts file upload
    - Locally: pass filepath directly
    - tam_s: TAM window size in seconds (default = TAM_S constant = 45.05s)
             Set to any value e.g. run_analysis('log.txt', tam_s=10.0)
    """
    if filepath is None:
        if IN_COLAB:
            print("📂 Please upload your log file...")
            uploaded = colab_files.upload()
            filepath = list(uploaded.keys())[0]
            print(f"✅ Uploaded: {filepath}")
        else:
            raise ValueError("Please provide filepath= argument when running locally")

    print(f"\n🔍 Parsing: {filepath}")
    result = parse_log(filepath)

    print(f"✅ Parsed {result['total_nodes']} nodes, "
          f"format={result['fmt']}, "
          f"duration={result['log_duration']:.1f}s")

    print_node_summary(result)
    print_aoi_distribution(result)
    print_pd_windows(result, tam_s=tam_s)

    # Export CSVs
    csv1 = export_csv(result, 'rdf_node_summary.csv')
    csv2 = export_per_src_csv(result, 'rdf_per_src_detail.csv')

    if IN_COLAB:
        colab_files.download(csv1)
        colab_files.download(csv2)

    return result


# ═══════════════════════════════════════════════════════════════════════════
# CELL 9 — Run (copy this cell into Colab and execute)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # ── Local run (provide path directly) ─────────────────────────────────
    import sys
    path   = sys.argv[1] if len(sys.argv) > 1 else 'loglistener2.txt'
    tam_s  = float(sys.argv[2]) if len(sys.argv) > 2 else TAM_S
    if os.path.exists(path):
        result = run_analysis(path, tam_s=tam_s)
    else:
        print(f"File not found: {path}")
        print("Usage: python rdf_analyzer.py <logfile>")
        print("       or call run_analysis() in Colab")

# ═══════════════════════════════════════════════════════════════════════════
# COLAB USAGE (paste into a Colab cell):
# ═══════════════════════════════════════════════════════════════════════════
#
#   # Cell 1: Upload the script
#   from google.colab import files
#   uploaded = files.upload()   # upload rdf_analyzer.py
#
#   # Cell 2: Run analysis (will prompt for log file upload)
#   exec(open('rdf_analyzer.py').read())
#   result = run_analysis()
#
#   # Cell 3: Access individual node stats
#   node1_stats = result['stats'][1]
#   print(node1_stats['pd_as_receiver_avg'])
#   print(node1_stats['pex_overall'])
#
#   # Cell 4: Re-run with a different file
#   result2 = run_analysis('/path/to/other_log.txt')

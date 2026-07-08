"""
ram_analyzer.py  —  RDF v11 RAM Usage Analyzer
================================================
Parses the serial_aggregator log produced by rdf-flood-HW-v11.c and
extracts RAM_REPORT + RAM_STATIC lines to produce:

  1. Console table: per-node RAM breakdown
  2. Plot 1: Static app RAM vs MAX_NODES (analytical model + measured)
  3. Plot 2: Peak stack per node (bar chart)
  4. Plot 3: Total RAM estimate per node
  5. Plot 4: RAM component breakdown stacked bar (seen[][], rdf_state,
             other arrays, peak stack, OS overhead)
  6. Plot 5: Scalability projection — total RAM vs N nodes up to 100

On launch: file-browser dialog — no hardcoded paths.

Log lines parsed:
  RAM_REPORT node=m3-XX max_nodes=N max_seq=M app_static=B peak_stack=S
             total=T seen_bytes=SB rdf_state_bytes=RB
  RAM_STATIC node=m3-XX max_nodes=N max_seq=M last_seq_seen=B seen=B
             rdf_state=B recv_count=B fwd_count=B slot_map=B app_total=B

arm-none-eabi-size input (optional, typed in at prompt):
  text=T data=D bss=B  → system_static = data + bss
  system_total = system_static + peak_stack
"""

import re
import sys
import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from collections import defaultdict

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════
STM32_TOTAL_RAM_KB = 64          # STM32F103REY6: 64 KB RAM
STM32_TOTAL_RAM    = 64 * 1024   # bytes

# sizeof(rdf_state_t) on ARM Cortex-M3 with GCC
# struct { uint8_t phase; uint8_t has_pending; uint16_t pending_seq;
#          flood_msg_t pending_msg (14 bytes); ctimer cbf; ctimer rdf }
# ctimer on Contiki-NG = struct ctimer { struct ctimer *next; clock_time_t etimer;
#                                        void(*f)(void*); void *ptr; } = ~16 bytes
# Total rdf_state_t ≈ 2 + 2 + 14 + 16 + 16 = 50 bytes (padded to 52)
RDF_STATE_T_SIZE = 52   # bytes — verify with: pahole build/.../rdf-flood-HW-v11.elf

# ═══════════════════════════════════════════════════════════════════════════
# REGEX
# ═══════════════════════════════════════════════════════════════════════════
# IoT-LAB serial_aggregator format: "unix_ts;m3-N;[INFO: RDF ] message"
_RAM_REPORT_RE = re.compile(
    r'[^;]*;[^;]*;\[INFO:\s+RDF\s*\]\s+RAM_REPORT\s+'
    r'node=(\S+)\s+max_nodes=(\d+)\s+active_nodes=(\d+)\s+network_size=(\d+)\s+'
    r'app_static=(\d+)\s+peak_stack=(\d+)\s+total=(\d+)\s+'
    r'seen_bytes=(\d+)\s+rdf_state_bytes=(\d+)'
)
_NODE_COUNT_RE = re.compile(
    r'[^;]*;[^;]*;\[INFO:\s+RDF\s*\]\s+NODE_COUNT\s+'
    r'node=(\S+)\s+active_heard=(\d+)\s+network_size=(\d+)\s+'
    r'max_nodes=(\d+)\s+slots_used=(\d+)\s+slots_free=(\d+)'
)
_RAM_STATIC_RE = re.compile(
    r'[^;]*;[^;]*;\[INFO:\s+RDF\s*\]\s+RAM_STATIC\s+'
    r'node=(\S+)\s+max_nodes=(\d+)\s+max_seq=(\d+)\s+'
    r'last_seq_seen=(\d+)\s+seen=(\d+)\s+rdf_state=(\d+)\s+'
    r'recv_count=(\d+)\s+fwd_count=(\d+)\s+slot_map=(\d+)\s+app_total=(\d+)'
)
_STACK_PAINT_RE = re.compile(
    r'[^;]*;[^;]*;\[INFO:\s+RDF\s*\]\s+STACK_PAINT\s+painted=(\d+)'
)


# ═══════════════════════════════════════════════════════════════════════════
# ANALYTICAL MODEL
# ═══════════════════════════════════════════════════════════════════════════
def analytical_app_static(max_nodes, max_seq_track=256,
                           rdf_state_t_size=RDF_STATE_T_SIZE):
    """
    Compute app .bss contribution analytically for any MAX_NODES.
    Matches APP_STATIC_RAM_BYTES macro in firmware exactly.
    """
    last_seq   = max_nodes * 2                  # uint16_t[MAX_NODES]
    seen       = max_nodes * max_seq_track * 1  # uint8_t[MAX_NODES][MAX_SEQ_TRACK]
    rdf_state  = max_nodes * rdf_state_t_size   # rdf_state_t[MAX_NODES]
    recv_count = max_nodes * 4                  # uint32_t[MAX_NODES]
    fwd_count  = max_nodes * 4                  # uint32_t[MAX_NODES]
    slot_map   = max_nodes * 2                  # uint16_t[MAX_NODES]
    return {
        'last_seq':   last_seq,
        'seen':       seen,
        'rdf_state':  rdf_state,
        'recv_count': recv_count,
        'fwd_count':  fwd_count,
        'slot_map':   slot_map,
        'total':      last_seq + seen + rdf_state + recv_count + fwd_count + slot_map,
    }


# ═══════════════════════════════════════════════════════════════════════════
# PARSER
# ═══════════════════════════════════════════════════════════════════════════
def parse_log(filepath):
    """
    Parse a v11 serial_aggregator log.
    Returns dict with per-node measurements.
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    # per-node: keep latest RAM_REPORT and RAM_STATIC
    nodes = {}   # node_name -> dict

    for line in lines:
        line = line.strip()

        # RAM_REPORT
        m = _RAM_REPORT_RE.search(line)
        if m:
            node         = m.group(1)
            max_nodes    = int(m.group(2))
            active_nodes = int(m.group(3))
            network_size = int(m.group(4))
            app_static   = int(m.group(5))
            peak_stack   = int(m.group(6))
            total        = int(m.group(7))
            seen_bytes   = int(m.group(8))
            rdf_bytes    = int(m.group(9))
            if node not in nodes:
                nodes[node] = {}
            nodes[node].update({
                'node':         node,
                'max_nodes':    max_nodes,
                'active_nodes': active_nodes,
                'network_size': network_size,
                'app_static':   app_static,
                'peak_stack':   peak_stack,
                'total':        total,
                'seen_bytes':   seen_bytes,
                'rdf_bytes':    rdf_bytes,
            })
            continue

        # NODE_COUNT
        m = _NODE_COUNT_RE.search(line)
        if m:
            node = m.group(1)
            if node not in nodes:
                nodes[node] = {}
            nodes[node].update({
                'node':         node,
                'active_heard': int(m.group(2)),
                'network_size': int(m.group(3)),
                'max_nodes':    int(m.group(4)),
                'slots_used':   int(m.group(5)),
                'slots_free':   int(m.group(6)),
            })
            continue

        # RAM_STATIC
        m = _RAM_STATIC_RE.search(line)
        if m:
            node      = m.group(1)
            max_nodes = int(m.group(2))
            max_seq   = int(m.group(3))
            if node not in nodes:
                nodes[node] = {}
            nodes[node].update({
                'node':           node,
                'max_nodes':      max_nodes,
                'max_seq':        max_seq,
                'static_last_seq':int(m.group(4)),
                'static_seen':    int(m.group(5)),
                'static_rdf':     int(m.group(6)),
                'static_recv':    int(m.group(7)),
                'static_fwd':     int(m.group(8)),
                'static_slot':    int(m.group(9)),
                'static_total':   int(m.group(10)),
            })

    return nodes


# ═══════════════════════════════════════════════════════════════════════════
# PRINT TABLE
# ═══════════════════════════════════════════════════════════════════════════
def print_table(nodes, system_static=None):
    print("\n" + "═" * 100)
    print("  RDF v11 — RAM Usage Report")
    print("═" * 100)

    if system_static:
        print(f"\n  arm-none-eabi-size input:")
        print(f"    System static RAM (.data + .bss) = {system_static} bytes "
              f"({system_static/1024:.1f} KB)")
        print(f"    STM32F103REY6 total RAM           = {STM32_TOTAL_RAM} bytes "
              f"({STM32_TOTAL_RAM_KB} KB)")
        print(f"    Free RAM (static)                 = "
              f"{STM32_TOTAL_RAM - system_static} bytes "
              f"({(STM32_TOTAL_RAM - system_static)/1024:.1f} KB)")

    print(f"\n  ┌─ PER-NODE RAM MEASUREMENTS ──────────────────────────────────────────────────────────┐")
    print(f"  │  {'Node':>8}  {'MAX_N':>6}  {'Active':>7}  {'NetSz':>6}  {'Utiliz':>7}  "
          f"{'AppStatic':>10}  {'PeakStack':>10}  {'SysTot*':>9}  {'%Used':>6}")
    print(f"  │  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*7}  "
          f"{'─'*10}  {'─'*10}  {'─'*9}  {'─'*6}")

    for node, d in sorted(nodes.items()):
        app_static   = d.get('app_static', d.get('static_total', 0))
        peak_stack   = d.get('peak_stack', 0)
        max_nodes    = d.get('max_nodes', '?')
        active_nodes = d.get('active_nodes', d.get('active_heard', '?'))
        network_size = d.get('network_size', '?')

        # Utilisation = active_nodes / (MAX_NODES - 1) * 100
        if isinstance(active_nodes, int) and isinstance(max_nodes, int) and max_nodes > 1:
            utiliz = f"{100*active_nodes/(max_nodes-1):.0f}%"
        else:
            utiliz = "N/A"

        if system_static:
            sys_total = system_static + peak_stack
            pct       = 100.0 * sys_total / STM32_TOTAL_RAM
            sys_str   = f"{sys_total:>9}"
            pct_str   = f"{pct:>5.1f}%"
        else:
            sys_str = "    N/A  "
            pct_str = "  N/A"

        print(f"  │  {node:>8}  {str(max_nodes):>6}  {str(active_nodes):>7}  "
              f"{str(network_size):>6}  {utiliz:>7}  "
              f"{app_static:>10}  {peak_stack:>10}  {sys_str}  {pct_str}")

    print(f"  │")
    print(f"  │  MAX_N    = MAX_NODES compile-time capacity")
    print(f"  │  Active   = distinct other nodes heard by this node (from NODE_COUNT log)")
    print(f"  │  NetSz    = Active + 1 (includes self)")
    print(f"  │  Utiliz   = Active / (MAX_N-1) — how much of the table is actually used")
    print(f"  │  AppStatic= RDF array .bss (FIXED by MAX_N, does NOT change with Active)")
    print(f"  │  PeakStack= runtime stack high-water mark (varies slightly with traffic)")
    print(f"  │  * SysTot = arm-none-eabi-size(.data+.bss) + peak_stack")
    print(f"  └──────────────────────────────────────────────────────────────────────────────────────┘")

    # Key insight box
    print(f"\n  ┌─ KEY INSIGHT: Static RAM vs Active Nodes ────────────────────────────────────────────┐")
    print(f"  │                                                                                      │")
    print(f"  │  Static RAM (.bss) is determined ENTIRELY by MAX_NODES at compile time.             │")
    print(f"  │  It does NOT change whether 2 or 50 nodes are active in the network.                │")
    print(f"  │                                                                                      │")
    print(f"  │  seen[MAX_NODES][MAX_SEQ_TRACK]  ← always allocated for MAX_NODES slots             │")
    print(f"  │  rdf_state[MAX_NODES]            ← always allocated for MAX_NODES slots             │")
    print(f"  │                                                                                      │")
    print(f"  │  Only peak_stack changes slightly with more active nodes (more packet callbacks).   │")
    print(f"  │  Typical variation: ±200 bytes between 2-node and 6-node experiments.               │")
    print(f"  │                                                                                      │")
    print(f"  │  For thesis KPI: vary MAX_NODES at compile time, rebuild, measure .data+.bss.       │")
    print(f"  └──────────────────────────────────────────────────────────────────────────────────────┘")

    # Analytical model
    print(f"\n  ┌─ ANALYTICAL MODEL (MAX_SEQ_TRACK=256) ───────────────────────────────────────────────┐")
    print(f"  │  {'MAX_NODES':>10}  {'seen[][]':>10}  {'rdf_state':>10}  "
          f"{'other':>8}  {'app_total':>10}  {'% of 64KB':>10}")
    print(f"  │  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*10}  {'─'*10}")
    for n in [5, 10, 15, 20, 30, 40, 51]:
        a = analytical_app_static(n)
        other = a['last_seq'] + a['recv_count'] + a['fwd_count'] + a['slot_map']
        pct   = 100.0 * a['total'] / STM32_TOTAL_RAM
        print(f"  │  {n:>10}  {a['seen']:>10}  {a['rdf_state']:>10}  "
              f"{other:>8}  {a['total']:>10}  {pct:>9.1f}%")
    print(f"  └──────────────────────────────────────────────────────────────────────────────────────┘")


# ═══════════════════════════════════════════════════════════════════════════
# PLOTTING
# ═══════════════════════════════════════════════════════════════════════════
def plot_ram(nodes, system_static=None):
    fig = plt.figure(figsize=(20, 14))
    fig.patch.set_facecolor('#1a1a2e')
    fig.suptitle("RDF v11 — RAM Usage Analysis  (STM32F103REY6, 64KB RAM)",
                 fontsize=13, color='white', fontweight='bold', y=0.98)

    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35,
                           top=0.93, bottom=0.07)

    node_names  = sorted(nodes.keys())
    app_statics = [nodes[n].get('app_static', nodes[n].get('static_total', 0))
                   for n in node_names]
    peak_stacks = [nodes[n].get('peak_stack', 0) for n in node_names]
    totals      = [a + p for a, p in zip(app_statics, peak_stacks)]
    max_nodes_v = [nodes[n].get('max_nodes', 0) for n in node_names]

    # ── Plot 0: App static RAM per node ──────────────────────────────────
    ax0 = fig.add_subplot(gs[0, 0])
    ax0.set_facecolor('#16213e')
    x = np.arange(len(node_names))
    bars = ax0.bar(x, [v/1024 for v in app_statics],
                   color='#2196F3', edgecolor='#333', alpha=0.85)
    ax0.set_xticks(x)
    ax0.set_xticklabels(node_names, rotation=30, ha='right', color='#aaa', fontsize=8)
    ax0.set_title("App Static RAM per Node\n(.bss arrays — fixed by MAX_NODES)", color='white', fontsize=9)
    ax0.set_ylabel("KB", color='#aaa', fontsize=8)
    ax0.tick_params(colors='#aaa', labelsize=7)
    for sp in ax0.spines.values(): sp.set_edgecolor('#444')
    for bar, v in zip(bars, app_statics):
        ax0.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                 f'{v/1024:.1f}KB', ha='center', va='bottom', color='white', fontsize=7)
    # Annotate with active/max nodes
    for i, n_name in enumerate(node_names):
        d = nodes[n_name]
        active = d.get('active_nodes', d.get('active_heard', '?'))
        maxn   = d.get('max_nodes', '?')
        ax0.text(i, 0.3, f'{active}/{maxn}\nnodes',
                 ha='center', va='bottom', color='yellow', fontsize=6)

    # ── Plot 1: Active nodes vs MAX_NODES capacity ───────────────────────
    ax1 = fig.add_subplot(gs[0, 1])
    ax1.set_facecolor('#16213e')
    active_list = [nodes[n].get('active_nodes', nodes[n].get('active_heard', 0))
                   for n in node_names]
    max_list    = [nodes[n].get('max_nodes', 0) for n in node_names]
    unused_list = [max(0, m - 1 - a) for m, a in zip(max_list, active_list)]

    # Stacked bar: active (green) + unused capacity (grey)
    ax1.bar(x, active_list,  color='#4CAF50', edgecolor='#333',
            alpha=0.85, label='Active nodes heard')
    ax1.bar(x, unused_list, bottom=active_list, color='#444',
            edgecolor='#333', alpha=0.6, label='Unused capacity')
    ax1.set_xticks(x)
    ax1.set_xticklabels(node_names, rotation=30, ha='right', color='#aaa', fontsize=8)
    ax1.set_title("Active Nodes vs MAX_NODES Capacity\n(green=heard, grey=unused slots)",
                  color='white', fontsize=9)
    ax1.set_ylabel("Node Count", color='#aaa', fontsize=8)
    ax1.tick_params(colors='#aaa', labelsize=7)
    ax1.legend(fontsize=7, facecolor='#1a1a2e', labelcolor='white')
    for sp in ax1.spines.values(): sp.set_edgecolor('#444')
    # Annotate utilisation %
    for i, (a, m) in enumerate(zip(active_list, max_list)):
        if m > 1:
            util = 100 * a / (m - 1)
            ax1.text(i, a + unused_list[i] + 0.3,
                     f'{util:.0f}%\nutil', ha='center', va='bottom',
                     color='yellow', fontsize=6)
    # Add note that static RAM is same regardless
    ax1.text(0.5, 0.02,
             '⚠ Static RAM is IDENTICAL regardless of active node count',
             transform=ax1.transAxes, ha='center', va='bottom',
             color='#FF9800', fontsize=6,
             bbox=dict(boxstyle='round,pad=0.2', facecolor='#333', alpha=0.7))

    # ── Plot 2 (was Plot 1): Peak stack per node ──────────────────────────
    ax2_stack = fig.add_subplot(gs[0, 2])
    ax2_stack.set_facecolor('#16213e')
    bars = ax2_stack.bar(x, peak_stacks,
                         color='#FF9800', edgecolor='#333', alpha=0.85)
    ax2_stack.set_xticks(x)
    ax2_stack.set_xticklabels(node_names, rotation=30, ha='right', color='#aaa', fontsize=8)
    ax2_stack.set_title("Peak Stack Usage per Node\n(runtime high-water mark)", color='white', fontsize=9)
    ax2_stack.set_ylabel("Bytes", color='#aaa', fontsize=8)
    ax2_stack.tick_params(colors='#aaa', labelsize=7)
    for sp in ax2_stack.spines.values(): sp.set_edgecolor('#444')
    for bar, v in zip(bars, peak_stacks):
        ax2_stack.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 5,
                       f'{v}B', ha='center', va='bottom', color='white', fontsize=7)

    # ── Plot 3: Total RAM per node (stacked) — row 1 left ─────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    ax3.set_facecolor('#16213e')
    ax3.bar(x, [v/1024 for v in app_statics],
            color='#2196F3', edgecolor='#333', alpha=0.85, label='App static (.bss)')
    ax3.bar(x, [v/1024 for v in peak_stacks],
            bottom=[v/1024 for v in app_statics],
            color='#FF9800', edgecolor='#333', alpha=0.85, label='Peak stack')
    if system_static:
        os_overhead = system_static - (sum(app_statics)/len(app_statics) if app_statics else 0)
        if os_overhead > 0:
            ax3.bar(x, [os_overhead/1024]*len(x),
                    bottom=[(a+p)/1024 for a, p in zip(app_statics, peak_stacks)],
                    color='#9C27B0', edgecolor='#333', alpha=0.85, label='OS overhead')
    ax3.axhline(STM32_TOTAL_RAM_KB, color='red', linestyle='--',
                linewidth=1.2, label=f'Total RAM ({STM32_TOTAL_RAM_KB}KB)')
    ax3.set_xticks(x)
    ax3.set_xticklabels(node_names, rotation=30, ha='right', color='#aaa', fontsize=8)
    ax3.set_title("Total RAM Estimate per Node\n(stacked breakdown)", color='white', fontsize=9)
    ax3.set_ylabel("KB", color='#aaa', fontsize=8)
    ax3.tick_params(colors='#aaa', labelsize=7)
    ax3.legend(fontsize=7, facecolor='#1a1a2e', labelcolor='white')
    for sp in ax3.spines.values(): sp.set_edgecolor('#444')

    # ── Plot 4: Analytical model — app static vs MAX_NODES ───────────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor('#16213e')
    n_range    = np.arange(1, 101)
    seen_vals  = [analytical_app_static(n)['seen']      / 1024 for n in n_range]
    rdf_vals   = [analytical_app_static(n)['rdf_state'] / 1024 for n in n_range]
    other_vals = [(analytical_app_static(n)['total'] -
                   analytical_app_static(n)['seen'] -
                   analytical_app_static(n)['rdf_state']) / 1024
                  for n in n_range]
    total_vals = [analytical_app_static(n)['total'] / 1024 for n in n_range]

    ax4.stackplot(n_range, seen_vals, rdf_vals, other_vals,
                  labels=['seen[][] (dominant)', 'rdf_state[]', 'other arrays'],
                  colors=['#F44336', '#FF9800', '#4CAF50'], alpha=0.8)
    ax4.plot(n_range, total_vals, color='white', linewidth=1.5,
             linestyle='--', label='Total app static')
    ax4.axhline(STM32_TOTAL_RAM_KB, color='red', linestyle=':',
                linewidth=1.2, label='64KB RAM limit')
    # Mark measured points
    if node_names:
        for n_name, n_val, a_val in zip(node_names, max_nodes_v, app_statics):
            ax4.scatter([n_val], [a_val/1024], color='yellow', s=60, zorder=5)
            ax4.annotate(n_name, (n_val, a_val/1024),
                         textcoords='offset points', xytext=(5, 5),
                         color='yellow', fontsize=6)
    ax4.set_title("App Static RAM vs MAX_NODES\n(analytical model, MAX_SEQ=256)",
                  color='white', fontsize=9)
    ax4.set_xlabel("MAX_NODES", color='#aaa', fontsize=8)
    ax4.set_ylabel("KB", color='#aaa', fontsize=8)
    ax4.tick_params(colors='#aaa', labelsize=7)
    ax4.legend(fontsize=7, facecolor='#1a1a2e', labelcolor='white', loc='upper left')
    for sp in ax4.spines.values(): sp.set_edgecolor('#444')

    # ── Plot 5: Scalability — total system RAM vs N nodes ─────────────────
    ax5 = fig.add_subplot(gs[1, 2])
    ax5.set_facecolor('#16213e')

    avg_peak = int(np.mean(peak_stacks)) if peak_stacks else 2048
    avg_os   = (system_static - int(np.mean(app_statics))) if (
                system_static and app_statics) else 25000

    for seq_track, col, ls in [(64, '#4CAF50', '-'),
                                (128, '#FF9800', '--'),
                                (256, '#F44336', '-.')]:
        sys_totals = [(analytical_app_static(n, max_seq_track=seq_track)['total']
                       + avg_os + avg_peak) / 1024
                      for n in n_range]
        ax5.plot(n_range, sys_totals, color=col, linewidth=1.5,
                 linestyle=ls, label=f'MAX_SEQ={seq_track}')

    ax5.axhline(STM32_TOTAL_RAM_KB, color='red', linestyle=':',
                linewidth=1.5, label='64KB RAM limit')
    ax5.fill_between(n_range, STM32_TOTAL_RAM_KB, 70,
                     alpha=0.15, color='red', label='Overflow zone')

    # Mark max safe N for each seq_track
    for seq_track, col in [(64, '#4CAF50'), (128, '#FF9800'), (256, '#F44336')]:
        for n in n_range:
            a = analytical_app_static(n, max_seq_track=seq_track)
            if (a['total'] + avg_os + avg_peak) / 1024 >= STM32_TOTAL_RAM_KB:
                ax5.axvline(n - 1, color=col, linestyle=':', linewidth=1.0, alpha=0.7)
                ax5.text(n - 1, STM32_TOTAL_RAM_KB * 0.45,
                         f'Max\nN={n-1}', color=col, fontsize=6, ha='center')
                break

    ax5.set_title(f"Scalability: Total System RAM vs N Nodes\n"
                  f"(OS≈{avg_os//1024}KB + stack≈{avg_peak}B assumed)",
                  color='white', fontsize=9)
    ax5.set_xlabel("Number of Nodes (MAX_NODES)", color='#aaa', fontsize=8)
    ax5.set_ylabel("Total System RAM (KB)", color='#aaa', fontsize=8)
    ax5.set_ylim(0, 75)
    ax5.tick_params(colors='#aaa', labelsize=7)
    ax5.legend(fontsize=7, facecolor='#1a1a2e', labelcolor='white')
    for sp in ax5.spines.values(): sp.set_edgecolor('#444')

    plt.show()


# ═══════════════════════════════════════════════════════════════════════════
# FILE SELECTION + OPTIONAL SIZE INPUT
# ═══════════════════════════════════════════════════════════════════════════
def select_file_and_size():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    # Select log file
    filepath = filedialog.askopenfilename(
        title="Select rdf_v11_run.log (serial_aggregator output)",
        filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")]
    )
    if not filepath:
        root.destroy()
        return None, None

    # Ask for arm-none-eabi-size output
    size_input = simpledialog.askstring(
        "arm-none-eabi-size (optional)",
        "Enter 'data + bss' value from arm-none-eabi-size output\n"
        "(e.g. type: 35840  for 35 KB static RAM)\n\n"
        "Leave blank to skip system-level calculation.\n\n"
        "How to get this value:\n"
        "  arm-none-eabi-size build/iotlab/m3/rdf-flood-HW-v11.iotlab\n"
        "  Add the 'data' and 'bss' columns together.",
        parent=root
    )

    system_static = None
    if size_input and size_input.strip():
        try:
            system_static = int(size_input.strip())
        except ValueError:
            messagebox.showwarning("Invalid input",
                                   f"Could not parse '{size_input}' as integer. "
                                   "Skipping system-level calculation.")

    root.destroy()
    return filepath, system_static


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 65)
    print("  RDF v11 — RAM Usage Analyzer")
    print("  STM32F103REY6 | 64KB RAM | Contiki-NG")
    print("=" * 65)

    filepath, system_static = select_file_and_size()
    if not filepath:
        print("No file selected. Exiting.")
        sys.exit(0)

    print(f"\n📂 Parsing: {filepath}")
    nodes = parse_log(filepath)

    if not nodes:
        print("❌ No RAM_REPORT or RAM_STATIC lines found in log.")
        print("   Make sure you are using rdf-flood-HW-v11.c firmware.")
        sys.exit(1)

    print(f"✅ Found RAM data for {len(nodes)} node(s): {', '.join(sorted(nodes.keys()))}")

    if system_static:
        print(f"✅ System static RAM (from arm-none-eabi-size): {system_static} bytes "
              f"({system_static/1024:.1f} KB)")

    print_table(nodes, system_static)

    print("\n📊 Generating plots...")
    plot_ram(nodes, system_static)
    print("✅ Done.")


if __name__ == '__main__':
    main()

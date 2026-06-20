=============================================================================
  Rate Decay Flooding (RDF)  —  Contiki-NG / COOJA Port
  Based on: Fuger & Timm-Giel, "On the Feasibility of Position-Flooding
            in Urban UAV Networks", IEEE VTC 2023-Spring
=============================================================================

WHAT THIS CODE DOES
───────────────────
This implements Rate Decay Flooding (RDF) — a protocol where every node
periodically broadcasts its position, and neighbours forward those packets
through the network (flooding). Two mechanisms reduce redundant traffic:

  1. CBF (Contention-Based Flooding):
     When multiple nodes could forward, the one FARTHEST from the sender
     sets a SHORT timer and goes first. Nodes that hear it forwarded cancel
     their own pending timers — no redundant TX.

  2. Rate Decay:
     Each hop SLOWS DOWN how often a packet stream is forwarded.
     Formula: wait = i * h^q   (i = base interval, h = hops, q = exponent)
     At hop 2 with q=2: wait = 125ms * 4 = 500ms between forwards.
     At hop 3 with q=2: wait = 125ms * 9 = 1125ms between forwards.
     Far hops forward less frequently → less channel congestion.

FILES
─────
  rdf-flood.c       Main application — do not edit parameters here
  project-conf.h    ALL tuning knobs — edit only this file
  Makefile          Build system — set CONTIKI_ROOT here

=============================================================================
  HOW TO BUILD AND RUN IN COOJA
=============================================================================

STEP 1: Place files
  Copy this folder to:
    contiki-ng/examples/rdf-flood/

STEP 2: Build
  cd contiki-ng/examples/rdf-flood/
  make TARGET=cooja

  If your contiki-ng is in a custom path:
    make TARGET=cooja CONTIKI_ROOT=/home/user/contiki-ng

STEP 3: Open COOJA
  From contiki-ng/tools/cooja/:
    ant run

STEP 4: Create simulation
  File → New Simulation → OK
  Motes → Add Motes → Cooja Mote
  → Browse to: contiki-ng/examples/rdf-flood/rdf-flood.cooja
  → Add 16 or 32 motes

STEP 5: Place nodes
  Place nodes in a GRID matching your COORD_GRID_COLS and COORD_SPACING_M.
  Example (defaults): 8 columns, 50m spacing
    Node 1  at (0 m,   0 m)
    Node 2  at (50 m,  0 m)
    Node 3  at (100 m, 0 m)
    ...
    Node 9  at (0 m,   50 m)
  The code DERIVES virtual coordinates from node_id — your physical
  placement in COOJA must match this layout exactly.

STEP 6: Set radio range
  In COOJA: Edit → Simulation parameters → Unit Disk Graph Medium
  Set TX range to match DIST_MAX_RANGE_M (default: 200 m)
  Set Interference range to 2× TX range (default: 400 m)

STEP 7: Run
  Press Start. Watch Mote Output window for RDF log lines.

=============================================================================
  PARAMETERS — WHAT EACH ONE DOES AND HOW TO CHANGE IT
=============================================================================

All parameters live in project-conf.h.
To change a parameter: REMOVE the "//" from the front of the line.
To restore default:   ADD "//" back to the front of the line.

─────────────────────────────────────────────────────────────────────────────
PARAMETER 1: SEND_INTERVAL   (base broadcast interval i)
─────────────────────────────────────────────────────────────────────────────
What it does:
  How often each node broadcasts its own position update.
  This is the "i" in the paper's formula T'_fwd = T_fwd + i*h^q.
  Paper used i = 120 ms. Default here: CLOCK_SECOND/8 ≈ 125 ms.

  Smaller i → more frequent updates → more channel load → risk of congestion
  Larger i  → less frequent updates → less load → higher position uncertainty

How to change in project-conf.h:
  DEFAULT (leave commented):
    /* #define SEND_INTERVAL        (CLOCK_SECOND / 8) */

  To use 250 ms:
    #define SEND_INTERVAL        (CLOCK_SECOND / 4)

  To use 500 ms:
    #define SEND_INTERVAL        (CLOCK_SECOND / 2)

─────────────────────────────────────────────────────────────────────────────
PARAMETER 2: RDF_Q   (decay exponent)
─────────────────────────────────────────────────────────────────────────────
What it does:
  Controls how aggressively the forwarding rate drops each hop.
  The wait before forwarding = SEND_INTERVAL * (hop_count ^ RDF_Q)

  RDF_Q = 0  → No decay. Every hop forwards at same rate. Pure CBF.
  RDF_Q = 1  → Linear decay: hop 2 waits 2x, hop 3 waits 3x.
  RDF_Q = 2  → Quadratic (paper optimal): hop 2 waits 4x, hop 3 waits 9x.
  RDF_Q = 3  → Cubic: very aggressive suppression at far hops.

How to change in project-conf.h:
  DEFAULT:
    /* #define RDF_Q                2 */

  To try linear decay:
    #define RDF_Q                1

  To try pure CBF (no decay):
    #define RDF_Q                0

─────────────────────────────────────────────────────────────────────────────
PARAMETER 3: RDF_MIN_JITTER / RDF_MAX_JITTER
─────────────────────────────────────────────────────────────────────────────
What it does:
  A small random delay added on top of every contention timer. Prevents
  nodes at exactly equal distances from colliding simultaneously.

  Too small → synchronized transmissions → collisions
  Too large → delays that override the distance-based ordering

How to change in project-conf.h:
  DEFAULT:
    /* #define RDF_MIN_JITTER       (CLOCK_SECOND / 100) */   // 10 ms
    /* #define RDF_MAX_JITTER       (CLOCK_SECOND / 20)  */   // 50 ms

  To reduce jitter (tighter timing, higher collision risk):
    #define RDF_MIN_JITTER       (CLOCK_SECOND / 200)         // 5 ms
    #define RDF_MAX_JITTER       (CLOCK_SECOND / 50)          // 20 ms

─────────────────────────────────────────────────────────────────────────────
PARAMETER 4: DIST_MIN_WAIT_MS / DIST_MAX_WAIT_MS
─────────────────────────────────────────────────────────────────────────────
What it does:
  The contention timer range for CBF.
  FARTHEST node from sender → DIST_MIN_WAIT_MS (fires first, wins race).
  CLOSEST node to sender   → DIST_MAX_WAIT_MS (holds back, suppressed).

  DIST_MAX_WAIT_MS must be less than SEND_INTERVAL or the flood stalls.
  Smaller window → faster flood, more collisions.
  Larger window  → slower flood, better suppression.

How to change in project-conf.h:
  DEFAULT:
    /* #define DIST_MIN_WAIT_MS     10  */
    /* #define DIST_MAX_WAIT_MS     150 */

  To narrow the window (aggressive/fast):
    #define DIST_MIN_WAIT_MS     5
    #define DIST_MAX_WAIT_MS     80

─────────────────────────────────────────────────────────────────────────────
PARAMETER 5: DIST_MAX_RANGE_M   (radio range cap)
─────────────────────────────────────────────────────────────────────────────
What it does:
  The maximum distance used for the CBF timer mapping.
  Any node beyond this distance gets DIST_MIN_WAIT (highest priority).
  Set this to match your COOJA unit-disk radio TX range.

  Paper (802.11p outdoors): ~509 m
  802.15.4 in COOJA (typical): 50–200 m depending on model

How to change in project-conf.h:
  DEFAULT:
    /* #define DIST_MAX_RANGE_M     200 */

  For a small indoor simulation (50 m range):
    #define DIST_MAX_RANGE_M     50

  For a larger outdoor simulation (300 m range):
    #define DIST_MAX_RANGE_M     300

─────────────────────────────────────────────────────────────────────────────
PARAMETER 6: COORD_GRID_COLS / COORD_SPACING_M   (virtual grid layout)
─────────────────────────────────────────────────────────────────────────────
What it does:
  Defines the virtual coordinate grid derived from node_id.
  COORD_GRID_COLS = how many nodes per row.
  COORD_SPACING_M = distance in metres between adjacent nodes.

  Example with defaults (COLS=8, SPACING=50m):
    Node 1 → (0,0)    Node 2 → (50,0)  ... Node 8 → (350,0)
    Node 9 → (0,50)   Node 10→ (50,50) ... Node 16→ (350,50)

  YOU MUST PLACE NODES IN COOJA AT THESE EXACT POSITIONS.
  If your COOJA layout doesn't match, CBF distances will be wrong.

How to change in project-conf.h:
  DEFAULT:
    /* #define COORD_GRID_COLS      8  */
    /* #define COORD_SPACING_M      50 */

  For a 4-column grid at 30m spacing:
    #define COORD_GRID_COLS      4
    #define COORD_SPACING_M      30

─────────────────────────────────────────────────────────────────────────────
PARAMETER 7: MAX_NODES / MAX_SEQ_TRACK
─────────────────────────────────────────────────────────────────────────────
What it does:
  MAX_NODES:     Size of all per-node arrays. Set to at least (num_nodes + 1).
  MAX_SEQ_TRACK: Rolling window for duplicate detection per source.
                 MUST be a power of 2 (64, 128, 256, 512...).
                 Larger = can detect older duplicates, uses more RAM.

How to change in project-conf.h:
  DEFAULT:
    /* #define MAX_NODES            32  */
    /* #define MAX_SEQ_TRACK        256 */

  For a 10-node simulation to save RAM:
    #define MAX_NODES            12
    #define MAX_SEQ_TRACK        64

=============================================================================
  READING LOG OUTPUT
=============================================================================

Every RDF log line starts with "[RDF]". Key lines to watch:

  [RDF] node_id=3 virtual_xy=(100 m, 0 m)
    → Printed once at startup. Confirms node's virtual coordinates.
      If this doesn't match your COOJA layout, fix COORD_GRID_COLS/SPACING.

  [RDF] TX seq=12 xy=(100,0)
    → This node is broadcasting its own position update (seq number 12).

  [RDF] RX src=1 seq=5 hop=2 src_xy=(0,0) sender_xy=(50,0) my_xy=(100,0) e2e=48 ms
    → Received a flood packet. Break it down:
        src=1        flood originated at node 1
        seq=5        sequence number 5
        hop=2        this packet has been forwarded twice
        src_xy       where node 1 is (virtual)
        sender_xy    where the immediate sender is (node 2 at 50,0)
        my_xy        where I am
        e2e=48 ms    end-to-end delay from origin_time to now

  [RDF] FWD src=1 seq=5 hop=3 sender_xy=(100,0)
    → This node is forwarding the packet. hop is now 3. sender_xy is
      updated to this node's own coordinates (Fix #3).

  [RDF] CBF suppress src=1 seq=5
    → A neighbour already forwarded (src=1, seq=5). Our pending timer
      was cancelled. This is the CBF overhearing suppression working.

  [RDF] PDR src=1 recv=47/50 = 94.0%
    → Running packet delivery ratio. 47 out of 50 expected packets
      from source 1 were received.

=============================================================================
  FUNCTIONS IN rdf-flood.c (QUICK REFERENCE)
=============================================================================

  init_node_coordinates()
    Called once at startup. Computes my_x_m / my_y_m from node_id.
    Depends on COORD_GRID_COLS and COORD_SPACING_M.

  rdf_decay_interval(hops)
    Returns i * h^q in clock ticks.
    This is equation (2) from the paper.
    Depends on SEND_INTERVAL and RDF_Q.

  distance_based_wait(msg)
    Returns the CBF contention timer for THIS node relative to the
    IMMEDIATE sender (msg->sender_x_m / sender_y_m).
    Depends on DIST_MIN_WAIT_MS, DIST_MAX_WAIT_MS, DIST_MAX_RANGE_M.

  rdf_cancel_pending(src, seq)
    Cancels a pending CBF forward. Called on duplicate receive AND
    on buffered-then-overheard receive (Fix #2).

  rdf_forward_callback(ptr)
    Timer callback. Increments hop_count, stamps own coordinates as
    sender_xy (Fix #3), transmits, sets next_allowed_time.

  rdf_handle_new_packet(src, msg)
    Decides whether to schedule, skip, or defer a forward based on
    rate-decay state and CBF contention.

  udp_rx_callback(...)
    Called by Contiki on every received UDP packet. Implements the
    full RDF receive-side logic tree.

  rdf_process (main loop)
    Periodic timer fires every SEND_INTERVAL ± jitter.
    Broadcasts own position update with hop_count=0.

=============================================================================
  QUICK EXPERIMENT GUIDE
=============================================================================

  Experiment 1 — Baseline CBF (no rate decay):
    In project-conf.h:
      #define RDF_Q   0
    Result: every node forwards every packet, high channel load.

  Experiment 2 — Paper-optimal RDF:
    In project-conf.h:
      /* #define RDF_Q   2 */   ← keep commented (default is 2)
    Result: best PDR vs. traffic density trade-off per paper.

  Experiment 3 — Dense network stress test:
    In project-conf.h:
      #define MAX_NODES         32
      #define COORD_GRID_COLS   8
      #define COORD_SPACING_M   30
      #define DIST_MAX_RANGE_M  60
    Add 32 nodes in COOJA at 30m grid spacing, TX range = 60m.

  Experiment 4 — Reduce traffic (higher decay):
    In project-conf.h:
      #define RDF_Q   3
    Higher q means far hops forward much less frequently.
    PDR at distance will drop. Good for bandwidth-limited scenarios.

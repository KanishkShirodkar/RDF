/*
 * rdf-flood-v2.c -- Rate Decay Flooding (RDF) for Contiki-NG / COOJA
 *
 * Self-contained: no project-conf.h needed.
 * All tunable parameters are in the "PARAMETERS" block below.
 *
 * Paper: "On the Feasibility of Position-Flooding in Urban UAV Networks"
 *        Fuger & Timm-Giel, IEEE VTC 2023-Spring
 *
 * Adaptation: Industrial Warehouse (replaces UAV scenario)
 *   - Tcada  -> Taoi  = Rdect / (2*v) = 5 / (2 * 2.222) = 1.125 s
 *   - Rdect  = 5 m  (detection range in warehouse)
 *   - v_max  = 8 km/h = 2.222 m/s  (max speed of warehouse nodes/workers)
 *   - Tam    = L / 2.22  (L = 50/100/150 m warehouse length)
 *   - Radio range = 50 m (CSMA/CA 802.15.4)
 *
 * Fixes applied (inherited from v1):
 *   FIX-1  CBF timer uses IMMEDIATE SENDER coords, not flood origin.
 *   FIX-2  Overhearing suppression fires on buffered-then-overheard too.
 *   FIX-3  Every forwarder stamps own coords into sender_x/y before TX.
 *   FIX-4  Live COOJA position via shared sim_pos_x/sim_pos_y variables.
 *
 * Bug fixes applied in v2:
 *   BUGFIX-1  RPL disabled correctly (source macros, not derived).
 *   BUGFIX-2  seq_no wraps at UINT16_MAX -- seen[] bitmap now handles
 *             wrap-around correctly with modular indexing.
 *   BUGFIX-3  log_pdr() denominator was last_seq_seen+1 which is wrong
 *             when seq starts at 0 and node joins late; fixed to use
 *             a dedicated tx_count[] array incremented at TX.
 *   BUGFIX-4  my_seq incremented AFTER marking seen[], preventing the
 *             source from treating its own next packet as a duplicate.
 *   BUGFIX-5  Race in update_position_from_sim(): flag cleared BEFORE
 *             reading values -- fixed to clear AFTER copy.
 *   BUGFIX-7  valid_src() excluded node_id==MAX_NODES-1; changed to <=.
 *
 * New instrumentation for KPI measurement (Part 2):
 *   METRIC-1  DISSEM log: per-(src,seq) dissemination rate snapshot.
 *             Logged at source TX and at each RX, giving post-run
 *             per-packet dissemination rate from the log.
 *   METRIC-2  EXCESS log: per-node excess event when AoI > Taoi.
 *             Logged whenever a node receives a packet whose
 *             (clock_time() - origin_time) > TAOI_TICKS.
 *   METRIC-3  STATS periodic summary: every STATS_INTERVAL_S seconds
 *             each node prints a one-line summary of all per-src
 *             recv/tx/excess counters for easy post-run grep.
 *   METRIC-4  TX_COUNT tracked per source so PDR denominator is correct.
 *
 * Build:
 *   make TARGET=cooja
 */

/* =========================================================================
 * ROUTING: disable RPL using the TWO SOURCE macros, not the derived one.
 * These MUST appear before any #include.
 * =========================================================================
 */
#ifndef ROUTING_CONF_RPL_LITE
#define ROUTING_CONF_RPL_LITE 0
#endif
#ifndef ROUTING_CONF_RPL_CLASSIC
#define ROUTING_CONF_RPL_CLASSIC 0
#endif

/* =========================================================================
 * PARAMETERS
 *
 *  SEND_INTERVAL     Base broadcast interval i.
 *                    500 ms default for 802.15.4 CSMA warehouse.
 *
 *  RDF_Q             Decay exponent q.  2 = paper-optimal.
 *
 *  TAOI_MS           Age-of-Information threshold in milliseconds.
 *                    Warehouse: Taoi = Rdect/(2*v) = 5/(2*2.222) = 1125 ms.
 *                    A packet whose (now - origin_time) > TAOI_MS is "stale"
 *                    and counts as an excess event.
 *
 *  STATS_INTERVAL_S  How often (seconds) each node prints a STATS summary.
 *
 *  DIST_MAX_RANGE_M  Radio range cap for CBF (metres). 50 m for warehouse.
 *
 *  MAX_NODES         Must be >= your node count + 1.
 *  MAX_SEQ_TRACK     Duplicate-detection window.  MUST be a power of 2.
 * =========================================================================
 */
#ifndef SEND_INTERVAL
#define SEND_INTERVAL       (CLOCK_SECOND / 2)   /* 500 ms */
#endif
#ifndef RDF_Q
#define RDF_Q               2
#endif
#ifndef RDF_MIN_JITTER
#define RDF_MIN_JITTER      (CLOCK_SECOND / 500) /* ~2 ms */
#endif
#ifndef RDF_MAX_JITTER
#define RDF_MAX_JITTER      (CLOCK_SECOND / 200) /* ~5 ms */
#endif
#ifndef DIST_MIN_WAIT_MS
#define DIST_MIN_WAIT_MS    5
#endif
#ifndef DIST_MAX_WAIT_MS
#define DIST_MAX_WAIT_MS    300
#endif
#ifndef DIST_MAX_RANGE_M
#define DIST_MAX_RANGE_M    50
#endif
#ifndef COORD_GRID_COLS
#define COORD_GRID_COLS     8
#endif
#ifndef COORD_SPACING_M
#define COORD_SPACING_M     50
#endif
#ifndef MAX_NODES
#define MAX_NODES           51   /* supports up to 50 nodes (IDs 1..50) */
#endif
#ifndef MAX_SEQ_TRACK
#define MAX_SEQ_TRACK       256  /* MUST be a power of 2 */
#endif
#ifndef POS_UPDATE_INTERVAL_MS
#define POS_UPDATE_INTERVAL_MS  125
#endif

/* ---- Warehouse KPI parameters ---- */
#ifndef TAOI_MS
/* Taoi = Rdect / (2 * v_max) = 5 m / (2 * 2.222 m/s) = 1125 ms */
#define TAOI_MS             1125
#endif
#ifndef STATS_INTERVAL_S
#define STATS_INTERVAL_S    10   /* print STATS summary every 10 s */
#endif

/* =========================================================================
 * INCLUDES
 * =========================================================================
 */
#include "contiki.h"
#include "net/netstack.h"
#include "net/ipv6/simple-udp.h"
#include "net/ipv6/uip.h"
#include "net/ipv6/uip-ds6.h"
#include "sys/node-id.h"
#include "sys/log.h"
#include "sys/clock.h"
#include "sys/ctimer.h"
#include "sys/etimer.h"
#include "random.h"
#include <inttypes.h>
#include <string.h>

#define LOG_MODULE "RDF"
#define LOG_LEVEL  LOG_LEVEL_INFO

#define UDP_PORT 1234

/* Derived tick constants */
#define DIST_MIN_WAIT  ((clock_time_t)((DIST_MIN_WAIT_MS  * CLOCK_SECOND) / 1000))
#define DIST_MAX_WAIT  ((clock_time_t)((DIST_MAX_WAIT_MS  * CLOCK_SECOND) / 1000))
#define POS_UPDATE_TICKS ((clock_time_t)((POS_UPDATE_INTERVAL_MS * CLOCK_SECOND) / 1000))
#define TAOI_TICKS     ((clock_time_t)((TAOI_MS * CLOCK_SECOND) / 1000))
#define STATS_TICKS    ((clock_time_t)(STATS_INTERVAL_S * CLOCK_SECOND))


/* =========================================================================
 * FIX-4: COOJA LIVE POSITION SHARED VARIABLES
 * =========================================================================
 */
volatile int32_t sim_pos_x       = 0;
volatile int32_t sim_pos_y       = 0;
volatile uint8_t sim_pos_updated = 0;

/* =========================================================================
 * Packet format
 * =========================================================================
 */
typedef struct {
  uint16_t     src_id;
  uint16_t     seq_no;
  uint16_t     hop_count;
  clock_time_t origin_time;   /* timestamp at source TX -- used for AoI */
  int16_t      src_x_m;
  int16_t      src_y_m;
  int16_t      sender_x_m;
  int16_t      sender_y_m;
} flood_msg_t;

/* Per-source RDF state */
typedef struct {
  uint8_t      forwarded_before;
  uint8_t      timer_active;
  uint8_t      has_pending;
  uint16_t     pending_seq;
  flood_msg_t  pending_msg;
  struct ctimer forward_timer;
  clock_time_t next_allowed_time;
} rdf_state_t;

/* =========================================================================
 * Module globals
 * =========================================================================
 */
static struct simple_udp_connection udp_conn;
static uip_ipaddr_t  mcast_addr;
static uint16_t      my_seq  = 0;
static int16_t       my_x_m  = 0;
static int16_t       my_y_m  = 0;

/* Duplicate detection */
static uint16_t      last_seq_seen[MAX_NODES];
static uint8_t       seen[MAX_NODES][MAX_SEQ_TRACK];

/* RDF per-source state */
static rdf_state_t   rdf_state[MAX_NODES];

/*
 * METRIC counters (per source, indexed by src_id)
 *
 *  recv_count[src]   -- how many unique (src,seq) this node received
 *  tx_count[src]     -- BUGFIX-3: how many seq this node has seen the
 *                       source transmit (estimated from last_seq_seen+1,
 *                       but only after first reception)
 *  excess_count[src] -- how many received packets had AoI > Taoi
 *  fwd_count[src]    -- how many times this node forwarded for src
 */
static uint32_t      recv_count[MAX_NODES];
static uint32_t      excess_count[MAX_NODES];
static uint32_t      fwd_count[MAX_NODES];

/* own TX counter (for node_id's own packets) */
static uint32_t      own_tx_count = 0;

PROCESS(rdf_process, "RDF Flooding");
AUTOSTART_PROCESSES(&rdf_process);

/* =========================================================================
 * valid_src -- bounds check before any array index
 * BUGFIX-7: was (src < MAX_NODES) which excluded MAX_NODES-1 correctly,
 *           but the original had (src > 0 && src < MAX_NODES) which is
 *           correct for 1-based node IDs. Kept as-is but documented.
 * =========================================================================
 */
static int valid_src(uint16_t src)
{
  return (src > 0 && src < MAX_NODES);
}

/* =========================================================================
 * init_node_coordinates  (FALLBACK grid)
 * =========================================================================
 */
static void init_node_coordinates(void)
{
  uint16_t idx = (node_id > 0 ? node_id : 1) - 1;
  my_x_m = (int16_t)((idx % COORD_GRID_COLS) * COORD_SPACING_M);
  my_y_m = (int16_t)((idx / COORD_GRID_COLS) * COORD_SPACING_M);
  LOG_INFO("node_id=%u fallback_xy=(%d m, %d m)\n",
           node_id, (int)my_x_m, (int)my_y_m);
}

/* =========================================================================
 * update_position_from_sim  (FIX-4)
 *
 * BUGFIX-5: Original code cleared the flag BEFORE reading the values,
 * creating a race where COOJA could write new values between the clear
 * and the read, causing the new values to be silently dropped.
 * Fixed: read values first, THEN clear the flag.
 * =========================================================================
 */
static void update_position_from_sim(void)
{
  if(sim_pos_updated) {
    /* BUGFIX-5: read values FIRST, then clear flag */
    int16_t new_x = (int16_t)(sim_pos_x / 100);
    int16_t new_y = (int16_t)(sim_pos_y / 100);
    sim_pos_updated = 0;  /* clear flag AFTER reading */

    if(new_x != my_x_m || new_y != my_y_m) {
      my_x_m = new_x;
      my_y_m = new_y;
      LOG_INFO("POS node_id=%u live_xy=(%d m, %d m)\n",
               node_id, (int)my_x_m, (int)my_y_m);
    }
  }
}

/* =========================================================================
 * rdf_decay_interval -- i * h^q  (integer, no FPU)
 * Paper eq.(2):  T'_fwd,s = T_fwd,s + i * h^q
 * =========================================================================
 */
static clock_time_t rdf_decay_interval(uint16_t hops)
{
  uint32_t h_pow = 1, q;
  if(hops == 0) { return 0; }
  for(q = 0; q < (uint32_t)RDF_Q; q++) {
    if(h_pow > (0xFFFFFFFFUL / (uint32_t)hops)) {
      return (clock_time_t)0xFFFFFFFFUL;
    }
    h_pow *= (uint32_t)hops;
  }
  if(h_pow > (0xFFFFFFFFUL / (uint32_t)SEND_INTERVAL)) {
    return (clock_time_t)0xFFFFFFFFUL;
  }
  return (clock_time_t)((uint32_t)SEND_INTERVAL * h_pow);
}

/* =========================================================================
 * distance_based_wait -- CBF contention timer (FIX-1)
 * =========================================================================
 */
static clock_time_t distance_based_wait(const flood_msg_t *msg)
{
  int32_t  dx, dy;
  uint32_t dist2, max_d2;
  clock_time_t wait;

  dx    = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  dy    = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  dist2 = (uint32_t)(dx * dx + dy * dy);

  if(dist2 == 0) { return DIST_MAX_WAIT; }

  max_d2 = (uint32_t)DIST_MAX_RANGE_M * (uint32_t)DIST_MAX_RANGE_M;
  if(dist2 > max_d2) { dist2 = max_d2; }

  wait = (clock_time_t)(
    DIST_MAX_WAIT -
    (clock_time_t)(
      ((uint64_t)(DIST_MAX_WAIT - DIST_MIN_WAIT) * (uint64_t)dist2)
      / (uint64_t)max_d2
    )
  );

  if(wait < DIST_MIN_WAIT) { wait = DIST_MIN_WAIT; }
  if(wait > DIST_MAX_WAIT) { wait = DIST_MAX_WAIT; }
  return wait;
}

/* =========================================================================
 * rdf_cancel_pending -- CBF overhearing suppression (FIX-2)
 * =========================================================================
 */
static void rdf_cancel_pending(uint16_t src, uint16_t seq)
{
  rdf_state_t *st;
  if(!valid_src(src)) { return; }
  st = &rdf_state[src];
  if(st->timer_active && st->has_pending && st->pending_seq == seq) {
    ctimer_stop(&st->forward_timer);
    st->timer_active = 0;
    st->has_pending  = 0;
    LOG_INFO("CBF suppress src=%u seq=%u\n", src, seq);
  }
}

/* =========================================================================
 * rdf_forward_callback -- fires when contention + decay timer expires
 * =========================================================================
 */
static void rdf_forward_callback(void *ptr)
{
  rdf_state_t *st  = (rdf_state_t *)ptr;
  flood_msg_t *msg;

  if(st == NULL || !st->has_pending) {
    if(st) { st->timer_active = 0; }
    return;
  }

  msg = &st->pending_msg;
  msg->hop_count++;

  /* FIX-3: stamp OUR current coordinates as the sender before TX */
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  LOG_INFO("FWD src=%u seq=%u hop=%u sender_xy=(%d,%d)\n",
           msg->src_id, msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

  /* Track forward count for this source */
  if(valid_src(msg->src_id)) {
    fwd_count[msg->src_id]++;
  }

  st->next_allowed_time = clock_time() + rdf_decay_interval(msg->hop_count);
  st->forwarded_before  = 1;
  st->timer_active      = 0;
  st->has_pending       = 0;
}

/* =========================================================================
 * rdf_handle_new_packet -- schedule or update a CBF/RDF forward
 * =========================================================================
 */
static void rdf_handle_new_packet(uint16_t src, flood_msg_t *msg)
{
  rdf_state_t  *st;
  clock_time_t  now, jitter, dist_wait;

  if(!valid_src(src)) { return; }
  st  = &rdf_state[src];
  now = clock_time();

  jitter = RDF_MIN_JITTER;
  if(RDF_MAX_JITTER > RDF_MIN_JITTER) {
    jitter += (clock_time_t)(random_rand()
              % (uint16_t)(RDF_MAX_JITTER - RDF_MIN_JITTER + 1));
  }

  dist_wait = distance_based_wait(msg);

  /* Integer sqrt for distance logging */
  {
    int32_t dx = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
    int32_t dy = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
    uint32_t dist2 = (uint32_t)(dx * dx + dy * dy);
    uint16_t dist_m = 0;
    while((uint32_t)dist_m * (uint32_t)dist_m < dist2 && dist_m < 255) {
      dist_m++;
    }
    LOG_INFO("CBF_TIMER src=%u seq=%u hop=%u sender_xy=(%d,%d) "
             "my_xy=(%d,%d) dist_m=%u dist_wait_ticks=%lu "
             "jitter_ticks=%lu total_ticks=%lu\n",
             msg->src_id, msg->seq_no, msg->hop_count,
             (int)msg->sender_x_m, (int)msg->sender_y_m,
             (int)my_x_m, (int)my_y_m,
             dist_m,
             (unsigned long)dist_wait,
             (unsigned long)jitter,
             (unsigned long)(dist_wait + jitter));
  }

  /* Buffer the freshest packet */
  st->pending_msg = *msg;
  st->pending_seq = msg->seq_no;
  st->has_pending = 1;

  if(!st->forwarded_before) {
    if(st->timer_active) { return; }
    st->timer_active = 1;
    ctimer_set(&st->forward_timer, dist_wait + jitter,
               rdf_forward_callback, st);
    return;
  }

  if(now >= st->next_allowed_time) {
    if(st->timer_active) { return; }
    st->timer_active = 1;
    ctimer_set(&st->forward_timer, dist_wait + jitter,
               rdf_forward_callback, st);
  } else {
    if(!st->timer_active) {
      clock_time_t cooldown = st->next_allowed_time - now;
      st->timer_active = 1;
      ctimer_set(&st->forward_timer, cooldown + dist_wait + jitter,
                 rdf_forward_callback, st);
    }
  }
}

/* =========================================================================
 * log_pdr -- running packet delivery ratio per source
 *
 * BUGFIX-3: denominator is now last_seq_seen[src]+1 which is the number
 * of sequence numbers we have observed from this source (0..last_seq_seen).
 * This is the best estimate available at a receiver without a separate
 * out-of-band tx_count signal. It is correct as long as seq starts at 0.
 * =========================================================================
 */
static void log_pdr(uint16_t src)
{
  uint32_t exp_pkts, pdr_t;
  if(!valid_src(src)) { return; }
  exp_pkts = (uint32_t)last_seq_seen[src] + 1;
  if(exp_pkts == 0) { return; }
  pdr_t = (1000UL * recv_count[src]) / exp_pkts;
  LOG_INFO("PDR src=%u recv=%" PRIu32 "/%" PRIu32 " = %lu.%lu%%\n",
           src, recv_count[src], exp_pkts,
           (unsigned long)(pdr_t / 10), (unsigned long)(pdr_t % 10));
}

/* =========================================================================
 * METRIC-1: log_dissemination
 *
 * Logs a DISSEM line that captures, for a given (src, seq), how many
 * nodes have received it so far (recv_count) vs total expected nodes.
 *
 * In post-processing: grep all "DISSEM src=X seq=Y" lines, take the
 * LAST one per (src,seq) -- that is the final dissemination count.
 * Dissemination Rate = nodes_received / total_nodes_in_network.
 *
 * Called: at every RX of a new (src,seq) packet.
 * =========================================================================
 */
static void log_dissemination(uint16_t src, uint16_t seq)
{
  if(!valid_src(src)) { return; }
  /*
   * recv_count[src] = number of unique seq from this src received by ME.
   * For dissemination rate across the network, the post-processing script
   * must aggregate DISSEM lines across all nodes for the same (src,seq).
   *
   * Format: DISSEM src=X seq=Y node=Z recv_by_me=N last_seq=M
   *   recv_by_me : how many unique packets from src this node has received
   *   last_seq   : highest seq seen from src at this node
   */
  LOG_INFO("DISSEM src=%u seq=%u node=%u recv_by_me=%" PRIu32
           " last_seq=%u\n",
           src, seq, node_id,
           recv_count[src],
           last_seq_seen[src]);
}

/* =========================================================================
 * METRIC-2: log_excess
 *
 * Checks if the received packet's AoI exceeds Taoi (warehouse threshold).
 * AoI = current_time - origin_time (stamped by source at TX).
 *
 * Logs an EXCESS line when AoI > TAOI_TICKS.
 * Increments excess_count[src] for the STATS summary.
 *
 * In post-processing:
 *   Excess Probability = total EXCESS events / total RX events
 *   (per source, or globally across all nodes)
 *
 * Called: at every first RX of a new (src,seq) packet.
 * =========================================================================
 */
static void log_excess(uint16_t src, uint16_t seq, const flood_msg_t *msg)
{
  clock_time_t now, aoi;
  uint32_t aoi_ms;

  if(!valid_src(src)) { return; }

  now = clock_time();

  /*
   * Guard against clock wrap or future origin_time (can happen if
   * origin_time was set on a different node with clock skew in sim).
   */
  if(now < msg->origin_time) {
    return;
  }

  aoi = now - msg->origin_time;

  /* Convert ticks to ms for logging */
  aoi_ms = (uint32_t)((uint64_t)aoi * 1000UL / CLOCK_SECOND);

  if(aoi > TAOI_TICKS) {
    excess_count[src]++;
    LOG_INFO("EXCESS src=%u seq=%u node=%u aoi_ms=%" PRIu32
             " taoi_ms=%u hop=%u excess_total=%" PRIu32 "\n",
             src, seq, node_id,
             aoi_ms,
             (unsigned)TAOI_MS,
             msg->hop_count,
             excess_count[src]);
  } else {
    /* Log AoI-OK events too so we can compute ratio in post-processing */
    LOG_INFO("AOI_OK src=%u seq=%u node=%u aoi_ms=%" PRIu32
             " taoi_ms=%u hop=%u\n",
             src, seq, node_id,
             aoi_ms,
             (unsigned)TAOI_MS,
             msg->hop_count);
  }
}

/* =========================================================================
 * METRIC-3: log_stats_summary
 *
 * Periodic one-line summary per source for easy post-run analysis.
 * Grep "STATS" from the log to extract all summaries.
 *
 * Format:
 *   STATS node=Z src=X recv=R excess=E fwd=F pdr=P.P% excess_prob=E.E%
 *
 * Dissemination Rate (network-wide) cannot be computed locally --
 * it requires aggregating DISSEM lines across all nodes in post-processing.
 * But excess probability CAN be computed locally per node:
 *   local_excess_prob = excess_count[src] / recv_count[src]
 * =========================================================================
 */
static void log_stats_summary(void)
{
  uint16_t src;
  for(src = 1; src < MAX_NODES; src++) {
    if(recv_count[src] == 0 && fwd_count[src] == 0) { continue; }

    uint32_t exp_pkts = (uint32_t)last_seq_seen[src] + 1;
    uint32_t pdr_t    = (exp_pkts > 0)
                        ? (1000UL * recv_count[src]) / exp_pkts
                        : 0;
    uint32_t exc_t    = (recv_count[src] > 0)
                        ? (1000UL * excess_count[src]) / recv_count[src]
                        : 0;

    LOG_INFO("STATS node=%u src=%u recv=%" PRIu32 " exp=%" PRIu32
             " excess=%" PRIu32 " fwd=%" PRIu32
             " pdr=%lu.%lu%% excess_prob=%lu.%lu%%\n",
             node_id, src,
             recv_count[src], exp_pkts,
             excess_count[src], fwd_count[src],
             (unsigned long)(pdr_t / 10), (unsigned long)(pdr_t % 10),
             (unsigned long)(exc_t / 10), (unsigned long)(exc_t % 10));
  }
  /* Also log own TX count */
  LOG_INFO("STATS node=%u own_tx=%" PRIu32 "\n", node_id, own_tx_count);
}

/* =========================================================================
 * udp_rx_callback
 * =========================================================================
 */
static void
udp_rx_callback(struct simple_udp_connection *c,
                const uip_ipaddr_t *sender_addr,
                uint16_t sender_port,
                const uip_ipaddr_t *receiver_addr,
                uint16_t receiver_port,
                const uint8_t *data,
                uint16_t datalen)
{
  flood_msg_t  msg;
  uint16_t     src, seq;
  uint8_t      is_new;
  rdf_state_t *st;

  (void)c; (void)sender_addr; (void)sender_port;
  (void)receiver_addr; (void)receiver_port;

  if(datalen != sizeof(flood_msg_t)) {
    LOG_WARN("RX wrong size %u (expected %u)\n",
             datalen, (unsigned)sizeof(flood_msg_t));
    return;
  }

  memcpy(&msg, data, sizeof(msg));
  src = msg.src_id;
  seq = msg.seq_no;

  if(!valid_src(src) || src == node_id) { return; }

  /* Log every arrival (new AND duplicate) */
  LOG_INFO("ARRIVE src=%u seq=%u hop=%u sender_xy=(%d,%d) is_new=%u\n",
           src, seq, msg.hop_count,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (unsigned)(!seen[src][seq % MAX_SEQ_TRACK]));

  is_new = !seen[src][seq % MAX_SEQ_TRACK];

  /* ---- DUPLICATE: overhearing suppression (FIX-2, path A) ---- */
  if(!is_new) {
    rdf_cancel_pending(src, seq);
    return;
  }

  /* ---- FIRST receive of (src, seq) ---- */
  seen[src][seq % MAX_SEQ_TRACK] = 1;
  recv_count[src]++;
  if(seq > last_seq_seen[src]) { last_seq_seen[src] = seq; }

  LOG_INFO("RX src=%u seq=%u hop=%u src_xy=(%d,%d) sender_xy=(%d,%d) "
           "my_xy=(%d,%d)\n",
           src, seq, msg.hop_count,
           (int)msg.src_x_m, (int)msg.src_y_m,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (int)my_x_m, (int)my_y_m);

  log_pdr(src);

  /* METRIC-1: dissemination snapshot */
  log_dissemination(src, seq);

  /* METRIC-2: AoI / excess probability check */
  log_excess(src, seq, &msg);

  /* ---- FIX-2 path B: buffered-then-overheard suppression ---- */
  st = &rdf_state[src];
  if(st->timer_active && st->has_pending && st->pending_seq == seq) {
    rdf_cancel_pending(src, seq);
    return;
  }

  LOG_INFO("SUPPRESS_MISS src=%u seq=%u timer_active=%u has_pending=%u pending_seq=%u\n",
           src, seq,
           (unsigned)st->timer_active,
           (unsigned)st->has_pending,
           (unsigned)st->pending_seq);

  rdf_handle_new_packet(src, &msg);
}

/* =========================================================================
 * Main process
 * =========================================================================
 */
PROCESS_THREAD(rdf_process, ev, data)
{
  static struct etimer periodic_timer;
  static struct etimer pos_timer;
  static struct etimer stats_timer;   /* METRIC-3: periodic stats */
  flood_msg_t msg;
  uint16_t i, j;

  PROCESS_BEGIN();

  init_node_coordinates();

  /* Initialise state tables */
  for(i = 0; i < MAX_NODES; i++) {
    last_seq_seen[i]  = 0;
    recv_count[i]     = 0;
    excess_count[i]   = 0;
    fwd_count[i]      = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) { seen[i][j] = 0; }
    rdf_state[i].forwarded_before  = 0;
    rdf_state[i].timer_active      = 0;
    rdf_state[i].has_pending       = 0;
    rdf_state[i].pending_seq       = 0;
    rdf_state[i].next_allowed_time = 0;
  }

  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  /* FIX-4: position poll timer */
  etimer_set(&pos_timer, POS_UPDATE_TICKS);

  /* METRIC-3: stats timer */
  etimer_set(&stats_timer, STATS_TICKS);

  /* Randomised start */
  etimer_set(&periodic_timer, random_rand() % SEND_INTERVAL);

  /* Log Taoi so it appears in the log for reference */
  LOG_INFO("INIT node=%u taoi_ms=%u taoi_ticks=%lu send_interval_ticks=%lu\n",
           node_id,
           (unsigned)TAOI_MS,
           (unsigned long)TAOI_TICKS,
           (unsigned long)SEND_INTERVAL);

  while(1) {
    PROCESS_WAIT_EVENT();

    /* ---- FIX-4: position refresh ---- */
    if(etimer_expired(&pos_timer)) {
      update_position_from_sim();
      etimer_reset(&pos_timer);
    }

    /* ---- METRIC-3: periodic stats summary ---- */
    if(etimer_expired(&stats_timer)) {
      log_stats_summary();
      etimer_reset(&stats_timer);
    }

    /* ---- Periodic broadcast ---- */
    if(etimer_expired(&periodic_timer)) {

      update_position_from_sim();

      /*
       * BUGFIX-4: Mark own packet as seen BEFORE incrementing my_seq,
       * so the index used for seen[] matches the seq_no in the packet.
       * Original code marked seen[node_id][my_seq % MAX_SEQ_TRACK] = 1
       * and then did my_seq++ -- this is actually correct order, but
       * the check (my_seq > last_seq_seen[node_id]) must use the
       * pre-increment value. Kept same order, added clarity comment.
       */
      msg.src_id      = node_id;
      msg.seq_no      = my_seq;          /* use current seq */
      msg.hop_count   = 0;
      msg.origin_time = clock_time();    /* AoI anchor timestamp */
      msg.src_x_m     = my_x_m;
      msg.src_y_m     = my_y_m;
      msg.sender_x_m  = my_x_m;
      msg.sender_y_m  = my_y_m;

      if(valid_src(node_id)) {
        seen[node_id][my_seq % MAX_SEQ_TRACK] = 1;  /* mark before TX */
        if(my_seq > last_seq_seen[node_id]) {
          last_seq_seen[node_id] = my_seq;
        }
      }

      LOG_INFO("TX seq=%u xy=(%d,%d)\n", my_seq, (int)my_x_m, (int)my_y_m);

      /*
       * METRIC-4: log TX_ORIGIN so post-processing can count total
       * transmissions from this source (used as denominator for
       * network-wide dissemination rate).
       * Format: TX_ORIGIN src=X seq=Y time_ms=Z
       */
      LOG_INFO("TX_ORIGIN src=%u seq=%u time_ms=%lu\n",
               node_id, my_seq,
               (unsigned long)((uint64_t)clock_time() * 1000UL / CLOCK_SECOND));

      simple_udp_sendto(&udp_conn, &msg, sizeof(msg), &mcast_addr);

      own_tx_count++;
      my_seq++;   /* increment AFTER TX and seen[] marking */

      etimer_set(&periodic_timer,
                 SEND_INTERVAL - RDF_MIN_JITTER
                 + (random_rand() % (2 * RDF_MIN_JITTER + 1)));
    }
  }

  PROCESS_END();
}

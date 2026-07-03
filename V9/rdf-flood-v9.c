/*
 * rdf-flood-v7.c -- Rate Decay Flooding (RDF) for Contiki-NG / COOJA
 *
 * Changes from v6:
 *  - SEND_INTERVAL changed from 500ms to 1000ms (CLOCK_SECOND / 1).
 *  - DIST_MAX_RANGE_M changed from 1m to 50m so CBF distance-based wait
 *    actually varies between nodes and provides real spatial selection.
 *  - ALL log lines now include src=X seq=Y so filtering by "src=X seq=Y"
 *    in the log viewer shows every event related to that packet:
 *    ARRIVE, RX, CBF_TIMER, CBF_DONE, CBF_SUPPRESS, CBF_HOP_IGNORE,
 *    RDF_SUPPRESS, RDF_HOP_IGNORE, PENDING_UPDATE, PENDING_DROP_STALE,
 *    FWD, PDR, DISSEM all carry src= and seq= fields.
 *  - Analysis tools (Python + HTML) unchanged — all existing fields kept.
 */

#ifndef ROUTING_CONF_RPL_LITE
#define ROUTING_CONF_RPL_LITE 0
#endif
#ifndef ROUTING_CONF_RPL_CLASSIC
#define ROUTING_CONF_RPL_CLASSIC 0
#endif

/* =========================================================================
 * PARAMETERS
 * =========================================================================
 */
#ifndef SEND_INTERVAL
#define SEND_INTERVAL (CLOCK_SECOND / 1)   /* 1000 ms */
#endif
#ifndef RDF_Q
#define RDF_Q 2
#endif
#ifndef RDF_MIN_JITTER
#define RDF_MIN_JITTER (CLOCK_SECOND / 500) /* ~2 ms */
#endif
#ifndef RDF_MAX_JITTER
#define RDF_MAX_JITTER (CLOCK_SECOND / 200) /* ~5 ms */
#endif
#ifndef DIST_MIN_WAIT_MS
#define DIST_MIN_WAIT_MS 1
#endif
#ifndef DIST_MAX_WAIT_MS
#define DIST_MAX_WAIT_MS 2000
#endif
#ifndef DIST_MAX_RANGE_M
#define DIST_MAX_RANGE_M 50    /* actual node spacing — enables real CBF */
#endif
#ifndef COORD_GRID_COLS
#define COORD_GRID_COLS 8
#endif
#ifndef COORD_SPACING_M
#define COORD_SPACING_M 50
#endif
#ifndef MAX_NODES
#define MAX_NODES 51
#endif
#ifndef MAX_SEQ_TRACK
#define MAX_SEQ_TRACK 256   /* MUST be a power of 2 */
#endif
#ifndef POS_UPDATE_INTERVAL_MS
#define POS_UPDATE_INTERVAL_MS 125
#endif
#ifndef STATS_INTERVAL_S
#define STATS_INTERVAL_S 10
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
#include <stdint.h>
#include <string.h>
#include <inttypes.h>

#define LOG_MODULE "RDF"
#define LOG_LEVEL  LOG_LEVEL_INFO
#define UDP_PORT   1234

#define DIST_MIN_WAIT    ((clock_time_t)((DIST_MIN_WAIT_MS  * CLOCK_SECOND) / 1000))
#define DIST_MAX_WAIT    ((clock_time_t)((DIST_MAX_WAIT_MS  * CLOCK_SECOND) / 1000))
#define POS_UPDATE_TICKS ((clock_time_t)((POS_UPDATE_INTERVAL_MS * CLOCK_SECOND) / 1000))
#define STATS_TICKS      ((clock_time_t)(STATS_INTERVAL_S * CLOCK_SECOND))

/* =========================================================================
 * COOJA LIVE POSITION SHARED VARIABLES
 * =========================================================================
 */
volatile int32_t sim_pos_x       = 0;
volatile int32_t sim_pos_y       = 0;
volatile uint8_t sim_pos_updated = 0;

/* =========================================================================
 * Packet format
 * AoI computed offline: AoI(src,seq,node) = t_RX_log - t_TX_log
 * =========================================================================
 */
typedef struct {
  uint16_t src_id;
  uint16_t seq_no;
  uint16_t hop_count;
  int16_t  src_x_m;
  int16_t  src_y_m;
  int16_t  sender_x_m;
  int16_t  sender_y_m;
} flood_msg_t;

/* =========================================================================
 * Per-source RDF state — two-phase (CBF then RDF)
 *
 * phase: 0=idle, 1=CBF wait, 2=RDF wait
 * =========================================================================
 */
typedef struct {
  uint8_t       phase;
  uint8_t       has_pending;
  uint16_t      pending_seq;
  flood_msg_t   pending_msg;
  struct ctimer cbf_timer;
  struct ctimer rdf_timer;
} rdf_state_t;

/* =========================================================================
 * Globals
 * =========================================================================
 */
static struct simple_udp_connection udp_conn;
static uip_ipaddr_t mcast_addr;
static uint16_t my_seq = 0;
static int16_t  my_x_m = 0;
static int16_t  my_y_m = 0;

static uint16_t    last_seq_seen[MAX_NODES];
static uint8_t     seen[MAX_NODES][MAX_SEQ_TRACK];
static rdf_state_t rdf_state[MAX_NODES];
static uint32_t    recv_count[MAX_NODES];
static uint32_t    fwd_count[MAX_NODES];
static uint32_t    own_tx_count = 0;

PROCESS(rdf_process, "RDF Flooding");
AUTOSTART_PROCESSES(&rdf_process);

/* =========================================================================
 * valid_src
 * =========================================================================
 */
static int valid_src(uint16_t src)
{
  return (src > 0 && src < MAX_NODES);
}

/* =========================================================================
 * init_node_coordinates (fallback grid)
 * =========================================================================
 */
static void init_node_coordinates(void)
{
  uint16_t idx = (node_id > 0 ? node_id : 1) - 1;
  my_x_m = (int16_t)((idx % COORD_GRID_COLS) * COORD_SPACING_M);
  my_y_m = (int16_t)((idx / COORD_GRID_COLS) * COORD_SPACING_M);
  LOG_INFO("INIT node=%u fallback_xy=(%d,%d)\n",
           node_id, (int)my_x_m, (int)my_y_m);
}

/* =========================================================================
 * update_position_from_sim
 * =========================================================================
 */
static void update_position_from_sim(void)
{
  if(sim_pos_updated) {
    int16_t new_x = (int16_t)(sim_pos_x / 100);
    int16_t new_y = (int16_t)(sim_pos_y / 100);
    sim_pos_updated = 0;
    if(new_x != my_x_m || new_y != my_y_m) {
      my_x_m = new_x;
      my_y_m = new_y;
      LOG_INFO("POS node=%u live_xy=(%d,%d)\n",
               node_id, (int)my_x_m, (int)my_y_m);
    }
  }
}

/* =========================================================================
 * rdf_decay_interval
 * Returns SEND_INTERVAL * hop^Q (overflow-safe)
 * For hop=0: returns 0 (no RDF wait — CBF only)
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
 * distance_based_wait
 * Nodes far from sender wait less; nodes close to sender wait more.
 * With DIST_MAX_RANGE_M=50m, nodes at 50m get DIST_MIN_WAIT (~5ms)
 * and nodes at 0m get DIST_MAX_WAIT (1000ms).
 * =========================================================================
 */
static clock_time_t distance_based_wait(const flood_msg_t *msg)
{
  int32_t      dx    = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  int32_t      dy    = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  uint32_t     dist2 = (uint32_t)(dx * dx + dy * dy);
  uint32_t     max_d2 = (uint32_t)DIST_MAX_RANGE_M * (uint32_t)DIST_MAX_RANGE_M;
  clock_time_t wait;

  if(dist2 == 0) { return DIST_MAX_WAIT; }
  if(dist2 > max_d2) { dist2 = max_d2; }

  wait = (clock_time_t)(DIST_MAX_WAIT -
         (clock_time_t)(((uint64_t)(DIST_MAX_WAIT - DIST_MIN_WAIT) *
                          (uint64_t)dist2) / (uint64_t)max_d2));

  if(wait < DIST_MIN_WAIT) { wait = DIST_MIN_WAIT; }
  if(wait > DIST_MAX_WAIT) { wait = DIST_MAX_WAIT; }
  return wait;
}

/* =========================================================================
 * cancel_all_timers
 * =========================================================================
 */
static void cancel_all_timers(rdf_state_t *st)
{
  ctimer_stop(&st->cbf_timer);
  ctimer_stop(&st->rdf_timer);
  st->phase       = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase2_callback  (Phase 2 — RDF wait complete → TX)
 * =========================================================================
 */
static void rdf_phase2_callback(void *ptr)
{
  rdf_state_t *st  = (rdf_state_t *)ptr;
  flood_msg_t *msg;

  if(st == NULL || !st->has_pending || st->phase != 2) {
    if(st) { st->phase = 0; st->has_pending = 0; }
    return;
  }

  msg = &st->pending_msg;
  msg->hop_count++;
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  /* FWD — src= and seq= included for easy log filtering */
  LOG_INFO("FWD src=%u seq=%u hop=%u sender_xy=(%d,%d)\n",
           msg->src_id, msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

  if(valid_src(msg->src_id)) { fwd_count[msg->src_id]++; }

  st->phase       = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase1_callback  (Phase 1 — CBF wait complete → start Phase 2)
 * =========================================================================
 */
static void rdf_phase1_callback(void *ptr)
{
  rdf_state_t  *st = (rdf_state_t *)ptr;
  clock_time_t  rdf_wait;

  if(st == NULL || !st->has_pending || st->phase != 1) {
    if(st) { st->phase = 0; st->has_pending = 0; }
    return;
  }

  rdf_wait = rdf_decay_interval(st->pending_msg.hop_count);

  /* CBF_DONE — src= and seq= included */
  LOG_INFO("CBF_DONE src=%u seq=%u hop=%u rdf_wait_ticks=%lu\n",
           st->pending_msg.src_id, st->pending_msg.seq_no,
           st->pending_msg.hop_count,
           (unsigned long)rdf_wait);

  if(rdf_wait == 0) {
    st->phase = 2;
    rdf_phase2_callback(st);
    return;
  }

  st->phase = 2;
  ctimer_set(&st->rdf_timer, rdf_wait, rdf_phase2_callback, st);
}

/* =========================================================================
 * suppress_check
 *
 * Unified suppression logic for both phases.
 * All log lines include src= and seq= for easy filtering.
 * =========================================================================
 */
static int suppress_check(rdf_state_t *st, uint16_t src, uint16_t seq,
                           uint16_t arriving_hop, const char *phase_label)
{
  if(arriving_hop > st->pending_msg.hop_count) {
    cancel_all_timers(st);
    if(phase_label[0] == 'c') {
      LOG_INFO("CBF_SUPPRESS src=%u seq=%u"
               " arriving_hop=%u pending_hop=%u\n",
               src, seq,
               (unsigned)arriving_hop,
               (unsigned)st->pending_msg.hop_count);
    } else {
      LOG_INFO("RDF_SUPPRESS src=%u seq=%u"
               " arriving_hop=%u pending_hop=%u period=%s\n",
               src, seq,
               (unsigned)arriving_hop,
               (unsigned)st->pending_msg.hop_count,
               phase_label);
    }
    return 1;
  }

  if(phase_label[0] == 'c') {
    LOG_INFO("CBF_HOP_IGNORE src=%u seq=%u"
             " arriving_hop=%u pending_hop=%u\n",
             src, seq,
             (unsigned)arriving_hop,
             (unsigned)st->pending_msg.hop_count);
  } else {
    LOG_INFO("RDF_HOP_IGNORE src=%u seq=%u"
             " arriving_hop=%u pending_hop=%u period=%s\n",
             src, seq,
             (unsigned)arriving_hop,
             (unsigned)st->pending_msg.hop_count,
             phase_label);
  }
  return 0;
}

/* =========================================================================
 * handle_arrival_during_wait
 *
 * All log lines include src= and seq= for easy filtering.
 * PENDING_UPDATE uses new_seq= as the seq field (the packet being buffered).
 * PENDING_DROP_STALE uses stale_seq= as the seq field.
 * =========================================================================
 */
static int handle_arrival_during_wait(rdf_state_t *st, uint16_t src,
                                      flood_msg_t *msg, const char *phase_label)
{
  uint16_t seq = msg->seq_no;

  if(seq == st->pending_seq) {
    return suppress_check(st, src, seq, msg->hop_count, phase_label);
  }

  if(seq > st->pending_seq) {
    /* PENDING_UPDATE — include both old_seq and new_seq=seq for filtering */
    LOG_INFO("PENDING_UPDATE src=%u seq=%u old_seq=%u new_seq=%u"
             " old_hop=%u new_hop=%u timer_kept=1 period=%s\n",
             src, seq,
             st->pending_seq, seq,
             (unsigned)st->pending_msg.hop_count,
             (unsigned)msg->hop_count,
             phase_label);
    st->pending_msg = *msg;
    st->pending_seq = seq;
    return 0;
  }

  /* PENDING_DROP_STALE — include seq=stale_seq for filtering */
  LOG_INFO("PENDING_DROP_STALE src=%u seq=%u buffered_seq=%u stale_seq=%u"
           " period=%s\n",
           src, seq,
           st->pending_seq, seq, phase_label);
  return 0;
}

/* =========================================================================
 * start_cbf_phase
 * All log lines include src= and seq=.
 * =========================================================================
 */
static void start_cbf_phase(uint16_t src, flood_msg_t *msg)
{
  rdf_state_t  *st;
  clock_time_t  dist_wait, jitter;
  int32_t       dx, dy;
  uint32_t      dist2;
  uint16_t      dist_m;

  if(!valid_src(src)) { return; }
  st = &rdf_state[src];

  jitter = RDF_MIN_JITTER;
  if(RDF_MAX_JITTER > RDF_MIN_JITTER) {
    jitter += (clock_time_t)(random_rand()
               % (uint16_t)(RDF_MAX_JITTER - RDF_MIN_JITTER + 1));
  }

  dist_wait = distance_based_wait(msg);

  dx    = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  dy    = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  dist2 = (uint32_t)(dx * dx + dy * dy);
  for(dist_m = 0;
      (uint32_t)dist_m * (uint32_t)dist_m < dist2 && dist_m < 255;
      dist_m++) {}

  /* CBF_TIMER — src= and seq= included */
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

  st->pending_msg = *msg;
  st->pending_seq = msg->seq_no;
  st->has_pending = 1;
  st->phase       = 1;
  ctimer_set(&st->cbf_timer, dist_wait + jitter, rdf_phase1_callback, st);
}


/* =========================================================================
 * log_dissemination — already has src= and seq=
 * =========================================================================
 */
static void log_dissemination(uint16_t src, uint16_t seq)
{
  if(!valid_src(src)) { return; }
  LOG_INFO("DISSEM src=%u seq=%u node=%u recv_by_me=%" PRIu32
           " last_seq=%u\n",
           src, seq, node_id, recv_count[src], last_seq_seen[src]);
}

/* =========================================================================
 * log_stats_summary — STATS lines are per-source summaries, not per-packet.
 * They already have src= but no seq= (not applicable for summaries).
 * =========================================================================
 */
static void log_stats_summary(void)
{
  uint16_t src;
  for(src = 1; src < MAX_NODES; src++) {
    if(recv_count[src] == 0 && fwd_count[src] == 0) { continue; }
    LOG_INFO("STATS node=%u src=%u recv=%" PRIu32 " exp=%" PRIu32
             " fwd=%" PRIu32 "\n",
             node_id, src,
             recv_count[src],
             (uint32_t)last_seq_seen[src] + 1,
             fwd_count[src]);
  }
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
  const char  *phase_label;

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

  is_new = !seen[src][seq % MAX_SEQ_TRACK];

  /* ARRIVE — src= and seq= already present */
  LOG_INFO("ARRIVE src=%u seq=%u hop=%u sender_xy=(%d,%d) is_new=%u\n",
           src, seq, msg.hop_count,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (unsigned)is_new);

  st = &rdf_state[src];
  phase_label = (st->phase == 1) ? "cbf" : "rdf_decay";

  /* ------------------------------------------------------------------ */
  /* DUPLICATE PATH (is_new=0)                                           */
  /* ------------------------------------------------------------------ */
  if(!is_new) {
    if(st->has_pending) {
      handle_arrival_during_wait(st, src, &msg, phase_label);
    }
    return;
  }

  /* ------------------------------------------------------------------ */
  /* FIRST RECEIVE of (src, seq)                                         */
  /* ------------------------------------------------------------------ */
  seen[src][seq % MAX_SEQ_TRACK] = 1;
  recv_count[src]++;
  if(seq > last_seq_seen[src]) { last_seq_seen[src] = seq; }

  /* RX — src= and seq= already present */
  LOG_INFO("RX src=%u seq=%u hop=%u src_xy=(%d,%d) sender_xy=(%d,%d) "
           "my_xy=(%d,%d)\n",
           src, seq, msg.hop_count,
           (int)msg.src_x_m, (int)msg.src_y_m,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (int)my_x_m, (int)my_y_m);

  log_dissemination(src, seq);

  /* ------------------------------------------------------------------ */
  /* RELAY DECISION                                                       */
  /* ------------------------------------------------------------------ */
  if(st->has_pending) {
    handle_arrival_during_wait(st, src, &msg, phase_label);
    return;
  }

  start_cbf_phase(src, &msg);
}

/* =========================================================================
 * Main process
 * =========================================================================
 */
PROCESS_THREAD(rdf_process, ev, data)
{
  static struct etimer periodic_timer;
  static struct etimer pos_timer;
  static struct etimer stats_timer;
  flood_msg_t   msg;
  uint16_t      i, j;

  PROCESS_BEGIN();

  init_node_coordinates();

  for(i = 0; i < MAX_NODES; i++) {
    last_seq_seen[i]          = 0;
    recv_count[i]             = 0;
    fwd_count[i]              = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) { seen[i][j] = 0; }
    rdf_state[i].phase        = 0;
    rdf_state[i].has_pending  = 0;
    rdf_state[i].pending_seq  = 0;
  }

  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  etimer_set(&pos_timer,      POS_UPDATE_TICKS);
  etimer_set(&stats_timer,    STATS_TICKS);
  etimer_set(&periodic_timer, random_rand() % SEND_INTERVAL);

  LOG_INFO("START node=%u send_interval_ticks=%lu\n",
           node_id, (unsigned long)SEND_INTERVAL);

  while(1) {
    PROCESS_WAIT_EVENT();

    if(etimer_expired(&pos_timer)) {
      update_position_from_sim();
      etimer_reset(&pos_timer);
    }

    if(etimer_expired(&stats_timer)) {
      log_stats_summary();
      etimer_reset(&stats_timer);
    }

    /* Originator TX — no CBF/RDF delay, immediate send at hop=0 */
    if(etimer_expired(&periodic_timer)) {
      update_position_from_sim();

      msg.src_id     = node_id;
      msg.seq_no     = my_seq;
      msg.hop_count  = 0;
      msg.src_x_m    = my_x_m;
      msg.src_y_m    = my_y_m;
      msg.sender_x_m = my_x_m;
      msg.sender_y_m = my_y_m;

      if(valid_src(node_id)) {
        seen[node_id][my_seq % MAX_SEQ_TRACK] = 1;
        if(my_seq > last_seq_seen[node_id]) {
          last_seq_seen[node_id] = my_seq;
        }
      }

      /* TX — src= and seq= included for filtering */
      LOG_INFO("TX src=%u seq=%u xy=(%d,%d)\n",
               node_id, my_seq, (int)my_x_m, (int)my_y_m);

      simple_udp_sendto(&udp_conn, &msg, sizeof(msg), &mcast_addr);
      own_tx_count++;
      my_seq++;

      etimer_set(&periodic_timer,
                 SEND_INTERVAL - RDF_MIN_JITTER
                 + (random_rand() % (2 * RDF_MIN_JITTER + 1)));
    }
  }

  PROCESS_END();
}

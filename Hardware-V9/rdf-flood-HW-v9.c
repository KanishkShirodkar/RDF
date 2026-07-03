/*
 * rdf-flood-HW-v7.c -- Rate Decay Flooding (RDF) for FIT IoT-LAB M3 hardware
 *
 * Based directly on rdf-flood-v7.c (Contiki-NG / COOJA). Core RDF logic,
 * two-phase CBF->RDF forwarding, suppression rules, and all log formats
 * are UNCHANGED. Only hardware-specific pieces are modified:
 *   - valid_src() no longer assumes small sequential COOJA IDs
 *   - HW_SLOT() hashes the large MAC-derived node_id into array bounds
 *   - slot_to_srcid[] maps slot -> real node_id for correct STATS logging
 *   - safe_hw_slot() detects and logs HW_SLOT() hash collisions
 *   - COOJA live-position feed (sim_pos_x/y) removed (not available on HW)
 *   - init_node_coordinates() uses HW_SLOT() instead of node_id-1
 *   - Timing macros (DIST_MIN_WAIT, DIST_MAX_WAIT, RDF_MIN/MAX_JITTER)
 *     are set as direct tick values for CLOCK_SECOND=100 (IoT-LAB M3 100Hz)
 *     to avoid integer truncation to 0 that breaks CBF distance-wait
 *   - PDR removed from STATS log lines
 *
 * AoI is still computed OFFLINE from TX/RX log timestamps -- origin_time
 * is intentionally NOT part of the packet, matching v7 exactly.
 *
 * Build:
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3 clean
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3
 *
 * Flash:
 *   iotlab-node --flash build/iotlab/m3/rdf-flood-HW-v7.iotlab \
 *     -l toulouse,m3,<n1>+<n2>+<n3>
 *
 * Monitor:
 *   serial_aggregator | tee ~/rdf_run.log
 */

#ifndef ROUTING_CONF_RPL_LITE
#define ROUTING_CONF_RPL_LITE    0
#endif
#ifndef ROUTING_CONF_RPL_CLASSIC
#define ROUTING_CONF_RPL_CLASSIC 0
#endif

/* =========================================================================
 * PARAMETERS
 * =========================================================================
 */
#ifndef SEND_INTERVAL
#define SEND_INTERVAL        (CLOCK_SECOND / 1)   /* 1000 ms */
#endif
#ifndef RDF_Q
#define RDF_Q                2
#endif
#ifndef DIST_MAX_RANGE_M
#define DIST_MAX_RANGE_M     50
#endif
#ifndef COORD_GRID_COLS
#define COORD_GRID_COLS      8
#endif
#ifndef COORD_SPACING_M
#define COORD_SPACING_M      50
#endif
#ifndef MAX_NODES
#define MAX_NODES            51
#endif
#ifndef MAX_SEQ_TRACK
#define MAX_SEQ_TRACK        256  /* MUST be a power of 2 */
#endif
#ifndef STATS_INTERVAL_S
#define STATS_INTERVAL_S     10
#endif

/* =========================================================================
 * TIMING -- fixed tick values for IoT-LAB M3 (CLOCK_SECOND = 100 Hz)
 *
 * The ms-based formula  (Xms * CLOCK_SECOND / 1000)  truncates to 0 when
 * CLOCK_SECOND=100 and X < 10ms, which kills CBF distance-wait entirely.
 * Use direct tick counts instead:
 *   2 ticks  =  20 ms  at 100 Hz
 *   5 ticks  =  50 ms  at 100 Hz
 *   200 ticks = 2000 ms at 100 Hz
 * =========================================================================
 */
#ifndef DIST_MIN_WAIT
#define DIST_MIN_WAIT        ((clock_time_t)2)    /*  20 ms at 100 Hz */
#endif
#ifndef DIST_MAX_WAIT
#define DIST_MAX_WAIT        ((clock_time_t)200)  /* 2000 ms at 100 Hz */
#endif
#ifndef RDF_MIN_JITTER
#define RDF_MIN_JITTER       ((clock_time_t)2)    /*  20 ms at 100 Hz */
#endif
#ifndef RDF_MAX_JITTER
#define RDF_MAX_JITTER       ((clock_time_t)5)    /*  50 ms at 100 Hz */
#endif

#define STATS_TICKS          ((clock_time_t)(STATS_INTERVAL_S * CLOCK_SECOND))

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
#include "lib/random.h"
#include <stdint.h>
#include <inttypes.h>
#include <string.h>

#define LOG_MODULE "RDF"
#define LOG_LEVEL  LOG_LEVEL_INFO
#define UDP_PORT   1234

/* Hardware node_id is MAC-derived and large; hash into array bounds. */
#define HW_SLOT(id)  ((uint16_t)((id) % MAX_NODES))

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
 * Per-source RDF state -- two-phase (CBF then RDF)
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
static uip_ipaddr_t  mcast_addr;
static uint16_t      my_seq  = 0;
static int16_t       my_x_m  = 0;
static int16_t       my_y_m  = 0;

static uint16_t      last_seq_seen[MAX_NODES];
static uint8_t       seen[MAX_NODES][MAX_SEQ_TRACK];
static rdf_state_t   rdf_state[MAX_NODES];
static uint32_t      recv_count[MAX_NODES];
static uint32_t      fwd_count[MAX_NODES];
static uint32_t      own_tx_count = 0;

/* Maps slot index -> real src node_id for correct STATS logging.
 * 0 means unoccupied. Populated on first packet from each source. */
static uint16_t      slot_to_srcid[MAX_NODES];

PROCESS(rdf_process, "RDF Flooding");
AUTOSTART_PROCESSES(&rdf_process);

/* =========================================================================
 * valid_src
 * =========================================================================
 */
static int
valid_src(uint16_t src)
{
  return src != 0;
}

/* =========================================================================
 * safe_hw_slot
 *
 * Returns the slot for src_id, registering it on first use.
 *
 * Uses linear probing to resolve hash collisions: if the natural slot
 * (src_id % MAX_NODES) is already owned by a different src_id, we walk
 * forward through the table until we find either the src_id's own slot
 * or a free slot, and assign it there.
 *
 * This guarantees every real node in the network gets its own unique slot
 * -- no node is ever silently dropped due to a collision.
 *
 * Returns 0xFFFF only if the entire table is full (all MAX_NODES slots
 * are occupied by different src_ids) -- extremely unlikely in practice.
 * =========================================================================
 */
static uint16_t
safe_hw_slot(uint16_t src_id)
{
  uint16_t slot = HW_SLOT(src_id);
  uint16_t probe;

  for(probe = 0; probe < MAX_NODES; probe++) {
    uint16_t candidate = (slot + probe) % MAX_NODES;

    if(slot_to_srcid[candidate] == 0) {
      /* Free slot -- register this src_id here */
      if(probe > 0) {
        LOG_INFO("HW_SLOT probe: src=%u natural_slot=%u assigned_slot=%u\n",
                 src_id, slot, candidate);
      }
      slot_to_srcid[candidate] = src_id;
      return candidate;
    }

    if(slot_to_srcid[candidate] == src_id) {
      /* Already registered -- return existing slot */
      return candidate;
    }

    /* This slot is taken by someone else -- keep probing */
  }

  /* Table completely full -- should never happen with MAX_NODES=51
   * and a typical IoT-LAB deployment of far fewer nodes             */
  LOG_WARN("HW_SLOT table full: cannot assign slot for src=%u\n", src_id);
  return 0xFFFF;
}

/* =========================================================================
 * init_node_coordinates (fallback grid, hardware-safe slot)
 * =========================================================================
 */
static void
init_node_coordinates(void)
{
  uint16_t idx = HW_SLOT(node_id);
  my_x_m = (int16_t)((idx % COORD_GRID_COLS) * COORD_SPACING_M);
  my_y_m = (int16_t)((idx / COORD_GRID_COLS) * COORD_SPACING_M);
  LOG_INFO("INIT node=%u hw_slot=%u fallback_xy=(%d,%d)\n",
           node_id, idx, (int)my_x_m, (int)my_y_m);
}

/* =========================================================================
 * rdf_decay_interval
 * Returns SEND_INTERVAL * hop^Q (overflow-safe)
 * For hop=0: returns 0 (no RDF wait -- CBF only)
 * =========================================================================
 */
static clock_time_t
rdf_decay_interval(uint16_t hops)
{
  uint32_t h_pow = 1, q;

  if(hops == 0) {
    return 0;
  }

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
 * Range: DIST_MIN_WAIT (20ms) to DIST_MAX_WAIT (2000ms) at 100Hz.
 * =========================================================================
 */
static clock_time_t
distance_based_wait(const flood_msg_t *msg)
{
  int32_t dx = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  int32_t dy = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  uint32_t dist2 = (uint32_t)(dx * dx + dy * dy);
  uint32_t max_d2 = (uint32_t)DIST_MAX_RANGE_M * (uint32_t)DIST_MAX_RANGE_M;
  clock_time_t wait;

  if(dist2 == 0) {
    return DIST_MAX_WAIT;
  }
  if(dist2 > max_d2) {
    dist2 = max_d2;
  }

  wait = (clock_time_t)(DIST_MAX_WAIT -
         (clock_time_t)(((uint64_t)(DIST_MAX_WAIT - DIST_MIN_WAIT) *
                         (uint64_t)dist2) / (uint64_t)max_d2));

  if(wait < DIST_MIN_WAIT) {
    wait = DIST_MIN_WAIT;
  }
  if(wait > DIST_MAX_WAIT) {
    wait = DIST_MAX_WAIT;
  }
  return wait;
}

/* =========================================================================
 * cancel_all_timers
 * =========================================================================
 */
static void
cancel_all_timers(rdf_state_t *st)
{
  ctimer_stop(&st->cbf_timer);
  ctimer_stop(&st->rdf_timer);
  st->phase = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase2_callback (Phase 2 -- RDF wait complete -> TX)
 * =========================================================================
 */
static void
rdf_phase2_callback(void *ptr)
{
  rdf_state_t *st = (rdf_state_t *)ptr;
  flood_msg_t *msg;
  uint16_t slot;

  if(st == NULL || !st->has_pending || st->phase != 2) {
    if(st) {
      st->phase = 0;
      st->has_pending = 0;
    }
    return;
  }

  msg = &st->pending_msg;
  msg->hop_count++;
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  LOG_INFO("FWD src=%u seq=%u hop=%u sender_xy=(%d,%d)\n",
           msg->src_id, msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

  if(valid_src(msg->src_id)) {
    slot = HW_SLOT(msg->src_id);
    fwd_count[slot]++;
  }

  st->phase = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase1_callback (Phase 1 -- CBF wait complete -> start Phase 2)
 * =========================================================================
 */
static void
rdf_phase1_callback(void *ptr)
{
  rdf_state_t *st = (rdf_state_t *)ptr;
  clock_time_t rdf_wait;

  if(st == NULL || !st->has_pending || st->phase != 1) {
    if(st) {
      st->phase = 0;
      st->has_pending = 0;
    }
    return;
  }

  rdf_wait = rdf_decay_interval(st->pending_msg.hop_count);

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
 * =========================================================================
 */
static int
suppress_check(rdf_state_t *st, uint16_t src, uint16_t seq,
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
 * =========================================================================
 */
static int
handle_arrival_during_wait(rdf_state_t *st, uint16_t src,
                            flood_msg_t *msg, const char *phase_label)
{
  uint16_t seq = msg->seq_no;

  if(seq == st->pending_seq) {
    return suppress_check(st, src, seq, msg->hop_count, phase_label);
  }

  if(seq > st->pending_seq) {
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

  LOG_INFO("PENDING_DROP_STALE src=%u seq=%u buffered_seq=%u stale_seq=%u"
           " period=%s\n",
           src, seq,
           st->pending_seq, seq, phase_label);
  return 0;
}

/* =========================================================================
 * start_cbf_phase
 * slot is pre-computed by the caller (safe_hw_slot already validated).
 * =========================================================================
 */
static void
start_cbf_phase(uint16_t src, flood_msg_t *msg, uint16_t slot)
{
  rdf_state_t *st;
  clock_time_t dist_wait, jitter;
  int32_t dx, dy;
  uint32_t dist2;
  uint16_t dist_m;

  (void)src;  /* src already validated by caller */
  st = &rdf_state[slot];

  jitter = RDF_MIN_JITTER;
  if(RDF_MAX_JITTER > RDF_MIN_JITTER) {
    jitter += (clock_time_t)(random_rand()
              % (uint16_t)(RDF_MAX_JITTER - RDF_MIN_JITTER + 1));
  }

  dist_wait = distance_based_wait(msg);

  dx = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  dy = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  dist2 = (uint32_t)(dx * dx + dy * dy);
  for(dist_m = 0;
      (uint32_t)dist_m * (uint32_t)dist_m < dist2 && dist_m < 255;
      dist_m++) {
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

  st->pending_msg = *msg;
  st->pending_seq = msg->seq_no;
  st->has_pending = 1;
  st->phase = 1;
  ctimer_set(&st->cbf_timer, dist_wait + jitter, rdf_phase1_callback, st);
}

/* =========================================================================
 * log_dissemination
 * =========================================================================
 */
static void
log_dissemination(uint16_t src, uint16_t seq, uint16_t slot)
{
  LOG_INFO("DISSEM src=%u seq=%u node=%u recv_by_me=%" PRIu32
           " last_seq=%u\n",
           src, seq, node_id, recv_count[slot], last_seq_seen[slot]);
}

/* =========================================================================
 * log_stats_summary
 * PDR removed. src= field prints real node_id via slot_to_srcid[].
 * =========================================================================
 */
static void
log_stats_summary(void)
{
  uint16_t slot;

  for(slot = 0; slot < MAX_NODES; slot++) {
    if(recv_count[slot] == 0 && fwd_count[slot] == 0) {
      continue;
    }
    LOG_INFO("STATS node=%u src=%u recv=%" PRIu32 " exp=%" PRIu32
             " fwd=%" PRIu32 "\n",
             node_id, slot_to_srcid[slot],
             recv_count[slot],
             (uint32_t)last_seq_seen[slot] + 1,
             fwd_count[slot]);
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
  flood_msg_t msg;
  uint16_t src, seq, slot;
  uint8_t is_new;
  rdf_state_t *st;
  const char *phase_label;

  (void)c;
  (void)sender_addr;
  (void)sender_port;
  (void)receiver_addr;
  (void)receiver_port;

  if(datalen != sizeof(flood_msg_t)) {
    LOG_WARN("RX wrong size %u (expected %u)\n",
             datalen, (unsigned)sizeof(flood_msg_t));
    return;
  }

  memcpy(&msg, data, sizeof(msg));
  src = msg.src_id;
  seq = msg.seq_no;

  /* Basic validity: reject src=0, own packets, insane hop counts */
  if(!valid_src(src) || src == node_id) {
    return;
  }
  if(msg.hop_count > 20) {
    LOG_WARN("RX sanity fail: src=%u seq=%u hop=%u too large, dropping\n",
             src, seq, msg.hop_count);
    return;
  }

  /* Resolve slot -- drop on hash collision */
  slot = safe_hw_slot(src);
  if(slot == 0xFFFF) {
    return;
  }

  is_new = !seen[slot][seq % MAX_SEQ_TRACK];

  LOG_INFO("ARRIVE src=%u seq=%u hop=%u sender_xy=(%d,%d) is_new=%u\n",
           src, seq, msg.hop_count,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (unsigned)is_new);

  st = &rdf_state[slot];
  phase_label = (st->phase == 1) ? "cbf" : "rdf_decay";

  if(!is_new) {
    if(st->has_pending) {
      handle_arrival_during_wait(st, src, &msg, phase_label);
    }
    return;
  }

  seen[slot][seq % MAX_SEQ_TRACK] = 1;
  recv_count[slot]++;
  if(seq > last_seq_seen[slot]) {
    last_seq_seen[slot] = seq;
  }

  LOG_INFO("RX src=%u seq=%u hop=%u src_xy=(%d,%d) sender_xy=(%d,%d) "
           "my_xy=(%d,%d)\n",
           src, seq, msg.hop_count,
           (int)msg.src_x_m, (int)msg.src_y_m,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (int)my_x_m, (int)my_y_m);

  log_dissemination(src, seq, slot);

  if(st->has_pending) {
    handle_arrival_during_wait(st, src, &msg, phase_label);
    return;
  }

  start_cbf_phase(src, &msg, slot);
}

/* =========================================================================
 * Main process
 * =========================================================================
 */
PROCESS_THREAD(rdf_process, ev, data)
{
  static struct etimer periodic_timer;
  static struct etimer stats_timer;
  flood_msg_t msg;
  uint16_t i, j;

  PROCESS_BEGIN();

  init_node_coordinates();

  for(i = 0; i < MAX_NODES; i++) {
    last_seq_seen[i]          = 0;
    recv_count[i]             = 0;
    fwd_count[i]              = 0;
    slot_to_srcid[i]          = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) {
      seen[i][j] = 0;
    }
    rdf_state[i].phase        = 0;
    rdf_state[i].has_pending  = 0;
    rdf_state[i].pending_seq  = 0;
  }

  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  etimer_set(&stats_timer,    STATS_TICKS);
  etimer_set(&periodic_timer, random_rand() % SEND_INTERVAL);

  LOG_INFO("START node=%u send_interval_ticks=%lu\n",
           node_id, (unsigned long)SEND_INTERVAL);

  while(1) {
    PROCESS_WAIT_EVENT();

    if(etimer_expired(&stats_timer)) {
      log_stats_summary();
      etimer_reset(&stats_timer);
    }

    if(etimer_expired(&periodic_timer)) {
      uint16_t own_slot = HW_SLOT(node_id);

      msg.src_id     = node_id;
      msg.seq_no     = my_seq;
      msg.hop_count  = 0;
      msg.src_x_m    = my_x_m;
      msg.src_y_m    = my_y_m;
      msg.sender_x_m = my_x_m;
      msg.sender_y_m = my_y_m;

      /* Mark own packet as seen so we never relay our own transmissions */
      seen[own_slot][my_seq % MAX_SEQ_TRACK] = 1;
      if(my_seq > last_seq_seen[own_slot]) {
        last_seq_seen[own_slot] = my_seq;
      }

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

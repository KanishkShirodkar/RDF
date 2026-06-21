/*
 * rdf-flood.c -- Rate Decay Flooding (RDF) for FIT IoT-LAB M3 (Grenoble)
 *
 * Nodes: m3-305, m3-306, m3-307 (Grenoble)
 * MACs : 0x8871 (34929), 0x9479 (38009), 0xa082 (41090)
 *
 * Build:
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3 clean
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3
 *
 * Flash:
 *   iotlab-node --flash build/iotlab/m3/rdf-flood.iotlab -l grenoble,m3,305+306+307
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
#define SEND_INTERVAL        (CLOCK_SECOND / 2)
#endif
#ifndef RDF_Q
#define RDF_Q                2
#endif
#ifndef RDF_MIN_JITTER
#define RDF_MIN_JITTER       (CLOCK_SECOND / 500)
#endif
#ifndef RDF_MAX_JITTER
#define RDF_MAX_JITTER       (CLOCK_SECOND / 200)
#endif
#ifndef DIST_MIN_WAIT_MS
#define DIST_MIN_WAIT_MS     5
#endif
#ifndef DIST_MAX_WAIT_MS
#define DIST_MAX_WAIT_MS     300
#endif
#ifndef DIST_MAX_RANGE_M
#define DIST_MAX_RANGE_M     50
#endif
#ifndef COORD_GRID_COLS
#define COORD_GRID_COLS      8
#endif
#ifndef COORD_SPACING_M
#define COORD_SPACING_M      10
#endif
#ifndef MAX_NODES
#define MAX_NODES            51
#endif
#ifndef MAX_SEQ_TRACK
#define MAX_SEQ_TRACK        16
#endif
#ifndef TAOI_MS
#define TAOI_MS              1125
#endif
#ifndef STATS_INTERVAL_S
#define STATS_INTERVAL_S     10
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
#include "lib/random.h"
#include <inttypes.h>
#include <string.h>

#define LOG_MODULE "RDF"
#define LOG_LEVEL  LOG_LEVEL_INFO
#define UDP_PORT   1234

#define DIST_MIN_WAIT  ((clock_time_t)((DIST_MIN_WAIT_MS * CLOCK_SECOND) / 1000))
#define DIST_MAX_WAIT  ((clock_time_t)((DIST_MAX_WAIT_MS * CLOCK_SECOND) / 1000))
#define TAOI_TICKS     ((clock_time_t)((TAOI_MS          * CLOCK_SECOND) / 1000))
#define STATS_TICKS    ((clock_time_t)( STATS_INTERVAL_S * CLOCK_SECOND))

/* HW node_id is large (MAC-derived). Hash to compact array index. */
#define HW_IDX(id)  ((uint16_t)((id) % MAX_NODES))

/* =========================================================================
 * Data types
 * =========================================================================
 */
typedef struct {
  uint16_t     src_id;
  uint16_t     seq_no;
  uint16_t     hop_count;
  clock_time_t origin_time;
  int16_t      src_x_m;
  int16_t      src_y_m;
  int16_t      sender_x_m;
  int16_t      sender_y_m;
} flood_msg_t;

typedef struct {
  uint16_t      real_src_id;
  uint8_t       occupied;
  uint8_t       forwarded_before;
  uint8_t       timer_active;
  uint8_t       has_pending;
  uint16_t      pending_seq;
  flood_msg_t   pending_msg;
  struct ctimer forward_timer;
  clock_time_t  next_allowed_time;
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
static uint32_t      excess_count[MAX_NODES];
static uint32_t      fwd_count[MAX_NODES];
static uint32_t      own_tx_count = 0;

PROCESS(rdf_process, "RDF Flooding");
AUTOSTART_PROCESSES(&rdf_process);

/* =========================================================================
 * get_or_claim_slot
 * =========================================================================
 */
static uint16_t get_or_claim_slot(uint16_t src_id)
{
  uint16_t idx = HW_IDX(src_id);
  if(!rdf_state[idx].occupied) {
    rdf_state[idx].occupied    = 1;
    rdf_state[idx].real_src_id = src_id;
    return idx;
  }
  if(rdf_state[idx].real_src_id == src_id) {
    return idx;
  }
  LOG_WARN("HASH_COLLISION src=%u idx=%u -- drop\n", src_id, idx);
  return MAX_NODES;
}

/* =========================================================================
 * init_node_coordinates
 * =========================================================================
 */
static void init_node_coordinates(void)
{
  uint16_t idx = HW_IDX(node_id);
  my_x_m = (int16_t)((idx % COORD_GRID_COLS) * COORD_SPACING_M);
  my_y_m = (int16_t)((idx / COORD_GRID_COLS) * COORD_SPACING_M);
  LOG_INFO("INIT node_id=%u hw_idx=%u xy=(%d,%d)\n",
           node_id, idx, (int)my_x_m, (int)my_y_m);
}

/* =========================================================================
 * rdf_decay_interval
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
 * =========================================================================
 */
static clock_time_t distance_based_wait(const flood_msg_t *msg)
{
  int32_t  dx  = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  int32_t  dy  = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  uint32_t dist2  = (uint32_t)(dx * dx + dy * dy);
  uint32_t max_d2 = (uint32_t)DIST_MAX_RANGE_M * (uint32_t)DIST_MAX_RANGE_M;
  clock_time_t wait;

  if(dist2 == 0)       { return DIST_MAX_WAIT; }
  if(dist2 > max_d2)   { dist2 = max_d2; }

  wait = (clock_time_t)(DIST_MAX_WAIT -
         (clock_time_t)(((uint64_t)(DIST_MAX_WAIT - DIST_MIN_WAIT) *
                         (uint64_t)dist2) / (uint64_t)max_d2));

  if(wait < DIST_MIN_WAIT) { wait = DIST_MIN_WAIT; }
  if(wait > DIST_MAX_WAIT) { wait = DIST_MAX_WAIT; }
  return wait;
}

/* =========================================================================
 * rdf_cancel_pending
 * =========================================================================
 */
static void rdf_cancel_pending(uint16_t src, uint16_t seq)
{
  uint16_t    idx = get_or_claim_slot(src);
  rdf_state_t *st;
  if(idx >= MAX_NODES) { return; }
  st = &rdf_state[idx];
  if(st->timer_active && st->has_pending && st->pending_seq == seq) {
    ctimer_stop(&st->forward_timer);
    st->timer_active = 0;
    st->has_pending  = 0;
    LOG_INFO("CBF_SUPPRESS src=%u seq=%u\n", src, seq);
  }
}

/* =========================================================================
 * rdf_forward_callback
 * =========================================================================
 */
static void rdf_forward_callback(void *ptr)
{
  rdf_state_t *st  = (rdf_state_t *)ptr;
  flood_msg_t *msg;
  uint16_t     idx;

  if(st == NULL || !st->has_pending) {
    if(st) { st->timer_active = 0; }
    return;
  }
  msg = &st->pending_msg;
  msg->hop_count++;
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  LOG_INFO("FWD src=%u seq=%u hop=%u xy=(%d,%d)\n",
           msg->src_id, msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

  idx = HW_IDX(msg->src_id);
  if(idx < MAX_NODES) { fwd_count[idx]++; }

  st->next_allowed_time = clock_time() + rdf_decay_interval(msg->hop_count);
  st->forwarded_before  = 1;
  st->timer_active      = 0;
  st->has_pending       = 0;
}

/* =========================================================================
 * rdf_handle_new_packet
 * =========================================================================
 */
static void rdf_handle_new_packet(uint16_t src, flood_msg_t *msg)
{
  uint16_t     idx = get_or_claim_slot(src);
  rdf_state_t *st;
  clock_time_t now, jitter, dist_wait;

  if(idx >= MAX_NODES) { return; }
  st  = &rdf_state[idx];
  now = clock_time();

  jitter = RDF_MIN_JITTER;
  if(RDF_MAX_JITTER > RDF_MIN_JITTER) {
    jitter += (clock_time_t)(random_rand()
               % (uint16_t)(RDF_MAX_JITTER - RDF_MIN_JITTER + 1));
  }
  dist_wait = distance_based_wait(msg);

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
 * Metric logging
 * =========================================================================
 */
static void log_pdr(uint16_t src, uint16_t idx)
{
  uint32_t exp  = (uint32_t)last_seq_seen[idx] + 1;
  uint32_t pdr  = (exp > 0) ? (1000UL * recv_count[idx]) / exp : 0;
  LOG_INFO("PDR src=%u recv=%" PRIu32 "/%" PRIu32 " = %lu.%lu%%\n",
           src, recv_count[idx], exp,
           (unsigned long)(pdr / 10), (unsigned long)(pdr % 10));
}

static void log_dissemination(uint16_t src, uint16_t seq, uint16_t idx)
{
  LOG_INFO("DISSEM src=%u seq=%u node=%u recv_by_me=%" PRIu32 " last_seq=%u\n",
           src, seq, node_id, recv_count[idx], last_seq_seen[idx]);
}

static void log_excess(uint16_t src, uint16_t seq,
                       const flood_msg_t *msg, uint16_t idx)
{
  clock_time_t now = clock_time();
  clock_time_t aoi;
  uint32_t     aoi_ms;

  if(now < msg->origin_time) { return; }
  aoi    = now - msg->origin_time;
  aoi_ms = (uint32_t)((uint64_t)aoi * 1000UL / CLOCK_SECOND);

  if(aoi > TAOI_TICKS) {
    excess_count[idx]++;
    LOG_INFO("EXCESS src=%u seq=%u node=%u aoi_ms=%" PRIu32
             " taoi_ms=%u hop=%u total=%" PRIu32 "\n",
             src, seq, node_id, aoi_ms,
             (unsigned)TAOI_MS, msg->hop_count, excess_count[idx]);
  } else {
    LOG_INFO("AOI_OK src=%u seq=%u node=%u aoi_ms=%" PRIu32
             " taoi_ms=%u hop=%u\n",
             src, seq, node_id, aoi_ms,
             (unsigned)TAOI_MS, msg->hop_count);
  }
}

static void log_stats_summary(void)
{
  uint16_t idx;
  for(idx = 0; idx < MAX_NODES; idx++) {
    uint32_t exp, pdr_t, exc_t;
    uint16_t src;
    if(!rdf_state[idx].occupied) { continue; }
    if(recv_count[idx] == 0 && fwd_count[idx] == 0) { continue; }
    src   = rdf_state[idx].real_src_id;
    exp   = (uint32_t)last_seq_seen[idx] + 1;
    pdr_t = (exp > 0) ? (1000UL * recv_count[idx]) / exp : 0;
    exc_t = (recv_count[idx] > 0)
            ? (1000UL * excess_count[idx]) / recv_count[idx] : 0;
    LOG_INFO("STATS node=%u src=%u recv=%" PRIu32 " exp=%" PRIu32
             " excess=%" PRIu32 " fwd=%" PRIu32
             " pdr=%lu.%lu%% exc_prob=%lu.%lu%%\n",
             node_id, src,
             recv_count[idx], exp, excess_count[idx], fwd_count[idx],
             (unsigned long)(pdr_t / 10), (unsigned long)(pdr_t % 10),
             (unsigned long)(exc_t / 10), (unsigned long)(exc_t % 10));
  }
  LOG_INFO("STATS node=%u own_tx=%" PRIu32 "\n", node_id, own_tx_count);
}

/* =========================================================================
 * UDP RX callback
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
  uint16_t     src, seq, idx;
  uint8_t      is_new;
  rdf_state_t *st;

  (void)c; (void)sender_addr; (void)sender_port;
  (void)receiver_addr; (void)receiver_port;

  if(datalen != sizeof(flood_msg_t)) {
    LOG_WARN("RX bad size %u\n", datalen);
    return;
  }
  memcpy(&msg, data, sizeof(msg));
  src = msg.src_id;
  seq = msg.seq_no;

  if(src == node_id) { return; }

  idx = get_or_claim_slot(src);
  if(idx >= MAX_NODES) { return; }

  is_new = !seen[idx][seq % MAX_SEQ_TRACK];

  LOG_INFO("ARRIVE src=%u seq=%u hop=%u is_new=%u\n",
           src, seq, msg.hop_count, (unsigned)is_new);

  if(!is_new) {
    rdf_cancel_pending(src, seq);
    return;
  }

  seen[idx][(seq + 1) % MAX_SEQ_TRACK] = 0;
  seen[idx][seq        % MAX_SEQ_TRACK] = 1;
  recv_count[idx]++;
  if(seq > last_seq_seen[idx]) { last_seq_seen[idx] = seq; }

  LOG_INFO("RX src=%u seq=%u hop=%u sxy=(%d,%d) mxy=(%d,%d)\n",
           src, seq, msg.hop_count,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (int)my_x_m, (int)my_y_m);

  log_pdr(src, idx);
  log_dissemination(src, seq, idx);
  log_excess(src, seq, &msg, idx);

  st = &rdf_state[idx];
  if(st->timer_active && st->has_pending && st->pending_seq == seq) {
    rdf_cancel_pending(src, seq);
    return;
  }
  rdf_handle_new_packet(src, &msg);
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
    last_seq_seen[i]               = 0;
    recv_count[i]                  = 0;
    excess_count[i]                = 0;
    fwd_count[i]                   = 0;
    rdf_state[i].occupied          = 0;
    rdf_state[i].real_src_id       = 0;
    rdf_state[i].forwarded_before  = 0;
    rdf_state[i].timer_active      = 0;
    rdf_state[i].has_pending       = 0;
    rdf_state[i].pending_seq       = 0;
    rdf_state[i].next_allowed_time = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) { seen[i][j] = 0; }
  }

  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  etimer_set(&stats_timer,    STATS_TICKS);
  etimer_set(&periodic_timer, SEND_INTERVAL +
             (clock_time_t)(random_rand() % CLOCK_SECOND));

  LOG_INFO("RDF started node_id=%u hw_idx=%u xy=(%d,%d)\n",
           node_id, HW_IDX(node_id), (int)my_x_m, (int)my_y_m);

  while(1) {
    PROCESS_WAIT_EVENT();

    if(etimer_expired(&periodic_timer)) {
      uint16_t own_idx;
      memset(&msg, 0, sizeof(msg));
      msg.src_id      = node_id;
      msg.seq_no      = my_seq;
      msg.hop_count   = 0;
      msg.origin_time = clock_time();
      msg.src_x_m     = my_x_m;
      msg.src_y_m     = my_y_m;
      msg.sender_x_m  = my_x_m;
      msg.sender_y_m  = my_y_m;

      own_idx = HW_IDX(node_id);
      seen[own_idx][(my_seq + 1) % MAX_SEQ_TRACK] = 0;
      seen[own_idx][my_seq       % MAX_SEQ_TRACK]  = 1;
      if(my_seq > last_seq_seen[own_idx]) {
        last_seq_seen[own_idx] = my_seq;
      }

      simple_udp_sendto(&udp_conn, &msg, sizeof(msg), &mcast_addr);
      own_tx_count++;
      my_seq++;

      LOG_INFO("TX seq=%u xy=(%d,%d) own_tx=%" PRIu32 "\n",
               msg.seq_no, (int)my_x_m, (int)my_y_m, own_tx_count);

      etimer_set(&periodic_timer, SEND_INTERVAL);
    }

    if(etimer_expired(&stats_timer)) {
      log_stats_summary();
      etimer_set(&stats_timer, STATS_TICKS);
    }
  }

  PROCESS_END();
}

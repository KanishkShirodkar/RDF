/*
 * rdf-flood.c -- Rate Decay Flooding (RDF) for Contiki-NG / COOJA
 *
 * Self-contained: no project-conf.h needed.
 * All tunable parameters are in the "PARAMETERS" block below.
 *
 * Paper: "On the Feasibility of Position-Flooding in Urban UAV Networks"
 *        Fuger & Timm-Giel, IEEE VTC 2023-Spring
 *
 * Fixes applied:
 *   FIX-1  CBF timer uses IMMEDIATE SENDER coords, not flood origin.
 *   FIX-2  Overhearing suppression fires on buffered-then-overheard too.
 *   FIX-3  Every forwarder stamps own coords into sender_x/y before TX.
 *   FIX-4  Live COOJA position via shared sim_pos_x/sim_pos_y variables.
 *          COOJA Java plugin writes these before every mote tick via JNI.
 *          On real hardware the variables stay 0 and the grid-formula
 *          fallback is used transparently -- no #ifdef needed.
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
 * PARAMETERS -- change values here, or override via Makefile CFLAGS.
 *
 *  SEND_INTERVAL     Base broadcast interval i.
 *                    CLOCK_SECOND/8 ~ 125 ms.  Paper used 120 ms (802.11p).
 *
 *  RDF_Q             Decay exponent q in:  T'_fwd = T_fwd + i * h^q
 *                    2 = paper-optimal.  0 = pure CBF, no decay.
 *
 *  RDF_MIN_JITTER    Min random tie-breaking jitter (clock ticks).
 *  RDF_MAX_JITTER    Max random tie-breaking jitter (clock ticks).
 *
 *  DIST_MIN_WAIT_MS  Contention timer floor  -- FARTHEST node fires first (ms).
 *  DIST_MAX_WAIT_MS  Contention timer ceiling -- CO-LOCATED node waits most (ms).
 *                    Keep DIST_MAX_WAIT_MS < SEND_INTERVAL.
 *
 *  DIST_MAX_RANGE_M  Radio range cap for CBF distance mapping (metres).
 *                    Match to your COOJA Unit Disk TX range.
 *
 *  COORD_GRID_COLS   Columns in fallback virtual grid.
 *  COORD_SPACING_M   Metres between adjacent grid nodes (fallback only).
 *                    These are ONLY used when COOJA does NOT push live
 *                    coordinates (i.e. real hardware or plugin not loaded).
 *
 *  MAX_NODES         Must be >= your node count + 1.
 *  MAX_SEQ_TRACK     Duplicate-detection window.  MUST be a power of 2.
 *
 *  POS_UPDATE_INTERVAL_MS  How often (ms) the process polls sim_pos_updated.
 *                          Set equal to SEND_INTERVAL for zero overhead.
 *                          Lower values give faster position refresh for
 *                          fast-moving nodes.
 * =========================================================================
 */
#ifndef SEND_INTERVAL
#define SEND_INTERVAL       (CLOCK_SECOND / 2)   /* /2 for ~500ms ---------   /8 for ~125 ms */
#endif
#ifndef RDF_Q
#define RDF_Q               2
#endif
#ifndef RDF_MIN_JITTER
#define RDF_MIN_JITTER      (CLOCK_SECOND / 500) /* 2 ms */     /*(CLOCK_SECOND / 100)  ---> 10 ms */
#endif
#ifndef RDF_MAX_JITTER
#define RDF_MAX_JITTER      (CLOCK_SECOND / 200)  /* 5 ms */    /*(CLOCK_SECOND / 20)  ---> 50 ms */
#endif
#ifndef DIST_MIN_WAIT_MS
#define DIST_MIN_WAIT_MS    5     //10
#endif
#ifndef DIST_MAX_WAIT_MS
#define DIST_MAX_WAIT_MS    300   //150
#endif
#ifndef DIST_MAX_RANGE_M
#define DIST_MAX_RANGE_M    50    //cooja range provided 50mtr
#endif
#ifndef COORD_GRID_COLS
#define COORD_GRID_COLS     8
#endif
#ifndef COORD_SPACING_M
#define COORD_SPACING_M     50
#endif
#ifndef MAX_NODES
#define MAX_NODES           32
#endif
#ifndef MAX_SEQ_TRACK
#define MAX_SEQ_TRACK       256    /* MUST be a power of 2 */
#endif
#ifndef POS_UPDATE_INTERVAL_MS
#define POS_UPDATE_INTERVAL_MS  125   /* same as SEND_INTERVAL default */
#endif

/* =========================================================================
 * INCLUDES -- all Contiki headers AFTER the config defines above
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

/* =========================================================================
 * FIX-4: COOJA LIVE POSITION SHARED VARIABLES
 *
 * These three globals are the bridge between the COOJA Java simulator and
 * your C firmware.  COOJA's JNI layer can read and write any global in the
 * mote's memory by symbol name.  The RdfPositionPlugin.java plugin (see
 * companion file) calls setMemory("sim_pos_x", ...) before every mote tick,
 * injecting the real (x, y) position from COOJA's internal Position object.
 *
 *   sim_pos_x   x-coordinate in centimetres  (int32, signed)
 *               e.g. 4500 means 45.00 metres
 *   sim_pos_y   y-coordinate in centimetres  (int32, signed)
 *   sim_pos_updated   set to 1 by plugin after writing new coords;
 *                     cleared to 0 by this firmware after reading.
 *
 * On real hardware (no COOJA), these stay 0 and init_node_coordinates()
 * provides the fallback grid position -- no #ifdef guard needed.
 * =========================================================================
 */
volatile int32_t sim_pos_x       = 0;   /* cm -- COOJA writes this */
volatile int32_t sim_pos_y       = 0;   /* cm -- COOJA writes this */
volatile uint8_t sim_pos_updated = 0;   /* flag -- COOJA sets to 1 */

/* =========================================================================
 * Packet format
 *
 *   src_x_m / src_y_m       flood originator -- never changed in transit
 *   sender_x_m / sender_y_m immediate upstream hop -- stamped each forward
 *                            (FIX-3)
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

/* Module globals */
static struct simple_udp_connection udp_conn;
static uip_ipaddr_t  mcast_addr;
static uint16_t      my_seq  = 0;
static int16_t       my_x_m  = 0;
static int16_t       my_y_m  = 0;
static uint16_t      last_seq_seen[MAX_NODES];
static uint32_t      recv_count[MAX_NODES];
static uint8_t       seen[MAX_NODES][MAX_SEQ_TRACK];
static rdf_state_t   rdf_state[MAX_NODES];

PROCESS(rdf_process, "RDF Flooding");
AUTOSTART_PROCESSES(&rdf_process);

/* =========================================================================
 * valid_src -- bounds check before any array index
 * =========================================================================
 */
static int valid_src(uint16_t src)
{
  return (src > 0 && src < MAX_NODES);
}

/* =========================================================================
 * init_node_coordinates  (FALLBACK)
 *
 * Derives a virtual grid (x,y) from node_id.  Used at startup and on real
 * hardware where COOJA never writes sim_pos_x/y.
 *
 * With FIX-4 installed, COOJA will overwrite my_x_m/my_y_m on the first
 * tick via update_position_from_sim(), so this only matters as a sensible
 * default before COOJA pushes a real value.
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
 * Reads the shared sim_pos_x / sim_pos_y variables that COOJA's Java plugin
 * writes before every tick.  Converts centimetres -> metres and updates
 * my_x_m / my_y_m.  Clears the flag so repeated calls are a no-op until
 * COOJA writes a fresh value.
 *
 * Call this at the top of your main loop (before building any packet).
 * =========================================================================
 */
static void update_position_from_sim(void)
{
  if(sim_pos_updated) {
    int16_t new_x = (int16_t)(sim_pos_x / 100);
    int16_t new_y = (int16_t)(sim_pos_y / 100);
    sim_pos_updated = 0;  /* clear flag BEFORE updating to avoid race */

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
 * Overflow is saturated to 0xFFFFFFFF (infinite cooldown).
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
 *
 * Distance is to IMMEDIATE SENDER (msg->sender_x_m / sender_y_m).
 * Paper sec.III-C: "timer inversely proportional to distance of sender."
 *
 *   dist == 0          -> DIST_MAX_WAIT  (co-located, waits longest)
 *   dist >= MAX_RANGE  -> DIST_MIN_WAIT  (farthest, fires first)
 *
 * Uses dist^2 to avoid sqrt() on MCUs without FPU.  The resulting mild
 * non-linearity is accepted (suppression ordering is still correct).
 * =========================================================================
 */
static clock_time_t distance_based_wait(const flood_msg_t *msg)
{
  int32_t  dx, dy;
  uint32_t dist2, max_d2;
  clock_time_t wait;

  /* FIX-1: use SENDER coords (immediate upstream hop), not origin src */
  dx    = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  dy    = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  dist2 = (uint32_t)(dx * dx + dy * dy);

  if(dist2 == 0) { return DIST_MAX_WAIT; }

  max_d2 = (uint32_t)DIST_MAX_RANGE_M * (uint32_t)DIST_MAX_RANGE_M;
  if(dist2 > max_d2) { dist2 = max_d2; }

  /* 64-bit intermediate prevents overflow */
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
 *
 * Cancels pending forward for (src, seq).
 * Called on BOTH the duplicate path AND the buffered-then-overheard path.
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

  /* FIX-3: stamp OUR current coordinates as the sender before TX.
   * FIX-4: my_x_m/my_y_m are already live-updated from COOJA, so this
   *         automatically carries the correct moving position. */
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  LOG_INFO("FWD src=%u seq=%u hop=%u sender_xy=(%d,%d)\n",
           msg->src_id, msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

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

  //start fix
  dist_wait = distance_based_wait(msg);

/* Compute integer distance to immediate sender in metres for logging
 * Only integer math, no sqrt() needed. */
{
  int32_t dx = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  int32_t dy = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  uint32_t dist2 = (uint32_t)(dx * dx + dy * dy);
  uint16_t dist_m = 0;

  /* Cheap integer sqrt approximation: increase dist_m until dist_m^2 >= dist2.
   * For our ranges (<= 50m), this is at most 50 iterations. */
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
         
  /////end fix 
         
  /* Buffer the freshest packet; the running timer will send it */
  st->pending_msg = *msg;
  st->pending_seq = msg->seq_no;
  st->has_pending = 1;

  if(!st->forwarded_before) {
    /* First-ever packet from this source: no decay gate yet */
    if(st->timer_active) { return; }
    st->timer_active = 1;
    ctimer_set(&st->forward_timer, dist_wait + jitter,
               rdf_forward_callback, st);
    return;
  }

  if(now >= st->next_allowed_time) {
    /* Decay cooldown has elapsed -- enter CBF contention */
    if(st->timer_active) { return; }
    st->timer_active = 1;
    ctimer_set(&st->forward_timer, dist_wait + jitter,
               rdf_forward_callback, st);
  } else {
    /* Still inside decay cooldown -- defer, do not increase rate */
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
 * udp_rx_callback -- called by Contiki on every received UDP packet
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
  
  /* FIX-DBG-2: log every arrival (new AND duplicate) to verify which
 * hop copy arrives first at this node. If hop>0 arrives before hop=0,
 * the sender coords used for CBF timer will be a relay, not the source.
 * Label: ARRIVE to distinguish from RX (first-only) log. */
  LOG_INFO("ARRIVE src=%u seq=%u hop=%u sender_xy=(%d,%d) is_new=%u\n",
         src, seq, msg.hop_count,
         (int)msg.sender_x_m, (int)msg.sender_y_m,
         (unsigned)(!seen[src][seq % MAX_SEQ_TRACK]));
  ////end fix  
  
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

  /* ---- FIX-2 path B: buffered-then-overheard suppression ----
   * First-time receive, but we already buffered this (src,seq) from
   * an earlier copy -> a neighbour already won the contention race. */
  st = &rdf_state[src];
  if(st->timer_active && st->has_pending && st->pending_seq == seq) {
    rdf_cancel_pending(src, seq);
    return;
  }
  /* FIX-DBG-3: log WHY suppression did NOT fire here.
 * This tells you if the timer was already gone (fired before overhear)
 * or if the pending seq was a different packet. */
  LOG_INFO("SUPPRESS_MISS src=%u seq=%u timer_active=%u has_pending=%u pending_seq=%u\n",
         src, seq,
         (unsigned)st->timer_active,
         (unsigned)st->has_pending,
         (unsigned)st->pending_seq);
  ////end fix 
  rdf_handle_new_packet(src, &msg);
}

/* =========================================================================
 * Main process
 * =========================================================================
 */
PROCESS_THREAD(rdf_process, ev, data)
{
  static struct etimer periodic_timer;
  static struct etimer pos_timer;        /* FIX-4: position poll timer */
  flood_msg_t msg;
  uint16_t i, j;

  PROCESS_BEGIN();

  /* ---- Initialise node coordinates (grid fallback) ---- */
  init_node_coordinates();

  /* ---- Initialise state tables ---- */
  for(i = 0; i < MAX_NODES; i++) {
    last_seq_seen[i] = 0;
    recv_count[i]    = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) { seen[i][j] = 0; }
    rdf_state[i].forwarded_before = 0;
    rdf_state[i].timer_active     = 0;
    rdf_state[i].has_pending      = 0;
    rdf_state[i].pending_seq      = 0;
    rdf_state[i].next_allowed_time = 0;
  }

  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  /* FIX-4: start position-poll timer -- fires every POS_UPDATE_INTERVAL_MS.
   * This is independent of the broadcast timer so fast-moving nodes can
   * set POS_UPDATE_INTERVAL_MS much smaller than SEND_INTERVAL. */
  etimer_set(&pos_timer, POS_UPDATE_TICKS);

  /* Randomised start -- prevents synchronised broadcast storm at t=0 */
  etimer_set(&periodic_timer, random_rand() % SEND_INTERVAL);

  while(1) {
    PROCESS_WAIT_EVENT();

    /* ---- FIX-4: position refresh from COOJA ---- */
    if(etimer_expired(&pos_timer)) {
      update_position_from_sim();
      etimer_reset(&pos_timer);
    }

    /* ---- Periodic broadcast ---- */
    if(etimer_expired(&periodic_timer)) {

      /* Always read latest position before building the packet */
      update_position_from_sim();

      /* Broadcast own position at hop 0 */
      msg.src_id      = node_id;
      msg.seq_no      = my_seq;
      msg.hop_count   = 0;
      msg.origin_time = clock_time();
      msg.src_x_m     = my_x_m;
      msg.src_y_m     = my_y_m;
      msg.sender_x_m  = my_x_m;  /* sender == self at hop 0 */
      msg.sender_y_m  = my_y_m;

      if(valid_src(node_id)) {
        seen[node_id][my_seq % MAX_SEQ_TRACK] = 1;
        if(my_seq > last_seq_seen[node_id]) {
          last_seq_seen[node_id] = my_seq;
        }
      }

      LOG_INFO("TX seq=%u xy=(%d,%d)\n", my_seq, (int)my_x_m, (int)my_y_m);
      simple_udp_sendto(&udp_conn, &msg, sizeof(msg), &mcast_addr);
      my_seq++;

      etimer_set(&periodic_timer,
                 SEND_INTERVAL - RDF_MIN_JITTER
                 + (random_rand() % (2 * RDF_MIN_JITTER + 1)));
    }
  }

  PROCESS_END();
}

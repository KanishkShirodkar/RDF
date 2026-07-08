/*
 * rdf-flood-HW-v11.c -- Rate Decay Flooding (RDF) for FIT IoT-LAB M3 hardware
 *                        with RAM measurement (stack painting + static reporting)
 *
 * Changes from v10:
 *  - Stack painting technique added for runtime peak-stack measurement.
 *    Uses _ebss and _estack symbols directly from stm32f103rey6.ld — NO
 *    linker script modifications required. These symbols already exist:
 *      _ebss   = end of .bss section (bottom of free RAM / stack paint start)
 *      _estack = 0x20010000 (top of 64KB RAM, stack grows DOWN from here)
 *  - stack_paint()        called once at PROCESS_BEGIN before any init
 *  - stack_measure_peak() called every STATS_INTERVAL_S seconds
 *  - RAM_REPORT log line emitted every stats interval:
 *      RAM_REPORT node=m3-XX max_nodes=N static_bss=B peak_stack=S total=T
 *    where:
 *      static_bss  = sizeof all app arrays (.bss contribution, bytes)
 *      peak_stack  = bytes consumed from top of RAM downward (high-water mark)
 *      total       = static_bss + peak_stack  (best estimate of total RAM used)
 *  - All v10 RDF logic, log formats, and timing are UNCHANGED.
 *
 * STM32F103REY6 memory map (from stm32f103rey6.ld):
 *   RAM   : ORIGIN = 0x20000000, LENGTH = 64K
 *   _estack = 0x20010000   (top of RAM, stack grows DOWN)
 *   _ebss   = end of .bss  (linker-computed, bottom of free RAM)
 *   Stack region = [_ebss .. _estack]  (everything not used by .data/.bss)
 *
 * Build:
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3 clean
 *   ARCH_PATH=../../../arch make TARGET=iotlab BOARD=m3
 *
 * After build, get static RAM:
 *   arm-none-eabi-size build/iotlab/m3/rdf-flood-HW-v11.iotlab
 *   Static RAM = data + bss columns
 *
 * Flash:
 *   iotlab-node --flash build/iotlab/m3/rdf-flood-HW-v11.iotlab \
 *     -l toulouse,m3,<n1>+<n2>+<n3>
 *
 * Monitor:
 *   serial_aggregator | tee ~/rdf_v11_run.log
 *
 * Analyse RAM from log:
 *   python3 ram_analyzer.py   (select rdf_v11_run.log via file browser)
 */

#ifndef ROUTING_CONF_RPL_LITE
#define ROUTING_CONF_RPL_LITE    0
#endif
#ifndef ROUTING_CONF_RPL_CLASSIC
#define ROUTING_CONF_RPL_CLASSIC 0
#endif

/* =========================================================================
 * PARAMETERS  (identical to v10 — override via CFLAGS if needed)
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
 * STACK PAINTING — runtime peak stack measurement
 *
 * How it works:
 *   1. At boot (before any init), fill the entire free RAM region between
 *      _ebss (end of .bss) and _estack (top of RAM) with 0xAA pattern.
 *   2. As the program runs, function calls push stack frames DOWNWARD from
 *      _estack, overwriting the 0xAA pattern.
 *   3. To measure peak usage, scan UPWARD from _ebss until we find the
 *      first byte that is NOT 0xAA — that is the deepest stack point ever
 *      reached (the "high-water mark").
 *   4. peak_stack_bytes = _estack - first_non_pattern_address
 *
 * Why this works without linker script changes:
 *   _ebss and _estack are ALREADY defined in stm32f103rey6.ld:
 *     _ebss   = end of .bss section  (line 142 of linker script)
 *     _estack = 0x20010000           (line 25 of linker script)
 *   We just declare them as extern symbols — the linker resolves them.
 *
 * Safety:
 *   - We only paint the FREE region (above .bss, below stack pointer).
 *   - The paint happens before Contiki initialises anything, so no live
 *     data is overwritten.
 *   - A 64-byte guard zone is left at the very top to avoid touching the
 *     initial reset stack frame.
 * =========================================================================
 */

/* Declare linker script symbols — already in stm32f103rey6.ld, no changes needed */
extern uint32_t _ebss;     /* end of .bss — bottom of free RAM              */
extern uint32_t _estack;   /* top of RAM = 0x20010000 — stack grows DOWN    */

#define STACK_PAINT_PATTERN  ((uint8_t)0xAA)

/*
 * stack_get_sp() — read current ARM Cortex-M3 Stack Pointer register.
 * Inlined so it returns the SP of the CALLER, not this function's frame.
 */
static inline uint32_t __attribute__((always_inline))
stack_get_sp(void)
{
  uint32_t sp;
  __asm__ volatile ("mov %0, sp" : "=r" (sp));
  return sp;
}

/*
 * stack_paint() — fill the FREE region between _ebss and current SP
 *                 with a known pattern (0xAA).
 *
 * FIX vs original: we read the live Stack Pointer and paint ONLY the
 * region BELOW it (i.e. memory not yet touched by any stack frame).
 * Painting above SP would overwrite the active call stack → Hard Fault.
 *
 * We add a 128-byte safety margin below SP to account for the few
 * stack bytes used by this function call itself and any IRQ that
 * might fire during the paint loop.
 *
 * Call from PROCESS_THREAD after PROCESS_BEGIN() — safe at any point.
 */
#define STACK_PAINT_SP_MARGIN  128u   /* safety gap below live SP */

static void
stack_paint(void)
{
  volatile uint8_t *bottom = (volatile uint8_t *)(&_ebss);
  /* Stop painting well below the current stack pointer */
  volatile uint8_t *top    = (volatile uint8_t *)(stack_get_sp() - STACK_PAINT_SP_MARGIN);
  volatile uint8_t *p;

  if(top <= bottom) {
    LOG_WARN("STACK_PAINT: no safe region to paint "
             "(bss_end=0x%08lx sp_safe=0x%08lx)\n",
             (unsigned long)bottom, (unsigned long)top);
    return;
  }

  for(p = bottom; p < top; p++) {
    *p = STACK_PAINT_PATTERN;
  }

  LOG_INFO("STACK_PAINT painted=%lu bytes from 0x%08lx to 0x%08lx "
           "(sp=0x%08lx estack=0x%08lx)\n",
           (unsigned long)(top - bottom),
           (unsigned long)bottom,
           (unsigned long)top,
           (unsigned long)stack_get_sp(),
           (unsigned long)(&_estack));
}

/*
 * stack_measure_peak() — scan painted region, return peak stack bytes.
 *
 * Scans UPWARD from _ebss until the first byte that is NOT 0xAA.
 * Everything from that address up to _estack was touched by the stack.
 * peak_stack = _estack - first_non_painted_address
 *
 * This is safe to call at any time — it only reads memory, never writes.
 */
static uint32_t
stack_measure_peak(void)
{
  volatile uint8_t *bottom = (volatile uint8_t *)(&_ebss);
  volatile uint8_t *estack = (volatile uint8_t *)(&_estack);
  volatile uint8_t *p;

  if(estack <= bottom) {
    return 0;
  }

  /* Scan upward from bottom of free RAM.
   * The first non-0xAA byte is the lowest address the stack ever reached. */
  for(p = bottom; p < estack; p++) {
    if(*p != STACK_PAINT_PATTERN) {
      /* Stack reached this address — peak = distance from here to _estack */
      return (uint32_t)(estack - p);
    }
  }

  /* All bytes still painted — stack never reached the painted region.
   * This means peak stack < STACK_PAINT_SP_MARGIN (very shallow). */
  return 0;
}

/* =========================================================================
 * STATIC RAM BREAKDOWN — computed at compile time from sizeof()
 *
 * This gives the exact .bss contribution of each RDF array.
 * Add these up and compare with arm-none-eabi-size output to verify.
 * =========================================================================
 */
#define APP_STATIC_RAM_BYTES  ( \
  sizeof(last_seq_seen)  +  /* MAX_NODES * 2                          */ \
  sizeof(seen)           +  /* MAX_NODES * MAX_SEQ_TRACK (dominant!)  */ \
  sizeof(rdf_state)      +  /* MAX_NODES * sizeof(rdf_state_t)        */ \
  sizeof(recv_count)     +  /* MAX_NODES * 4                          */ \
  sizeof(fwd_count)      +  /* MAX_NODES * 4                          */ \
  sizeof(slot_to_srcid)     /* MAX_NODES * 2                          */ \
)

/* =========================================================================
 * Board name lookup
 * =========================================================================
 */
typedef struct { uint16_t node_id; const char *name; } board_name_t;
static const board_name_t board_names[] = {
  { 37252, "m3-36" },
  { 38281, "m3-38" },
  { 42886, "m3-34" },
  { 43396, "m3-23" },
  { 45190, "m3-30" },
  { 45703, "m3-32" },
  { 0,     NULL    }   /* sentinel */
};
static const char *
board_name(uint16_t id)
{
  const board_name_t *p;
  for(p = board_names; p->name != NULL; p++) {
    if(p->node_id == id) { return p->name; }
  }
  return "m3-??";
}

/* =========================================================================
 * Packet format
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
 * Per-source RDF state
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

static uint16_t      slot_to_srcid[MAX_NODES];
static uint16_t      my_slot = 0;

PROCESS(rdf_process, "RDF Flooding v11");
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
 * safe_hw_slot — linear probing hash table
 * =========================================================================
 */
static uint16_t
safe_hw_slot(uint16_t src_id)
{
  uint16_t slot = HW_SLOT(src_id), probe;
  for(probe = 0; probe < MAX_NODES; probe++) {
    uint16_t candidate = (slot + probe) % MAX_NODES;
    if(slot_to_srcid[candidate] == 0) {
      if(probe > 0) {
        LOG_INFO("HW_SLOT probe: src=%s natural_slot=%u assigned_slot=%u\n",
                 board_name(src_id), slot, candidate);
      }
      slot_to_srcid[candidate] = src_id;
      return candidate;
    }
    if(slot_to_srcid[candidate] == src_id) { return candidate; }
  }
  LOG_WARN("HW_SLOT table full: cannot assign slot for src=%s\n", board_name(src_id));
  return 0xFFFF;
}

/* =========================================================================
 * init_node_coordinates
 * =========================================================================
 */
static void
init_node_coordinates(void)
{
  my_x_m = (int16_t)((my_slot % COORD_GRID_COLS) * COORD_SPACING_M);
  my_y_m = (int16_t)((my_slot / COORD_GRID_COLS) * COORD_SPACING_M);
  LOG_INFO("INIT node=%s hw_slot=%u fallback_xy=(%d,%d)\n",
           board_name(node_id), my_slot, (int)my_x_m, (int)my_y_m);
}

/* =========================================================================
 * rdf_decay_interval
 * =========================================================================
 */
static clock_time_t
rdf_decay_interval(uint16_t hops)
{
  uint32_t h_pow = 1, q;
  if(hops == 0) { return 0; }
  for(q = 0; q < (uint32_t)RDF_Q; q++) {
    if(h_pow > (0xFFFFFFFFUL / (uint32_t)hops)) { return (clock_time_t)0xFFFFFFFFUL; }
    h_pow *= (uint32_t)hops;
  }
  if(h_pow > (0xFFFFFFFFUL / (uint32_t)SEND_INTERVAL)) { return (clock_time_t)0xFFFFFFFFUL; }
  return (clock_time_t)((uint32_t)SEND_INTERVAL * h_pow);
}

/* =========================================================================
 * distance_based_wait
 * =========================================================================
 */
static clock_time_t
distance_based_wait(const flood_msg_t *msg)
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
static void
cancel_all_timers(rdf_state_t *st)
{
  ctimer_stop(&st->cbf_timer);
  ctimer_stop(&st->rdf_timer);
  st->phase       = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase2_callback — Phase 2: RDF wait complete → TX
 * =========================================================================
 */
static void
rdf_phase2_callback(void *ptr)
{
  rdf_state_t *st = (rdf_state_t *)ptr;
  flood_msg_t *msg;
  uint16_t     slot;

  if(st == NULL || !st->has_pending || st->phase != 2) {
    if(st) { st->phase = 0; st->has_pending = 0; }
    return;
  }
  msg = &st->pending_msg;
  msg->hop_count++;
  msg->sender_x_m = my_x_m;
  msg->sender_y_m = my_y_m;

  LOG_INFO("FWD src=%s seq=%u hop=%u sender_xy=(%d,%d)\n",
           board_name(msg->src_id), msg->seq_no, msg->hop_count,
           (int)my_x_m, (int)my_y_m);

  simple_udp_sendto(&udp_conn, msg, sizeof(*msg), &mcast_addr);

  if(valid_src(msg->src_id)) {
    slot = safe_hw_slot(msg->src_id);
    if(slot != 0xFFFF) { fwd_count[slot]++; }
  }
  st->phase       = 0;
  st->has_pending = 0;
}

/* =========================================================================
 * rdf_phase1_callback — Phase 1: CBF wait complete → start Phase 2
 * =========================================================================
 */
static void
rdf_phase1_callback(void *ptr)
{
  rdf_state_t  *st = (rdf_state_t *)ptr;
  clock_time_t  rdf_wait;

  if(st == NULL || !st->has_pending || st->phase != 1) {
    if(st) { st->phase = 0; st->has_pending = 0; }
    return;
  }
  rdf_wait = rdf_decay_interval(st->pending_msg.hop_count);

  LOG_INFO("CBF_DONE src=%s seq=%u hop=%u rdf_wait_ticks=%lu\n",
           board_name(st->pending_msg.src_id), st->pending_msg.seq_no,
           st->pending_msg.hop_count, (unsigned long)rdf_wait);

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
      LOG_INFO("CBF_SUPPRESS src=%s seq=%u arriving_hop=%u pending_hop=%u\n",
               board_name(src), seq,
               (unsigned)arriving_hop, (unsigned)st->pending_msg.hop_count);
    } else {
      LOG_INFO("RDF_SUPPRESS src=%s seq=%u arriving_hop=%u pending_hop=%u period=%s\n",
               board_name(src), seq,
               (unsigned)arriving_hop, (unsigned)st->pending_msg.hop_count, phase_label);
    }
    return 1;
  }
  if(phase_label[0] == 'c') {
    LOG_INFO("CBF_HOP_IGNORE src=%s seq=%u arriving_hop=%u pending_hop=%u\n",
             board_name(src), seq,
             (unsigned)arriving_hop, (unsigned)st->pending_msg.hop_count);
  } else {
    LOG_INFO("RDF_HOP_IGNORE src=%s seq=%u arriving_hop=%u pending_hop=%u period=%s\n",
             board_name(src), seq,
             (unsigned)arriving_hop, (unsigned)st->pending_msg.hop_count, phase_label);
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
    LOG_INFO("PENDING_UPDATE src=%s seq=%u old_seq=%u new_seq=%u "
             "old_hop=%u new_hop=%u timer_kept=1 period=%s\n",
             board_name(src), seq, st->pending_seq, seq,
             (unsigned)st->pending_msg.hop_count, (unsigned)msg->hop_count,
             phase_label);
    st->pending_msg = *msg;
    st->pending_seq = seq;
    return 0;
  }
  LOG_INFO("PENDING_DROP_STALE src=%s seq=%u buffered_seq=%u stale_seq=%u period=%s\n",
           board_name(src), seq, st->pending_seq, seq, phase_label);
  return 0;
}

/* =========================================================================
 * start_cbf_phase
 * =========================================================================
 */
static void
start_cbf_phase(uint16_t src, flood_msg_t *msg, uint16_t slot)
{
  rdf_state_t  *st;
  clock_time_t  dist_wait, jitter;
  int32_t       dx, dy;
  uint32_t      dist2;
  uint16_t      dist_m;

  (void)src;
  st = &rdf_state[slot];

  jitter = RDF_MIN_JITTER;
  if(RDF_MAX_JITTER > RDF_MIN_JITTER) {
    jitter += (clock_time_t)(random_rand() %
               (uint16_t)(RDF_MAX_JITTER - RDF_MIN_JITTER + 1));
  }
  dist_wait = distance_based_wait(msg);

  dx    = (int32_t)my_x_m - (int32_t)msg->sender_x_m;
  dy    = (int32_t)my_y_m - (int32_t)msg->sender_y_m;
  dist2 = (uint32_t)(dx * dx + dy * dy);
  for(dist_m = 0;
      (uint32_t)dist_m * (uint32_t)dist_m < dist2 && dist_m < 255;
      dist_m++) {}

  LOG_INFO("CBF_TIMER src=%s seq=%u hop=%u sender_xy=(%d,%d) my_xy=(%d,%d) "
           "dist_m=%u dist_wait_ticks=%lu jitter_ticks=%lu total_ticks=%lu\n",
           board_name(msg->src_id), msg->seq_no, msg->hop_count,
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
 * log_dissemination
 * =========================================================================
 */
static void
log_dissemination(uint16_t src, uint16_t seq, uint16_t slot)
{
  LOG_INFO("DISSEM src=%s seq=%u node=%s recv_by_me=%" PRIu32 " last_seq=%u\n",
           board_name(src), seq, board_name(node_id),
           recv_count[slot], last_seq_seen[slot]);
}

/* =========================================================================
 * count_active_nodes — count nodes actually seen in network
 *
 * Counts non-zero entries in slot_to_srcid[] (populated by safe_hw_slot()
 * on first packet from each source). This is the number of DISTINCT other
 * nodes that have sent at least one packet received by this node.
 *
 * NOTE: This count is per-node (what THIS node has heard).
 *       It does NOT include this node itself.
 *       Total network size = active_nodes_heard + 1 (self).
 *
 * Why static RAM does NOT depend on this count:
 *   Arrays are allocated for MAX_NODES slots at compile time.
 *   Whether 2 or 50 nodes are active, the same RAM is consumed.
 *   Only peak_stack changes slightly with more nodes (more callbacks).
 * =========================================================================
 */
static uint16_t
count_active_nodes(void)
{
  uint16_t slot, count = 0;
  for(slot = 0; slot < MAX_NODES; slot++) {
    if(slot_to_srcid[slot] != 0 && slot_to_srcid[slot] != node_id) {
      count++;
    }
  }
  return count;
}

/* =========================================================================
 * log_stats_summary — RDF stats + RAM_REPORT
 * =========================================================================
 */
static void
log_stats_summary(void)
{
  uint16_t slot;
  uint16_t active_nodes;
  uint32_t peak_stack;
  uint32_t app_static;
  uint32_t total_ram_est;

  /* ── RDF per-source stats (unchanged from v10) ── */
  for(slot = 0; slot < MAX_NODES; slot++) {
    if(recv_count[slot] == 0 && fwd_count[slot] == 0) { continue; }
    LOG_INFO("STATS node=%s src=%s recv=%" PRIu32 " exp=%" PRIu32 " fwd=%" PRIu32 "\n",
             board_name(node_id),
             board_name(slot_to_srcid[slot]),
             recv_count[slot],
             (uint32_t)last_seq_seen[slot] + 1,
             fwd_count[slot]);
  }
  LOG_INFO("STATS node=%s own_tx=%" PRIu32 "\n",
           board_name(node_id), own_tx_count);

  /* ── Node count ── */
  active_nodes = count_active_nodes();

  /*
   * NODE_COUNT log line:
   *   active_nodes = distinct other nodes heard by THIS node (heard ≥1 packet)
   *   network_size = active_nodes + 1 (includes self)
   *   max_nodes    = compile-time capacity (MAX_NODES)
   *   slots_used   = active_nodes (same, for clarity)
   *   slots_free   = MAX_NODES - 1 - active_nodes (remaining capacity)
   *
   * IMPORTANT: static RAM (seen[][], rdf_state[], etc.) is ALWAYS
   * allocated for MAX_NODES slots regardless of active_nodes.
   * RAM does NOT shrink if fewer nodes are present.
   * Only peak_stack varies slightly with traffic load.
   */
  LOG_INFO("NODE_COUNT node=%s active_heard=%u network_size=%u "
           "max_nodes=%u slots_used=%u slots_free=%u\n",
           board_name(node_id),
           (unsigned)active_nodes,
           (unsigned)(active_nodes + 1),   /* +1 for self */
           (unsigned)MAX_NODES,
           (unsigned)active_nodes,
           (unsigned)(MAX_NODES - 1 - active_nodes));

  /* ── RAM measurement ── */
  peak_stack    = stack_measure_peak();
  app_static    = APP_STATIC_RAM_BYTES;
  total_ram_est = app_static + peak_stack;

  /*
   * RAM_REPORT format (parsed by ram_analyzer.py):
   *   RAM_REPORT node=<name> max_nodes=<N> active_nodes=<A> network_size=<S>
   *              app_static=<bytes> peak_stack=<bytes> total=<bytes>
   *              seen_bytes=<bytes> rdf_state_bytes=<bytes>
   *
   * app_static   = sum of all RDF global arrays (.bss contribution)
   *                Does NOT include Contiki-NG OS .bss — use arm-none-eabi-size
   * peak_stack   = high-water mark of stack usage since boot (bytes)
   * total        = app_static + peak_stack (app RAM estimate)
   * active_nodes = nodes actually heard (varies at runtime)
   * network_size = active_nodes + 1 (includes self)
   *
   * Key insight: app_static is FIXED by MAX_NODES at compile time.
   * active_nodes tells you how much of that capacity is actually used.
   *
   * To get TOTAL system RAM:
   *   system_total = arm-none-eabi-size(.data + .bss) + peak_stack
   */
  LOG_INFO("RAM_REPORT node=%s max_nodes=%u active_nodes=%u network_size=%u "
           "app_static=%lu peak_stack=%lu total=%lu "
           "seen_bytes=%lu rdf_state_bytes=%lu\n",
           board_name(node_id),
           (unsigned)MAX_NODES,
           (unsigned)active_nodes,
           (unsigned)(active_nodes + 1),
           (unsigned long)app_static,
           (unsigned long)peak_stack,
           (unsigned long)total_ram_est,
           (unsigned long)sizeof(seen),
           (unsigned long)sizeof(rdf_state));
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
  uint16_t     src, seq, slot;
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
  if(msg.hop_count > 20) {
    LOG_WARN("RX sanity fail: src=%s seq=%u hop=%u too large, dropping\n",
             board_name(src), seq, msg.hop_count);
    return;
  }

  slot = safe_hw_slot(src);
  if(slot == 0xFFFF) { return; }

  is_new = !seen[slot][seq % MAX_SEQ_TRACK];

  LOG_INFO("ARRIVE src=%s seq=%u hop=%u sender_xy=(%d,%d) is_new=%u\n",
           board_name(src), seq, msg.hop_count,
           (int)msg.sender_x_m, (int)msg.sender_y_m, (unsigned)is_new);

  st          = &rdf_state[slot];
  phase_label = (st->phase == 1) ? "cbf" : "rdf_decay";

  if(!is_new) {
    if(st->has_pending) {
      handle_arrival_during_wait(st, src, &msg, phase_label);
    }
    return;
  }

  seen[slot][seq % MAX_SEQ_TRACK] = 1;
  recv_count[slot]++;
  if(seq > last_seq_seen[slot]) { last_seq_seen[slot] = seq; }

  LOG_INFO("RX src=%s seq=%u hop=%u src_xy=(%d,%d) sender_xy=(%d,%d) my_xy=(%d,%d)\n",
           board_name(src), seq, msg.hop_count,
           (int)msg.src_x_m,    (int)msg.src_y_m,
           (int)msg.sender_x_m, (int)msg.sender_y_m,
           (int)my_x_m,         (int)my_y_m);

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
  static struct etimer periodic_timer, stats_timer;
  flood_msg_t msg;
  uint16_t    i, j;

  PROCESS_BEGIN();

  /* ── STEP 1: Paint stack FIRST, before any other init ── */
  stack_paint();

  /* ── STEP 2: Assign own slot ── */
  my_slot = safe_hw_slot(node_id);
  if(my_slot == 0xFFFF) {
    LOG_WARN("my_slot: table full at boot, using slot 0\n");
    my_slot = 0;
  }

  /* ── STEP 3: Coordinates ── */
  init_node_coordinates();

  /* ── STEP 4: Zero all arrays ── */
  for(i = 0; i < MAX_NODES; i++) {
    last_seq_seen[i] = 0;
    recv_count[i]    = 0;
    fwd_count[i]     = 0;
    slot_to_srcid[i] = 0;
    for(j = 0; j < MAX_SEQ_TRACK; j++) { seen[i][j] = 0; }
    rdf_state[i].phase       = 0;
    rdf_state[i].has_pending = 0;
    rdf_state[i].pending_seq = 0;
  }

  /* ── STEP 5: Network setup ── */
  simple_udp_register(&udp_conn, UDP_PORT, NULL, UDP_PORT, udp_rx_callback);
  uip_create_linklocal_allnodes_mcast(&mcast_addr);

  /* ── STEP 6: Timers ── */
  etimer_set(&stats_timer,    STATS_TICKS);
  etimer_set(&periodic_timer, random_rand() % SEND_INTERVAL);

  LOG_INFO("START node=%s send_interval_ticks=%lu max_nodes=%u max_seq=%u\n",
           board_name(node_id), (unsigned long)SEND_INTERVAL,
           (unsigned)MAX_NODES, (unsigned)MAX_SEQ_TRACK);

  /* ── STEP 7: Log compile-time RAM breakdown immediately ── */
  LOG_INFO("RAM_STATIC node=%s max_nodes=%u max_seq=%u "
           "last_seq_seen=%u seen=%lu rdf_state=%lu "
           "recv_count=%u fwd_count=%u slot_map=%u app_total=%lu\n",
           board_name(node_id),
           (unsigned)MAX_NODES,
           (unsigned)MAX_SEQ_TRACK,
           (unsigned)sizeof(last_seq_seen),
           (unsigned long)sizeof(seen),
           (unsigned long)sizeof(rdf_state),
           (unsigned)sizeof(recv_count),
           (unsigned)sizeof(fwd_count),
           (unsigned)sizeof(slot_to_srcid),
           (unsigned long)APP_STATIC_RAM_BYTES);

  /* ── Main loop ── */
  while(1) {
    PROCESS_WAIT_EVENT();

    if(etimer_expired(&stats_timer)) {
      log_stats_summary();   /* includes RAM_REPORT */
      etimer_reset(&stats_timer);
    }

    if(etimer_expired(&periodic_timer)) {
      msg.src_id     = node_id;
      msg.seq_no     = my_seq;
      msg.hop_count  = 0;
      msg.src_x_m    = my_x_m;
      msg.src_y_m    = my_y_m;
      msg.sender_x_m = my_x_m;
      msg.sender_y_m = my_y_m;

      seen[my_slot][my_seq % MAX_SEQ_TRACK] = 1;
      if(my_seq > last_seq_seen[my_slot]) { last_seq_seen[my_slot] = my_seq; }

      LOG_INFO("TX src=%s seq=%u xy=(%d,%d)\n",
               board_name(node_id), my_seq, (int)my_x_m, (int)my_y_m);

      simple_udp_sendto(&udp_conn, &msg, sizeof(msg), &mcast_addr);
      own_tx_count++;
      my_seq++;

      etimer_set(&periodic_timer,
                 SEND_INTERVAL - RDF_MIN_JITTER +
                 (random_rand() % (2 * RDF_MIN_JITTER + 1)));
    }
  }

  PROCESS_END();
}

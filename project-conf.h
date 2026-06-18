#ifndef PROJECT_CONF_H_
#define PROJECT_CONF_H_

/* =========================================================
 * MAC-layer drop and debug logging
 * LOG_LEVEL_DBG = full CSMA internals: queue ops, backoff,
 *                 per-packet tx status, drop reasons
 * LOG_LEVEL_WARN = only queue full / alloc failures (quieter)
 * ========================================================= */
#define LOG_CONF_LEVEL_MAC     LOG_LEVEL_WARN
#define LOG_CONF_LEVEL_FRAMER  LOG_LEVEL_WARN

/* Optional: also see IP/6LoWPAN framing if needed */
/* #define LOG_CONF_LEVEL_6LOWPAN  LOG_LEVEL_DBG */
/* #define LOG_CONF_LEVEL_IPV6     LOG_LEVEL_WARN */

/* =========================================================
 * CSMA tuning (informational - broadcasts ignore retries,
 * but these values appear in backoff logs)
 * Default: MAX_FRAME_RETRIES=7, MAX_BACKOFF=5
 * ========================================================= */
/* #define CSMA_CONF_MAX_FRAME_RETRIES  3  */  /* unicast only */
/* #define CSMA_CONF_MAX_BACKOFF        3  */  /* reduce for less backoff */

#endif /* PROJECT_CONF_H_ */


/*run 
make distclean   
make TARGET=cooja 2>&1 | grep -i "project-conf"*/

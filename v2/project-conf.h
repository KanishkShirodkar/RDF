/*
 * project-conf.h -- Contiki-NG project configuration for rdf-flood
 *
 * Picked up automatically when:
 *   CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"
 * is set in the Makefile.
 *
 * WHY THIS FILE IS NEEDED:
 *   ROUTING_CONF_RPL_LITE=0 defined only inside rdf-flood-v2.c is too late --
 *   Contiki's build system includes routing headers before your .c file.
 *   This file is seen by ALL translation units before any Contiki header.
 *
 * NOTE on redefinition errors:
 *   The Makefile already passes -DROUTING_CONF_RPL_LITE=0 via CFLAGS.
 *   Do NOT repeat those defines here with plain #define -- that causes
 *   "redefined [-Werror]" because the -D flag and the #define both fire.
 *   Use #ifndef guards so the Makefile CFLAGS always win.
 *
 *   UIP_CONF_IPV6_RPL is a DERIVED macro in contiki-default-conf.h:
 *     #define UIP_CONF_IPV6_RPL (ROUTING_CONF_RPL_LITE || ROUTING_CONF_RPL_CLASSIC)
 *   Do NOT define it manually -- it will conflict. It becomes 0 automatically
 *   once ROUTING_CONF_RPL_LITE=0 and ROUTING_CONF_RPL_CLASSIC=0 are set.
 */

#ifndef PROJECT_CONF_H_
#define PROJECT_CONF_H_

/* Disable RPL Lite -- guarded so Makefile -D flag takes precedence */
#ifndef ROUTING_CONF_RPL_LITE
#define ROUTING_CONF_RPL_LITE    0
#endif

/* Disable RPL Classic -- guarded so Makefile -D flag takes precedence */
#ifndef ROUTING_CONF_RPL_CLASSIC
#define ROUTING_CONF_RPL_CLASSIC 0
#endif

/* NOTE: Do NOT define UIP_CONF_IPV6_RPL here.
 * contiki-default-conf.h derives it as:
 *   (ROUTING_CONF_RPL_LITE || ROUTING_CONF_RPL_CLASSIC)
 * which will correctly evaluate to 0 once the above are 0. */

#endif /* PROJECT_CONF_H_ */

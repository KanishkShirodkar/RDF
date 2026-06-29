CONTIKI_PROJECT = rdf-flood

ifndef CONTIKI_ROOT
  CONTIKI_ROOT = ../..
endif

MAKE_ROUTING = MAKE_ROUTING_NULLROUTING

CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"

CONTIKI_SOURCEFILES += rdf-flood-v7.c

MODULES += os/net/ipv6
MODULES += os/net/ipv6/simple-udp
MODULES += os/net/app-layer

include $(CONTIKI_ROOT)/Makefile.include
CONTIKI_PROJECT = rdf-flood-HW-v11

all: $(CONTIKI_PROJECT)

CONTIKI = ../..

MAKE_ROUTING = MAKE_ROUTING_NULLROUTING

CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"

MODULES += os/net/ipv6
MODULES += os/net/ipv6/simple-udp
MODULES += os/net/app-layer

include $(CONTIKI)/Makefile.include

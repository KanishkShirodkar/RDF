CONTIKI_PROJECT = rdf-flood
all: $(CONTIKI_PROJECT)

MAKE_ROUTING = MAKE_ROUTING_NULLROUTING

CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"
CFLAGS += -DROUTING_CONF_RPL_LITE=0
CFLAGS += -DROUTING_CONF_RPL_CLASSIC=0
CFLAGS += -Wno-error
CFLAGS += -Wno-implicit-function-declaration

CONTIKI = ../..
include $(CONTIKI)/Makefile.include

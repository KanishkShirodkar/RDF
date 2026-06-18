# Makefile for rdf-flood -- Contiki-NG / COOJA
#
# Place this folder inside contiki-ng/examples/rdf-flood/
# then run:
#   make TARGET=cooja
#   make TARGET=cooja clean
#
# Override parameters from command line (example):
#   make TARGET=cooja CFLAGS+="-DRDF_Q=3"
#
# Set CONTIKI_ROOT if contiki-ng is not two directories up:
#   make TARGET=cooja CONTIKI_ROOT=/home/user/contiki-ng

CONTIKI_PROJECT = rdf-flood

ifndef CONTIKI_ROOT
  CONTIKI_ROOT = ../..
endif

# Tell Contiki-NG to use our project-conf.h in this folder
CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"

# Disable RPL at the Contiki build-system level.
MAKE_WITH_ROUTING = 0

CONTIKI_SOURCEFILES += rdf-flood.c

MODULES += os/net/ipv6
MODULES += os/net/ipv6/simple-udp
MODULES += os/net/app-layer

include $(CONTIKI_ROOT)/Makefile.include
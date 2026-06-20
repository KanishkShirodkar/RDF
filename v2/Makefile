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

# ---- RPL DISABLE ----
#
# Layer 1: Contiki build system -- do not compile RPL module at all.
MAKE_WITH_ROUTING = 0
#
# Layer 2: project-conf.h -- single source of truth for RPL defines.
#          The #ifndef guards in project-conf.h prevent any redefinition.
#          Do NOT add -DROUTING_CONF_RPL_LITE=0 here as well -- Cooja's
#          own build command already injects defines via <command-line>
#          and a second definition causes "redefined [-Werror]".
CFLAGS += -DPROJECT_CONF_H=\"project-conf.h\"

CONTIKI_SOURCEFILES += rdf-flood-v2.c

MODULES += os/net/ipv6
MODULES += os/net/ipv6/simple-udp
MODULES += os/net/app-layer

include $(CONTIKI_ROOT)/Makefile.include
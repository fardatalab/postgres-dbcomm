DOCA_SAMPLES_DIR ?= /opt/mellanox/doca/samples
PKG_CONFIG ?= pkg-config
CC ?= gcc

PKGS := doca-dma doca-common
COMMON_SRC := $(DOCA_SAMPLES_DIR)/common.c

CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Wno-deprecated-declarations -D_GNU_SOURCE
CFLAGS += -I$(DOCA_SAMPLES_DIR) $(shell $(PKG_CONFIG) --cflags $(PKGS))
LDLIBS += $(shell $(PKG_CONFIG) --libs $(PKGS))

.PHONY: all clean

all: doca_homer_validation

doca_homer_validation: doca_homer_validation.c doca_homer_validation_common.h $(COMMON_SRC)
	$(CC) $(CFLAGS) doca_homer_validation.c $(COMMON_SRC) -o $@ $(LDLIBS)

clean:
	rm -f doca_homer_validation

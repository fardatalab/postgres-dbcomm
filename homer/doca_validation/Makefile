DOCA_SAMPLES_DIR ?= /opt/mellanox/doca/samples
PKG_CONFIG ?= pkg-config
CC ?= gcc

PKGS := doca-dma doca-rdma doca-common
COMMON_SRC := $(DOCA_SAMPLES_DIR)/common.c
VERBS_CFLAGS := $(shell $(PKG_CONFIG) --cflags libibverbs 2>/dev/null)
VERBS_LDLIBS := $(shell $(PKG_CONFIG) --libs libibverbs 2>/dev/null || echo -libverbs)

CFLAGS ?= -O2 -g
CFLAGS += -std=gnu11 -Wall -Wextra -Wno-deprecated-declarations -D_GNU_SOURCE
CFLAGS += -I$(DOCA_SAMPLES_DIR) $(shell $(PKG_CONFIG) --cflags $(PKGS))
LDLIBS += $(shell $(PKG_CONFIG) --libs $(PKGS))

.PHONY: all clean

all: doca_homer_validation doca_pci_rdma_bridge_validation ibverbs_rdma_write_validation \
	doca_dma_ibverbs_rdma_validation

doca_homer_validation: doca_homer_validation.c doca_homer_validation_common.h $(COMMON_SRC)
	$(CC) $(CFLAGS) doca_homer_validation.c $(COMMON_SRC) -o $@ $(LDLIBS)

doca_pci_rdma_bridge_validation: doca_pci_rdma_bridge_validation.c $(COMMON_SRC)
	$(CC) $(CFLAGS) doca_pci_rdma_bridge_validation.c $(COMMON_SRC) -o $@ $(LDLIBS)

ibverbs_rdma_write_validation: ibverbs_rdma_write_validation.c
	$(CC) $(CFLAGS) $(VERBS_CFLAGS) ibverbs_rdma_write_validation.c -o $@ $(VERBS_LDLIBS)

doca_dma_ibverbs_rdma_validation: doca_dma_ibverbs_rdma_validation.c $(COMMON_SRC)
	$(CC) $(CFLAGS) $(VERBS_CFLAGS) doca_dma_ibverbs_rdma_validation.c $(COMMON_SRC) -o $@ $(LDLIBS) $(VERBS_LDLIBS)

clean:
	rm -f doca_homer_validation doca_pci_rdma_bridge_validation ibverbs_rdma_write_validation \
		doca_dma_ibverbs_rdma_validation

# Build the x11vnc-nvfbc benchmark harness.
#
# NVFBC.h comes from the x11vnc fork this harness measures.  Override if the
# checkout lives elsewhere:  make NVFBC_INC=/path/to/src/nvfbc

NVFBC_INC ?= ../x11vnc/src/nvfbc
CFLAGS    ?= -O2 -Wall

BINS = loadgen nvfloor diffcheck pollmode

all: $(BINS)

loadgen: loadgen.c
	$(CC) $(CFLAGS) -o $@ $< -lX11 -lXext

nvfloor: nvfloor.c
	$(CC) $(CFLAGS) -I$(NVFBC_INC) -o $@ $< -ldl

diffcheck: diffcheck.c
	$(CC) $(CFLAGS) -I$(NVFBC_INC) -o $@ $< -ldl

pollmode: pollmode.c
	$(CC) $(CFLAGS) -I$(NVFBC_INC) -o $@ $< -ldl

clean:
	rm -f $(BINS)
	rm -rf __pycache__

.PHONY: all clean

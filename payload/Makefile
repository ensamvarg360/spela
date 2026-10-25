ifdef PS5_PAYLOAD_SDK
    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk
else
    $(error PS5_PAYLOAD_SDK is undefined)
endif

# The helper's version is the app's version, so the app can tell whether the running helper matches.
VERSION := $(shell sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' ../backend/app/__init__.py)
ELF := ../backend/app/payload/spela.elf
LZ4_DIR ?= /tmp/lz4

CFLAGS := -Wall -Werror -O2 -DVERSION=\"$(VERSION)\" -Ie2a -I$(LZ4_DIR)
# SQLite's amalgamation (build.ps1 downloads it to SQLITE_DIR): set_logo() edits the home screen's database with it.
SQLITE_DIR ?= /tmp/sqlite
SQLITE_FLAGS := -O2 -DSQLITE_THREADSAFE=1 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_OMIT_DEPRECATED -DSQLITE_DEFAULT_MEMSTATUS=0
# LZ4 packing on the console (pack_start()): exFAT2AMPR's packer in e2a/, as its author builds it, with lz4 (build.ps1
# downloads it to LZ4_DIR) and the AMPR emulator the PC app uses (backend/app/assets/libSceAmpr.sprx) built in.
E2A_FLAGS := -O2 -std=gnu11 -Ie2a -I$(LZ4_DIR) -DE2A_VERSION=\"spela-$(VERSION)\" -DE2A_STATE_DIR=\"/data/spela/lz4\" -DLOG_JSON_CAP=20000
E2A_OBJS := e2a/scan.o e2a/ampr_pack.o e2a/ampr_index.o e2a/verify.o e2a/util.o e2a/sha256.o lz4.o lz4hc.o sprx.o

$(ELF): main.c sqlite3.o $(E2A_OBJS) ../backend/app/__init__.py
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -I$(SQLITE_DIR) -o $@ main.c sqlite3.o $(E2A_OBJS) -lkernel_sys

sqlite3.o: $(SQLITE_DIR)/sqlite3.c
	$(CC) $(SQLITE_FLAGS) -c -o $@ $<

e2a/%.o: e2a/%.c e2a/*.h
	$(CC) $(E2A_FLAGS) -c -o $@ $<

lz4.o lz4hc.o: %.o: $(LZ4_DIR)/%.c
	$(CC) -O2 -I$(LZ4_DIR) -c -o $@ $<

sprx.c: ../backend/app/assets/libSceAmpr.sprx
	cp $< libSceAmpr.sprx && xxd -i libSceAmpr.sprx > $@ && rm libSceAmpr.sprx

sprx.o: sprx.c
	$(CC) -O2 -c -o $@ $<

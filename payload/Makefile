ifdef PS5_PAYLOAD_SDK
    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk
else
    $(error PS5_PAYLOAD_SDK is undefined)
endif

# The helper's version is the app's version, so the app can tell whether the running helper matches.
VERSION := $(shell sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' ../backend/app/__init__.py)
ELF := ../backend/app/payload/storefront-helper.elf

CFLAGS := -Wall -Werror -O2 -DVERSION=\"$(VERSION)\"
# SQLite's amalgamation (build.ps1 downloads it to SQLITE_DIR): set_logo() edits the home screen's database with it.
SQLITE_DIR ?= /tmp/sqlite
SQLITE_FLAGS := -O2 -DSQLITE_THREADSAFE=1 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_OMIT_DEPRECATED -DSQLITE_DEFAULT_MEMSTATUS=0

$(ELF): main.c sqlite3.o ../backend/app/__init__.py
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -I$(SQLITE_DIR) -o $@ main.c sqlite3.o -lkernel_sys

sqlite3.o: $(SQLITE_DIR)/sqlite3.c
	$(CC) $(SQLITE_FLAGS) -c -o $@ $<

ifdef PS5_PAYLOAD_SDK
    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk
else
    $(error PS5_PAYLOAD_SDK is undefined)
endif

# The helper's version is the app's version, so the app can tell whether the running helper matches.
VERSION := $(shell sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' ../backend/app/__init__.py)
ELF := ../backend/app/payload/storefront-helper.elf

CFLAGS := -Wall -Werror -O2 -DVERSION=\"$(VERSION)\"

$(ELF): main.c ../backend/app/__init__.py
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -o $@ main.c -lkernel_sys

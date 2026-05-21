FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Base runtime: ARM emulation for md380-emu, Python for the Rewind client, ffmpeg for the audio pipe.
RUN apt-get update --allow-releaseinfo-change && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-pip \
        qemu-user-static \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Stub systemctl — the md380-emu .deb's postinst runs `systemctl disable`
# which fails in a containerised build without systemd. A no-op is fine since
# we invoke the binary directly, not via the systemd unit.
RUN printf '#!/bin/sh\nexit 0\n' > /tmp/systemctl-stub \
    && chmod +x /tmp/systemctl-stub \
    && cp /tmp/systemctl-stub /usr/local/bin/systemctl \
    && cp /tmp/systemctl-stub /usr/bin/systemctl \
    && cp /tmp/systemctl-stub /bin/systemctl \
    && rm /tmp/systemctl-stub

# Install md380-emu from DVSwitch's Debian repository.
# This adds dvswitch.org's apt source and pulls the maintained md380-emu package.
RUN wget --quiet -O /tmp/bookworm-setup http://dvswitch.org/bookworm \
    && chmod +x /tmp/bookworm-setup \
    && /tmp/bookworm-setup \
    && apt-get update \
    && apt-get install -y --no-install-recommends md380-emu \
    && rm -rf /var/lib/apt/lists/* /tmp/bookworm-setup

# Patch md380-emu's ELF program headers to mark the .firmware (0x0800c000) and
# .sram (0x20000000) segments as executable (RW -> RWE). On kernel >= 5.4 (and
# under qemu-arm-static emulating same), without PROT_EXEC on these segments the
# vocoder code is mapped non-executable and any decode call segfaults. This is
# the same fix as upstream PR #932 (a runtime mprotect call) but applied at the
# ELF level, since the DVSwitch package binary predates that patch.
RUN python3 - <<'PYEOF'
import struct
path = "/opt/md380-emu/md380-emu"
# ELF32 PHDR: 32 bytes per entry, starts at file offset 52 (per `readelf -l`).
# p_flags is at +0x18 (24) within each entry.
# Segments 03 (.firmware) and 04 (.sram) -> file offsets 172 and 204.
with open(path, "r+b") as f:
    for entry_off in (172, 204):
        f.seek(entry_off)
        old = struct.unpack("<I", f.read(4))[0]
        assert old == 6, f"unexpected phdr flags {old} at offset {entry_off}; binary layout changed"
        f.seek(entry_off)
        f.write(struct.pack("<I", 7))  # PF_R | PF_W | PF_X
print("md380-emu PHDR flags patched (.firmware and .sram now RWE)")
PYEOF

# Python deps for AMBE FEC decode
RUN pip3 install --no-cache-dir --break-system-packages \
        bitarray==2.9.2 \
        bitstring==4.2.3

WORKDIR /app
COPY rewind.py /app/rewind.py

# md380-emu binary lives at /opt/md380-emu/md380-emu and is ARM, run via qemu-arm-static.

CMD ["python3", "-u", "/app/rewind.py"]

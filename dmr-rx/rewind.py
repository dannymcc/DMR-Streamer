"""DMR-RX: BrandMeister Rewind/Open DMR Terminal protocol client.

Connects to a BM master via UDP, authenticates with the user's Hotspot
Security password, subscribes to a list of talkgroups, decodes the
72-bit FEC-encoded AMBE+2 voice frames into raw 49-bit AMBE, and pipes
them to md380-emu (running under qemu-arm-static) for PCM decode. The
resulting 8kHz PCM is fed to ffmpeg which encodes Opus and pushes to
the existing Icecast container.

Replaces the old Chromium+Hoseline scraper.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from binascii import hexlify
from datetime import datetime, timezone

from bitarray import bitarray
from bitstring import BitArray

# --- Rewind protocol constants -----------------------------------------------

REWIND_SIGN = b"REWIND01"

# Packet classes
_CLASS_CONTROL = 0x0000
_CLASS_CONSOLE = 0x0100
_CLASS_APP = 0x0900

PKT_KEEPALIVE = _CLASS_CONTROL + 0
PKT_CLOSE = _CLASS_CONTROL + 1
PKT_CHALLENGE = _CLASS_CONTROL + 2
PKT_AUTH = _CLASS_CONTROL + 3
PKT_REPORT = _CLASS_CONSOLE + 0
PKT_CONFIGURATION = _CLASS_APP + 0x00
PKT_SUBSCRIPTION = _CLASS_APP + 0x01
PKT_DMR_VOICE_HEADER = _CLASS_APP + 0x11  # call setup (3x per call, DMR sync frames)
PKT_DMR_TERMINATOR = _CLASS_APP + 0x12
PKT_DMR_AUDIO_FRAME = _CLASS_APP + 0x20
PKT_TALKER_ALIAS = _CLASS_APP + 0x27       # DMR Talker Alias fragments (callsign/name/qth)
PKT_SUPER_HEADER = _CLASS_APP + 0x28
PKT_FAILURE = _CLASS_APP + 0x29

REWIND_ROLE_APPLICATION = 0x20
REWIND_SERVICE_OPEN_DMR_TERMINAL = REWIND_ROLE_APPLICATION + 1

REWIND_SESSION_TYPE_GROUP_VOICE = 7

REWIND_OPTION_SUPER_HEADER = 1 << 0

VERSION_DESC = b"dmrstream"

# --- FEC decode (DMR 72-bit AMBE+FEC -> 49-bit raw AMBE) ---------------------
# Lifted from dmr_utils via redfast00/brandmeister-dmr-opendmr decode72to49.py.
# DMR uses Golay(23,12) over C0 and C1 of the AMBE codeword; this strips the
# parity/Hamming bits to recover the 49 raw bits the AMBE vocoder needs.

# Deinterleave schedule
_rW = [0,1,0,1,0,1, 0,1,0,1,0,1, 0,1,0,1,0,1, 0,1,0,1,0,2, 0,2,0,2,0,2, 0,2,0,2,0,2]
_rX = [23,10,22,9,21,8, 20,7,19,6,18,5, 17,4,16,3,15,2, 14,1,13,0,12,10, 11,9,10,8,9,7, 8,6,7,5,6,4]
_rY = [0,2,0,2,0,2, 0,2,0,3,0,3, 1,3,1,3,1,3, 1,3,1,3,1,3, 1,3,1,3,1,3, 1,3,1,3,1,3]
_rZ = [5,3,4,2,3,1, 2,0,1,13,0,12, 22,11,21,10,20,9, 19,8,18,7,17,6, 16,5,15,4,14,3, 13,2,12,1,11,0]


def _demodulate_ambe(ambe_fr):
    pr = [0] * 115
    foo = 0
    for i in range(23, 11, -1):
        foo = (foo << 1) | ambe_fr[0][i]
    pr[0] = 16 * foo
    for i in range(1, 24):
        pr[i] = (173 * pr[i - 1] + 13849) - 65536 * (((173 * pr[i - 1] + 13849) // 65536))
    for i in range(1, 24):
        pr[i] = pr[i] // 32768
    k = 1
    for j in range(22, -1, -1):
        ambe_fr[1][j] = ambe_fr[1][j] ^ pr[k]
        k += 1
    return ambe_fr


def _ecc_ambe(ambe_fr):
    out = bitarray()
    for j in range(23, 11, -1):
        out.append(ambe_fr[0][j])
    for j in range(22, 10, -1):
        out.append(ambe_fr[1][j])
    for j in range(10, -1, -1):
        out.append(ambe_fr[2][j])
    for j in range(13, -1, -1):
        out.append(ambe_fr[3][j])
    return out


def _deinterleave(data):
    fr = [[None] * 24 for _ in range(4)]
    bit_index = 0
    for i in range(36):
        bit1 = 1 if data[bit_index] else 0
        bit_index += 1
        bit0 = 1 if data[bit_index] else 0
        bit_index += 1
        fr[_rW[i]][_rX[i]] = bit1
        fr[_rY[i]][_rZ[i]] = bit0
    return fr


def convert_72_to_49(ambe72_bits: BitArray) -> bytes:
    """Take a 72-bit BitArray, return 7 bytes (56 bits) holding 49 bits of AMBE
    in the format md380-emu expects (high bits first, low 7 bits of last byte unused).
    """
    fr = _deinterleave(ambe72_bits)
    fr = _demodulate_ambe(fr)
    ambe49 = _ecc_ambe(fr)
    # tobytes() pads to a byte boundary (49 -> 56 bits = 7 bytes)
    return ambe49.tobytes()


# --- packet helpers ---------------------------------------------------------

HEADER_FMT = "<8sHHIH"  # sign(8), type(2), flags(2), seq(4), payload_len(2)
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 18


def build_packet(pkt_type: int, payload: bytes, seq: int) -> bytes:
    return struct.pack(HEADER_FMT, REWIND_SIGN, pkt_type, 0, seq, len(payload)) + payload


def parse_packet(data: bytes):
    if len(data) < HEADER_LEN:
        return None
    sign, pkt_type, _flags, _seq, payload_len = struct.unpack(HEADER_FMT, data[:HEADER_LEN])
    if sign != REWIND_SIGN:
        return None
    return pkt_type, data[HEADER_LEN:HEADER_LEN + payload_len]


# --- client ------------------------------------------------------------------

log = logging.getLogger("dmr-rx")

CALLSIGN_RE = re.compile(r"[A-Z0-9]{1,3}\d[A-Z]{1,4}")


class DMRRx:
    def __init__(self, *, dmr_id: int, password: str, master: str, port: int,
                 tgs: list[int], icecast_url: str, db_path: str | None = None,
                 kerchunk_min_seconds: float = 1.0):
        self.dmr_id = dmr_id
        self.password = password.encode("utf-8")
        self.master = master
        self.port = port
        self.base_tgs = list(tgs)
        self.tgs = list(tgs)
        self.icecast_url = icecast_url
        self.db_path = db_path
        self.kerchunk_min_seconds = max(0.0, kerchunk_min_seconds)
        self._muted_tgs: set[int] = set()
        self._muted_tgs_mtime = 0.0
        self._extra_tgs_mtime = 0.0
        self._tg_activity: dict[int, dict] = {}
        self._last_active_call_refresh = 0.0

        self.sock: socket.socket | None = None
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.logged_in = False
        self.subscribed: set[int] = set()
        self.requested_tgs: set[int] = set()

        self.md380: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.md380_lock = threading.Lock()

        self.last_audio_at = 0.0
        self.calls_started = 0
        self.frames_received = 0

        # DMR Talker Alias: BM sends caller's callsign/name/QTH as multiple
        # 9-byte 0x0927 fragments indexed 0x04..0x06. We buffer them and emit
        # a single "caller:" log when the call ends.
        self.ta_fragments: dict[int, bytes] = {}

        # Current call state, populated from the first 0x0911 voice header
        # of a transmission and cleared on terminator.
        self.current_call: dict | None = None

        # radioid.net lookup cache and async work queue: dmr-rx never blocks the
        # UDP loop on a network call. We INSERT the row immediately with src_id,
        # then a background worker enriches it with callsign/name from radioid.net.
        self._radioid_cache: dict[int, tuple[str | None, str | None]] = {}
        self._lookup_queue: queue.Queue[tuple[int | None, int]] = queue.Queue(maxsize=256)
        self._lookup_pending: set[int] = set()

    def next_seq(self) -> int:
        with self.seq_lock:
            s = self.seq
            self.seq += 1
            return s

    # ---- send helpers ----

    def _send(self, pkt_type: int, payload: bytes):
        pkt = build_packet(pkt_type, payload, self.next_seq())
        assert self.sock is not None
        self.sock.send(pkt)

    def send_keepalive(self):
        # KeepAlive payload = RewindVersionData: RemoteID(u32) + Service(u8) + Description(N bytes)
        payload = struct.pack("<IB", self.dmr_id, REWIND_SERVICE_OPEN_DMR_TERMINAL) + VERSION_DESC
        self._send(PKT_KEEPALIVE, payload)

    def send_auth(self, salt: bytes):
        digest = hashlib.sha256(salt + self.password).digest()
        self._send(PKT_AUTH, digest)

    def send_configuration(self, options: int):
        self._send(PKT_CONFIGURATION, struct.pack("<I", options))

    def send_subscription(self, tg: int):
        payload = struct.pack("<II", REWIND_SESSION_TYPE_GROUP_VOICE, tg)
        self._send(PKT_SUBSCRIPTION, payload)
        self.requested_tgs.add(tg)

    # ---- subprocess wiring ----

    def start_md380(self):
        log.info("starting md380-emu under qemu-arm-static")
        self.md380 = subprocess.Popen(
            ["qemu-arm-static", "/opt/md380-emu/md380-emu", "-d"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # Forward md380-emu stderr to our log so we can see what it complains about.
        def _stderr_drain():
            assert self.md380 is not None and self.md380.stderr is not None
            for line in iter(self.md380.stderr.readline, b""):
                txt = line.decode("utf-8", errors="replace").rstrip()
                if txt:
                    log.info("md380-emu: %s", txt)
            log.warning("md380-emu stderr drain ended (process exited)")

        threading.Thread(target=_stderr_drain, daemon=True, name="md380-stderr").start()

        # .amb file header (4 bytes), then per frame: \x00 + 7 bytes
        assert self.md380.stdin is not None
        self.md380.stdin.write(b".amb")
        self.md380.stdin.flush()

    def start_ffmpeg(self):
        log.info("starting ffmpeg → icecast")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-re",  # read input at native rate so icecast keeps streaming silence between calls
            "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
            "-ar", "48000", "-ac", "1",
            "-c:a", "libopus", "-b:a", "48k", "-application", "voip",
            # VBR with voip mode: silence frames compress to ~6 kbps while voice
            # still gets the full 48 kbps. Big mobile-data win — the iPhone only
            # pulls real bytes when someone's actually keyed up.
            "-vbr", "on", "-compression_level", "10",
            "-content_type", "audio/ogg",
            "-ice_name", os.environ.get("STREAM_NAME", "DMR Stream"),
            "-ice_description", os.environ.get("STREAM_DESCRIPTION", "BrandMeister DMR"),
            "-ice_genre", "Ham Radio",
            "-flush_packets", "1",
            "-page_duration", "20000",
            "-oggpagesize", "512",
            "-f", "ogg",
            self.icecast_url,
        ]
        self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)

    # ---- worker threads ----

    def pcm_pump(self):
        """Read 8kHz s16le PCM from md380-emu and write a steady stream to ffmpeg.

        Two regimes:
        - **Call active** (a voice frame arrived in the last 400ms): block on the
          queue. Don't inject silence mid-call — BM delivers voice in 60ms bursts
          and we want the natural inter-burst gap to stay zero-pad-free so audio
          plays out continuously, paced by ffmpeg's -re flag.
        - **Idle** (no frame for >400ms): pump silence at 20ms granularity to
          keep icecast's TCP stream alive and ffmpeg ticking forward.
        """
        SAMPLES_PER_FRAME = 160  # 20ms at 8kHz
        BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2
        silence = b"\x00" * BYTES_PER_FRAME
        IDLE_GAP_S = 0.4  # how long without a voice frame before we declare idle

        assert self.md380 is not None and self.md380.stdout is not None
        assert self.ffmpeg is not None and self.ffmpeg.stdin is not None

        import queue
        # Cap the queue at ~1.5s of audio. When it fills (ffmpeg/icecast aren't
        # draining fast enough on a busy net), we drop the OLDEST frame and
        # enqueue the newest. Keeps the listener close to live instead of
        # accumulating a minute-long delay over a busy QSO.
        pcm_q: queue.Queue[bytes] = queue.Queue(maxsize=75)

        def reader():
            buf = b""
            while True:
                chunk = self.md380.stdout.read(BYTES_PER_FRAME)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= BYTES_PER_FRAME:
                    try:
                        pcm_q.put_nowait(buf[:BYTES_PER_FRAME])
                    except queue.Full:
                        # Drop the oldest chunk to make room for the newest —
                        # never grow latency by stalling.
                        try:
                            pcm_q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            pcm_q.put_nowait(buf[:BYTES_PER_FRAME])
                        except queue.Full:
                            pass
                    buf = buf[BYTES_PER_FRAME:]

        threading.Thread(target=reader, daemon=True, name="md380-reader").start()

        while True:
            call_active = (time.time() - self.last_audio_at) < IDLE_GAP_S
            try:
                # While call is active, wait longer for the next BM frame (60ms cadence)
                # so we don't insert silence in the middle of speech.
                timeout = 0.2 if call_active else 0.02
                data = pcm_q.get(timeout=timeout)
            except queue.Empty:
                if call_active:
                    # In an active call but PCM queue drained — md380-emu may be slightly
                    # behind. Wait a bit longer instead of forcing silence.
                    continue
                data = silence
            try:
                self.ffmpeg.stdin.write(data)
            except BrokenPipeError:
                log.error("ffmpeg pipe closed, exiting pcm_pump")
                return

    def keepalive_loop(self):
        while True:
            time.sleep(5)
            try:
                self._refresh_extra_tgs()
                self.send_keepalive()
                self._write_receiver_heartbeat()
            except OSError as e:
                log.warning("keepalive send failed: %s", e)

    # ---- packet handlers ----

    def handle_packet(self, data: bytes):
        parsed = parse_packet(data)
        if parsed is None:
            return
        pt, payload = parsed

        if pt == PKT_CHALLENGE:
            log.info("challenge received (salt=%s), sending auth", hexlify(payload[:8]).decode())
            self.send_auth(payload)
        elif pt == PKT_KEEPALIVE:
            if not self.logged_in:
                log.info("auth ack — enabling SuperHeader + subscribing to TGs %s", self.tgs)
                # Configuration is fire-and-forget per BM (no ack expected).
                # Enabling SuperHeader makes the server send call-start packets
                # with src/dst ID + callsign for diagnostics.
                self.send_configuration(REWIND_OPTION_SUPER_HEADER)
                self._subscribe_missing_tgs()
        elif pt == PKT_CONFIGURATION:
            log.info("got configuration ack")
        elif pt == PKT_SUBSCRIPTION:
            self.subscribed.add(len(self.subscribed))  # count acks
            if not self.logged_in:
                self.logged_in = True
                log.info("logged in and subscribed; waiting for traffic on %s", self.tgs)
        elif pt == PKT_SUPER_HEADER:
            if len(payload) >= 12:
                session_type, src_id, dst_id = struct.unpack("<III", payload[:12])
                src_call = payload[12:22].rstrip(b"\x00").decode("ascii", errors="replace") if len(payload) >= 22 else ""
                self.calls_started += 1
                log.info("call started: TG%d from %s (%d)", dst_id, src_call or "?", src_id)
                if session_type == REWIND_SESSION_TYPE_GROUP_VOICE and self._is_tg_monitored(dst_id):
                    self._start_or_update_call(dst_id, src_id, src_call or None)
        elif pt == PKT_DMR_AUDIO_FRAME:
            self._handle_audio_frame(payload)
        elif pt == PKT_DMR_VOICE_HEADER:
            # 12-byte DMR Link Control frame: [FLCO/FID/Options:3][DstID:3 BE][SrcID:3 BE][CRC:3]
            # Fires 3x at start of each transmission (DMR sync frames).
            # BM doesn't always send a Terminator between back-to-back PTTs on
            # the same TG, so detect a new talker by src_id change too.
            if len(payload) >= 9:
                dst = int.from_bytes(payload[3:6], "big")
                src = int.from_bytes(payload[6:9], "big")
                if self._is_tg_monitored(dst):
                    self._start_or_update_call(dst, src)
        elif pt == PKT_TALKER_ALIAS:
            # 9-byte fragments: [index(1)] [flag(1)] [7 bytes data]. Indexes 0x04..0x06
            # carry callsign/name/QTH as parts of the DMR Talker Alias string.
            if len(payload) >= 9:
                idx = payload[0]
                self.ta_fragments[idx] = payload[2:9]
                # Refresh the active_call.json so the UI's "Now playing" picks
                # up callsign/name as soon as TA fragments arrive (typically
                # within the first second of a transmission).
                if self.current_call:
                    cs, nm = self._extract_callsign_name()
                    if cs and not self.current_call.get("callsign"):
                        self.current_call["callsign"] = cs
                    if nm and not self.current_call.get("name"):
                        self.current_call["name"] = nm
                    if self._call_has_enough_audio():
                        self._write_active_call()
        elif pt == PKT_DMR_TERMINATOR:
            self._finalize_call()
            self.current_call = None
            log.info("call ended")
        elif pt == PKT_FAILURE:
            log.error("server failure packet: %s", hexlify(payload).decode())
        elif pt == PKT_REPORT:
            try:
                msg = payload.decode("utf-8", errors="replace").rstrip()
            except Exception:
                msg = repr(payload)
            log.info("server report: %s", msg)
        elif pt == PKT_CLOSE:
            log.error("server closed connection")
            sys.exit(1)
        else:
            log.debug("unhandled packet type 0x%04x len=%d", pt, len(payload))

    def _start_or_update_call(self, tg: int, src_id: int, callsign: str | None = None):
        """Create/refresh current_call and kick off caller enrichment early."""
        if self.current_call and (
            self.current_call["src_id"] != src_id or self.current_call["tg"] != tg
        ):
            self._finalize_call()

        if not self.current_call:
            self.current_call = {
                "tg": tg,
                "src_id": src_id,
                "start_time": time.time(),
                "audio_frames": 0,
            }
            log.info("call started: TG%d from %d", tg, src_id)

        if callsign and not self.current_call.get("callsign"):
            self.current_call["callsign"] = callsign.strip()[:16] or None

        if self._call_has_enough_audio():
            self._mark_call_active()

    def _finalize_call(self):
        """Wrap up the current call: parse caller info from TA fragments, log it,
        write a listen_events row, and clear the active_call sidecar so the UI
        knows nothing is being transmitted right now."""
        callsign, name = self._extract_callsign_name()
        self._flush_caller_log()
        if self._call_has_enough_audio():
            self._write_listen_event(callsign, name)
        elif self.current_call:
            log.info(
                "ignored kerchunk: TG%d from %d (%.2fs, %d audio frames)",
                self.current_call["tg"],
                self.current_call["src_id"],
                self._call_audio_duration(),
                self.current_call.get("audio_frames", 0),
            )
        if self.current_call:
            self._write_tg_activity(
                self.current_call["tg"],
                self.current_call["src_id"],
                active=False,
            )
        self.ta_fragments.clear()
        self._clear_active_call()

    def _call_audio_duration(self) -> float:
        if not self.current_call:
            return 0.0
        first = self.current_call.get("first_audio_time")
        last = self.current_call.get("last_audio_time")
        if first and last:
            return max(0.0, float(last) - float(first))
        return max(0.0, time.time() - self.current_call["start_time"])

    def _call_has_enough_audio(self) -> bool:
        if not self.current_call:
            return False
        if self.kerchunk_min_seconds <= 0:
            return True
        return self._call_audio_duration() >= self.kerchunk_min_seconds

    def _mark_call_active(self):
        if not self.current_call:
            return
        self._write_active_call()
        self._write_tg_activity(
            self.current_call["tg"],
            self.current_call["src_id"],
            active=True,
        )
        self._queue_radioid_lookup(None, self.current_call["src_id"])

    def _extract_callsign_name(self) -> tuple[str | None, str | None]:
        """Parse the TA fragments into (callsign, name) tuple, both optional."""
        if not self.ta_fragments:
            return None, None
        ordered = b"".join(self.ta_fragments[i] for i in sorted(self.ta_fragments))
        readable = "".join(chr(b) if 32 <= b < 127 else " " for b in ordered)
        readable = " ".join(readable.split())
        return self._clean_talker_alias(readable)

    def _clean_talker_alias(self, readable: str) -> tuple[str | None, str | None]:
        readable = " ".join((readable or "").split())
        if not readable:
            return None, None
        match = CALLSIGN_RE.search(readable.upper())
        if not match:
            return None, readable[:64] or None
        callsign = match.group(0)[:16]
        name = readable[match.end():].strip(" -_/|:;,")
        # Talker Alias often contains QTH/bridge text after the callsign. Keep
        # a short readable suffix only when RadioID cannot supply a better name.
        return callsign, name[:64] or None

    def _write_listen_event(self, callsign: str | None, name: str | None):
        """Insert a row into the shared SQLite listen_events table.

        If we already have a cached radioid.net result for this src_id, use it
        to fill in missing callsign/name synchronously. Otherwise the row is
        inserted with what we know and the background worker enriches it later.
        """
        if not self.db_path or not self.current_call:
            return
        tg = self.current_call["tg"]
        src_id = self.current_call["src_id"]
        duration = max(0, int(time.time() - self.current_call["start_time"]))

        # Prefer RadioID over Talker Alias. TA is useful, but BrandMeister often
        # forwards noisy prefixes or location/bridge text that looks like a name.
        if src_id:
            r_cs, r_nm = self._radioid_lookup(src_id)
            callsign = r_cs or callsign
            name = r_nm or name

        try:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
            try:
                cur = conn.execute(
                    "INSERT INTO listen_events (tg, src_id, callsign, name, duration_seconds, heard_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (tg, src_id, callsign, name, duration,
                     datetime.now(timezone.utc).isoformat()),
                )
                row_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            log.warning("could not write listen_event (%s) — retry next call", e)
            return
        except Exception as e:
            log.warning("listen_event write failed: %s", e)
            return

        # Any missing field + uncached src_id → kick off async radioid.net lookup
        if src_id and (not callsign or not name):
            self._queue_radioid_lookup(row_id, src_id)

    def _queue_radioid_lookup(self, row_id: int | None, src_id: int):
        if not src_id or src_id in self._radioid_cache:
            return
        if row_id is None and src_id in self._lookup_pending:
            return
        try:
            self._lookup_pending.add(src_id)
            self._lookup_queue.put_nowait((row_id, src_id))
        except queue.Full:
            self._lookup_pending.discard(src_id)

    def _radioid_lookup_worker(self):
        """Background thread: pops queued (row_id, src_id) entries and hits
        radioid.net to backfill callsign/name on the corresponding row."""
        while True:
            try:
                row_id, src_id = self._lookup_queue.get()
            except Exception:
                continue
            callsign, name = self._radioid_lookup(src_id)
            self._lookup_pending.discard(src_id)
            if not callsign and not name:
                continue
            if row_id is not None:
                self._update_listen_event(row_id, callsign, name)
            # Update active_call.json if this is still the active caller, so the
            # UI's "Now playing" updates as soon as the lookup completes.
            if self.current_call and self.current_call.get("src_id") == src_id:
                self.current_call.setdefault("callsign", callsign)
                self.current_call.setdefault("name", name)
                if self._call_has_enough_audio():
                    self._write_active_call()

    def _radioid_lookup(self, dmr_id: int) -> tuple[str | None, str | None]:
        if dmr_id in self._radioid_cache:
            return self._radioid_cache[dmr_id]
        cached = self._radioid_lookup_local(dmr_id)
        if cached != (None, None):
            self._radioid_cache[dmr_id] = cached
            return cached
        url = f"https://radioid.net/api/dmr/user/?id={dmr_id}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dmrstream/1.0"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            log.debug("radioid lookup for %d failed: %s", dmr_id, e)
            self._radioid_cache[dmr_id] = (None, None)  # negative cache
            return None, None

        callsign: str | None = None
        name: str | None = None
        results = data.get("results") if isinstance(data, dict) else None
        if results:
            row = results[0]
            callsign = (row.get("callsign") or "").strip()[:16] or None
            # Prefer the user's first name + first surname token to keep it short
            fname = (row.get("fname") or "").strip()
            surname = (row.get("surname") or "").strip()
            full = (row.get("name") or "").strip()
            if fname or surname:
                name = " ".join(p for p in (fname, surname) if p)
            elif full:
                name = full
            if name:
                name = name[:64]
        result = (callsign or None, name or None)
        self._radioid_cache[dmr_id] = result
        if callsign or name:
            log.info("radioid: %d -> %s %s", dmr_id, callsign or "?", name or "")
        return result

    def _radioid_lookup_local(self, dmr_id: int) -> tuple[str | None, str | None]:
        if not self.db_path:
            return None, None
        try:
            conn = sqlite3.connect(self.db_path, timeout=1.0)
            try:
                row = conn.execute(
                    "SELECT callsign, name FROM radioid_users WHERE dmr_id = ?",
                    (dmr_id,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return None, None
        except Exception as e:
            log.debug("local radioid lookup failed for %d: %s", dmr_id, e)
            return None, None
        if not row:
            return None, None
        callsign = (row[0] or "").strip()[:16] or None
        name = (row[1] or "").strip()[:64] or None
        if callsign or name:
            log.info("local radioid: %d -> %s %s", dmr_id, callsign or "?", name or "")
        return callsign, name

    def _update_listen_event(self, row_id: int, callsign: str | None, name: str | None):
        """Backfill the row's callsign/name from radioid.net for any field TA
        didn't already populate (COALESCE keeps the more-authoritative TA value)."""
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path, timeout=2.0)
            try:
                conn.execute(
                    "UPDATE listen_events SET callsign = COALESCE(callsign, ?), "
                    "name = COALESCE(name, ?) WHERE id = ?",
                    (callsign, name, row_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("listen_event update failed: %s", e)

    @property
    def active_call_path(self) -> str | None:
        """Sidecar file path the FastAPI app reads for the "Now playing" UI."""
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "active_call.json")

    @property
    def tg_mutes_path(self) -> str | None:
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "tg_mutes.json")

    @property
    def tg_temp_mutes_path(self) -> str | None:
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "tg_temp_mutes.json")

    @property
    def tg_activity_path(self) -> str | None:
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "tg_activity.json")

    @property
    def receiver_heartbeat_path(self) -> str | None:
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "receiver_heartbeat.json")

    @property
    def tg_extra_path(self) -> str | None:
        if not self.db_path:
            return None
        return os.path.join(os.path.dirname(self.db_path), "tg_extra.json")

    def _is_tg_monitored(self, tg: int | None) -> bool:
        return tg is not None and tg in self.tgs

    def _subscribe_missing_tgs(self):
        if not self.logged_in and self.requested_tgs:
            return
        for tg in self.tgs:
            if tg not in self.requested_tgs:
                log.info("subscribing to TG%d", tg)
                self.send_subscription(tg)

    def _refresh_extra_tgs(self):
        path = self.tg_extra_path
        if not path:
            return
        try:
            st = os.stat(path)
            mtime = st.st_mtime
        except FileNotFoundError:
            mtime = 0.0
            rows = []
        except OSError:
            return
        else:
            if mtime == self._extra_tgs_mtime:
                return
            try:
                with open(path) as f:
                    data = json.load(f)
                rows = data.get("tgs") if isinstance(data, dict) else data
            except (ValueError, OSError):
                return

        extra_tgs: list[int] = []
        if isinstance(rows, list):
            for row in rows:
                value = row.get("tg") if isinstance(row, dict) else row
                try:
                    tg = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= tg <= 9999999 and tg not in extra_tgs and tg not in self.base_tgs:
                    extra_tgs.append(tg)

        next_tgs = list(self.base_tgs)
        for tg in extra_tgs:
            if tg not in next_tgs:
                next_tgs.append(tg)
        if next_tgs != self.tgs:
            removed = set(self.tgs) - set(next_tgs)
            self.tgs = next_tgs
            log.info("monitored TGs now: %s", self.tgs)
            if self.current_call and self.current_call.get("tg") in removed:
                self._finalize_call()
                self.current_call = None
            if self.logged_in:
                self._subscribe_missing_tgs()
        self._extra_tgs_mtime = mtime

    def _write_receiver_heartbeat(self):
        path = self.receiver_heartbeat_path
        if not path:
            return
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "logged_in": self.logged_in,
            "subscribed": sorted(self.subscribed),
            "requested_tgs": sorted(self.requested_tgs),
            "configured_tgs": self.tgs,
            "current_tg": self.current_call.get("tg") if self.current_call else None,
            "current_src_id": self.current_call.get("src_id") if self.current_call else None,
            "frames_received": self.frames_received,
            "calls_started": self.calls_started,
            "last_audio_age_s": round(time.time() - self.last_audio_at, 1) if self.last_audio_at else None,
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception as e:
            log.debug("receiver heartbeat write failed: %s", e)

    def _cached_caller_info(self, src_id: int) -> tuple[str | None, str | None]:
        """Return caller details without doing network IO on the audio path."""
        cached = self._radioid_cache.get(src_id)
        if cached is not None:
            return cached
        local = self._radioid_lookup_local(src_id)
        if local != (None, None):
            self._radioid_cache[src_id] = local
        return local

    def _write_tg_activity(self, tg: int, src_id: int, *, active: bool):
        path = self.tg_activity_path
        if not path:
            return
        cs, nm = self._cached_caller_info(src_id)
        now = datetime.now(timezone.utc).isoformat()
        self._tg_activity[tg] = {
            "tg": tg,
            "src_id": src_id,
            "callsign": cs,
            "name": nm,
            "active": active,
            "updated_at": now,
        }
        # Keep the file compact; the app only needs recent selector indicators.
        cutoff = time.time() - 300
        compact = {}
        for k, v in self._tg_activity.items():
            try:
                ts = datetime.fromisoformat(v["updated_at"]).timestamp()
            except Exception:
                ts = time.time()
            if ts >= cutoff:
                compact[k] = v
        self._tg_activity = compact
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"tgs": compact}, f)
            os.replace(tmp, path)
        except Exception as e:
            log.debug("TG activity write failed: %s", e)

    def _is_tg_muted(self, tg: int | None) -> bool:
        if tg is None:
            return False
        if self._is_tg_temp_muted(tg):
            return True
        path = self.tg_mutes_path
        if not path:
            return False
        try:
            st = os.stat(path)
        except FileNotFoundError:
            if self._muted_tgs:
                self._muted_tgs.clear()
            self._muted_tgs_mtime = 0.0
            return False
        except OSError:
            return tg in self._muted_tgs

        if st.st_mtime != self._muted_tgs_mtime:
            try:
                with open(path) as f:
                    data = json.load(f)
                values = data.get("muted_tgs") if isinstance(data, dict) else data
                if isinstance(values, list):
                    self._muted_tgs = {int(v) for v in values}
                else:
                    self._muted_tgs = set()
                self._muted_tgs_mtime = st.st_mtime
                log.info("muted TGs now: %s", sorted(self._muted_tgs) or "none")
            except Exception as e:
                log.debug("could not read TG mute file: %s", e)
        return tg in self._muted_tgs

    def _is_tg_temp_muted(self, tg: int) -> bool:
        path = self.tg_temp_mutes_path
        if not path:
            return False
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return False
        rows = data.get("tgs") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return False
        row = rows.get(str(tg))
        if not isinstance(row, dict):
            return False
        if self.current_call and row.get("src_id"):
            return row.get("src_id") == self.current_call.get("src_id")
        return True

    def _write_active_call(self):
        path = self.active_call_path
        if not path or not self.current_call:
            return
        # Synchronously pick up any cached radioid info we already have
        src = self.current_call["src_id"]
        cs = self.current_call.get("callsign")
        nm = self.current_call.get("name")
        if (not cs or not nm) and src in self._radioid_cache:
            r_cs, r_nm = self._radioid_cache[src]
            cs = cs or r_cs
            nm = nm or r_nm
        if not cs or not nm:
            r_cs, r_nm = self._cached_caller_info(src)
            cs = cs or r_cs
            nm = nm or r_nm
            self._queue_radioid_lookup(None, src)
        payload = {
            "active": True,
            "tg": self.current_call["tg"],
            "src_id": src,
            "callsign": cs,
            "name": nm,
            "started_at": datetime.fromtimestamp(
                self.current_call["start_time"], tz=timezone.utc
            ).isoformat(),
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception as e:
            log.debug("active_call write failed: %s", e)

    def _clear_active_call(self):
        path = self.active_call_path
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.debug("active_call clear failed: %s", e)

    def _flush_caller_log(self):
        """Concatenate buffered Talker Alias fragments and emit a single caller line.
        Each fragment is 7 bytes of mixed binary + ASCII; we strip non-printable
        bytes so the readable callsign/name/QTH come out clean.
        """
        if not self.ta_fragments:
            return
        # Walk indexes in order (04, 05, 06, ...) and concat the 7-byte payloads
        ordered = b"".join(self.ta_fragments[i] for i in sorted(self.ta_fragments))
        # Replace non-printable bytes with spaces, then collapse runs of whitespace
        readable = "".join(chr(b) if 32 <= b < 127 else " " for b in ordered)
        readable = " ".join(readable.split())  # collapse multiple spaces
        callsign, name = self._clean_talker_alias(readable)
        if callsign or name:
            log.info("caller: %s%s", callsign or "?", f" {name}" if name else "")
        self.ta_fragments.clear()

    def _handle_audio_frame(self, payload: bytes):
        if len(payload) != 27:
            log.warning("unexpected audio frame length %d", len(payload))
            return
        if not self.current_call:
            return
        now = time.time()
        self.current_call["audio_frames"] = self.current_call.get("audio_frames", 0) + 1
        self.current_call.setdefault("first_audio_time", now)
        self.current_call["last_audio_time"] = now
        if now - self._last_active_call_refresh >= 2.0:
            if self._call_has_enough_audio():
                self._mark_call_active()
                self._last_active_call_refresh = now
        if self._is_tg_muted(self.current_call.get("tg")):
            return
        self.frames_received += 1
        self.last_audio_at = time.time()
        if self.frames_received <= 5 or self.frames_received % 50 == 0:
            log.info("audio frame #%d (payload %s...)", self.frames_received, hexlify(payload[:9]).decode())

        try:
            with self.md380_lock:
                assert self.md380 is not None and self.md380.stdin is not None
                for i in range(3):
                    chunk = payload[i * 9:(i + 1) * 9]
                    ambe72 = BitArray(bytes=chunk)
                    ambe7 = convert_72_to_49(ambe72)
                    # .amb frame format: marker byte (0x00) + 7 bytes of packed AMBE
                    self.md380.stdin.write(b"\x00" + ambe7)
                self.md380.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log.error("md380-emu write failed (%s); will not exit, watch for restart", e)
            # Don't sys.exit — let stderr drain tell us why md380-emu died,
            # and let ffmpeg keep streaming silence so icecast stays up.

    # ---- main loop ----

    def run(self):
        self.start_md380()
        self.start_ffmpeg()

        threading.Thread(target=self.pcm_pump, daemon=True, name="pcm-pump").start()
        threading.Thread(target=self.keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self._radioid_lookup_worker, daemon=True, name="radioid").start()
        self._clear_active_call()  # stale file from previous run
        self._write_receiver_heartbeat()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((self.master, self.port))
        log.info("connected (UDP) to %s:%d as DMR ID %d", self.master, self.port, self.dmr_id)

        # Kick off the handshake
        self.send_keepalive()

        # Main RX loop
        while True:
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                continue
            except OSError as e:
                log.error("socket error: %s", e)
                time.sleep(2)
                continue
            self.handle_packet(data)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    dmr_id = int(os.environ["BM_DMR_ID"])
    password = os.environ["BM_PASSWORD"]
    master = os.environ["BM_MASTER"]
    port = int(os.environ.get("BM_PORT", "54006"))
    tgs = [int(t) for t in os.environ.get("BM_TGS", "91,2350,2351,2352,2353,235,3100,23520,23526,23531,23562,235175").split(",") if t.strip()]
    kerchunk_min_seconds = float(os.environ.get("KERCHUNK_MIN_SECONDS", "1.0"))

    icecast_user = "source"
    icecast_pw = os.environ["ICECAST_SOURCE_PASSWORD"]
    icecast_host = os.environ.get("ICECAST_HOST", "dmrstream-icecast")
    icecast_port = int(os.environ.get("ICECAST_PORT", "8000"))
    icecast_mount = os.environ.get("ICECAST_MOUNT", "/dmr.opus")
    icecast_url = f"icecast://{icecast_user}:{icecast_pw}@{icecast_host}:{icecast_port}{icecast_mount}"

    client = DMRRx(
        dmr_id=dmr_id,
        password=password,
        master=master,
        port=port,
        tgs=tgs,
        icecast_url=icecast_url,
        db_path=os.environ.get("DMRSTREAM_DB"),
        kerchunk_min_seconds=kerchunk_min_seconds,
    )
    client.run()


if __name__ == "__main__":
    main()

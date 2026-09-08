"""Protocol constants for PCUTP v0.2 (see docs/PCUTP-0.2.md)."""

PROTOCOL_VERSION = "PCUTP/2"

# The one line rate for the whole link. Negotiation was a feature of v0.2 that
# was removed after hardware testing: the Flipper USB-UART bridge carried
# 115200 reliably but corrupted 4096-byte blocks at every faster rate, so a
# fixed rate is the honest default. It stays a single constant - a direct wire
# can be retested by changing this one number (section 5).
BASE_BAUDRATE = 115200

DEFAULT_BLOCK_SIZE = 4096
MAX_BLOCK_SIZE = 4096

MAX_FILENAME_LEN = 64
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MiB

MAX_RETRIES = 5          # per block, section 15
MAX_DROPS = 3            # sequence gaps before restarting the file
MAX_RESETS = 2           # full-file restarts before aborting
RST_RETRIES = 3          # RST 0 token attempts before declaring the peer lost
FLOW_GROW_ACKS = 8       # clean cumulative ACKs before widening cwnd
ACK_TIMEOUT = 12.0       # seconds; includes SD-card writes and recovery barriers
MAX_TIMEOUTS = 5         # section 16
LINE_TIMEOUT = 30.0      # generic control-line read timeout
DATA_TIMEOUT = 12.0      # PicoCalc side: raw payload after a DATA header
PROBE_GUARD = 1.0        # after ACK timeout, let a 20s raw read finish first
PROBE_TIMEOUT = 3.0      # wait for one HERE response
PROBE_RETRIES = 10       # tolerate a manually reconnected three-wire link

# Teardown (section 6 of docs/PCUTP-0.2.md). CLOSE is FIN, BYE is FIN-ACK,
# and each direction closes independently.
CLOSE_TIMEOUT = 3.0      # wait for BYE before resending CLOSE
CLOSE_RETRIES = 3        # attempts before giving up and dropping the link
# After the last BYE, keep watching the line. A BYE can be lost, in which case
# the peer resends CLOSE; if this end had already gone idle that stray CLOSE
# would arrive as the *next* session's first control line. The longest thing
# that can still be in flight is one block, 0.36s at 115200, so this is ample.
LINGER_TIME = 2.0

KEEPALIVE_INTERVAL = 5.0   # idle seconds before sending a PING probe
KEEPALIVE_RETRIES = 3      # numbered probes before declaring the link lost
KEEPALIVE_TIMEOUT = 20.0   # 3 probes at 5, 10, 15s; fail at about 20s
BEACON_INTERVAL = 4.0      # bidirectional idle heartbeat; PING is the fallback
CONNECT_DELAY = 0.4        # retained API setting; handshake audio pacing only
HANDSHAKE_TIMEOUT = 10.0   # wait for HELLO HRU? / OHRU / HRU
HELLO_RETRIES = 10         # first HELLO plus nine HELLO? retries

MAX_REDIRECTS = 5        # section 24
HTTP_TIMEOUT = 30.0      # how long the server may spend on the internet leg

# Nothing crosses the wire while the server is fetching, so the receiver's
# link-loss timer would fire on a perfectly healthy link. The server announces
# the gap with FETCHING and the receiver widens its window to this for the
# META that follows. INVARIANT: FETCH_TIMEOUT > HTTP_TIMEOUT, and the same
# relation must hold for FETTMO in picocalc/PCUTP.BAS - a fetch that outlives
# the receiver's patience looks exactly like a dead link (section 16.4).
FETCH_TIMEOUT = 45.0

ALLOWED_SCHEMES = ("http", "https")
PART_SUFFIX = ".PART"

LF = b"\n"

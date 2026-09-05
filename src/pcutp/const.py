"""Protocol constants for PCUTP v0.1 (see docs/PCUTP-0.1.md)."""

PROTOCOL_VERSION = "PCUTP/1"

DEFAULT_BAUDRATE = 115200
DEFAULT_BLOCK_SIZE = 1024
MAX_BLOCK_SIZE = 1024

MAX_FILENAME_LEN = 64
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MiB

MAX_RETRIES = 5          # per block, section 15
ACK_TIMEOUT = 5.0        # seconds, section 16
MAX_TIMEOUTS = 5         # section 16
LINE_TIMEOUT = 30.0      # generic control-line read timeout
DATA_TIMEOUT = 10.0      # PicoCalc side: raw payload after a DATA header

MAX_REDIRECTS = 5        # section 24
HTTP_TIMEOUT = 30.0

ALLOWED_SCHEMES = ("http", "https")
PART_SUFFIX = ".PART"

LF = b"\n"

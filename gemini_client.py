"""gemini_client.py -- Async Gemini protocol client for MicroPythonOS.  
  
UI-free: safe to import from tests, services, or other apps.  
  
Prefers AF_INET (IPv4) because asyncio.open_connection() only tries the  
first getaddrinfo entry, which can be a blackholed IPv6 route on networks  
without IPv6 connectivity.  
  
The getaddrinfo sockaddr format is NOT portable across MicroPython ports:  
  - unix port: opaque bytearray (binary sockaddr_in, IPv4 addr at [4:8])  
  - esp32 port (lwIP): plain (ip_str, port) tuple  
_open_connection() handles both shapes, and falls back to connecting by  
hostname (best for TLS SNI) when no AF_INET entry is returned.  
"""  
  
import asyncio  
import socket  
import ssl  
from mpos import TaskManager  
from mpos.net.async_dns import getaddrinfo_async  
  
_DEFAULT_PORT = 1965  
_CONNECT_TIMEOUT_S = 20  
_READ_TIMEOUT_S = 20  
_MAX_REDIRECTS = 5  # best-practices.gmi: clients should cap automatic redirects  
  
# MIME types that are safe to decode as text and render as gemtext/plain text.  
_TEXT_MIME_PREFIXES = ("text/",)  
  
# Map common non-gemtext MIME types to a file extension, so a fetched body  
# can be written to a temp file and handed to the right app via an Intent  
# resolved by pathPattern (AppManager matches by extension, not MIME string).  
_MIME_TO_EXT = {  
    "image/png": ".png",  
    "image/jpeg": ".jpg",  
    "image/jpg": ".jpg",  
    "image/bmp": ".bmp",  
    "image/x-ms-bmp": ".bmp",  
    "audio/wav": ".wav",  
    "audio/x-wav": ".wav",  
}  
  
  
class CrossProtocolRedirect(Exception):  
    """Raised when a redirect leaves gemini:// -- caller must confirm with the user."""  
    def __init__(self, url):  
        super().__init__(url)  
        self.url = url  
  
  
class TooManyRedirects(Exception):  
    pass  
  
  
def parse_url(url):  
    """Split a gemini:// URL into (host, port, path)."""  
    if not url.startswith("gemini://"):  
        raise ValueError("not a gemini URL: %s" % url)  
    rest = url[len("gemini://"):]  
    slash = rest.find("/")  
    authority, path = (rest, "/") if slash < 0 else (rest[:slash], rest[slash:])  
    colon = authority.find(":")  
    if colon < 0:  
        return authority, _DEFAULT_PORT, path  
    return authority[:colon], int(authority[colon + 1:]), path  
  
  
def _ipv4_from_sockaddr(sa):  
    """Extract an IPv4 "a.b.c.d" string from a getaddrinfo sockaddr.  
  
    Returns None if the sockaddr shape is not recognized. Handles:  
      - esp32/lwIP: ('46.224.212.116', 1965) tuple  
      - unix port:  bytearray(b'\\x02\\x00\\x07\\xad.\\xe0\\xd4t...')  (addr at [4:8])  
    """  
    if isinstance(sa, tuple):  
        return sa[0]  
    try:  
        if len(sa) >= 8:  
            return ".".join(str(b) for b in sa[4:8])  
    except TypeError:  
        pass  
    return None  
  
  
async def _open_connection(host, port, ssl_context):  
    """Resolve off the event-loop thread, prefer an AF_INET route, connect.  
  
    getaddrinfo_async() runs the blocking lookup on a _thread worker so the  
    event loop / LVGL thread keeps ticking. We then pick the first AF_INET  
    result -- asyncio.open_connection() would otherwise try only the FIRST  
    getaddrinfo entry, which on dual-stack unix DNS is often a blackholed  
    IPv6 address. If no IPv4 entry exists (or the sockaddr shape is unknown),  
    we connect by hostname so TLS SNI still uses the real host.  
    """  
    ai = await getaddrinfo_async(host, port)  
    target = host  # fallback: hostname connect (correct SNI)  
    for entry in ai:  
        if entry[0] == socket.AF_INET:  
            ip = _ipv4_from_sockaddr(entry[-1])  
            if ip:  
                target = ip  
                break  
    return await asyncio.open_connection(target, port, ssl=ssl_context)  
  
  
async def _fetch_once(url, connect_timeout, read_timeout):  
    """Single gemini request/response, no redirect handling. Returns raw bytes."""  
    host, port, path = parse_url(url)  
  
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  
    context.verify_mode = ssl.CERT_NONE  
  
    reader, writer = await TaskManager.wait_for(  
        _open_connection(host, port, context), connect_timeout)  
    try:  
        await writer.awrite("gemini://%s%s\r\n" % (host, path))  
        chunks = []  
        while True:  
            chunk = await TaskManager.wait_for(reader.read(4096), read_timeout)  
            if not chunk:  
                break  
            chunks.append(chunk)  
        return b"".join(chunks)  
    finally:  
        await writer.aclose()  
  
  
def split_response_bytes(raw):  
    """Split a raw Gemini response into (status_line_str, body_bytes).  
  
    Only the header line is decoded as UTF-8 (per spec, <META> is UTF-8 text);  
    the body is kept as raw bytes since it may be a binary MIME type (image,  
    audio, etc.) that would raise/corrupt if blindly decoded as UTF-8.  
    """  
    sep = raw.find(b"\r\n")  
    header_len = 2  
    if sep < 0:  
        sep = raw.find(b"\n")  
        header_len = 1  
    if sep < 0:  
        # No header terminator found at all -- treat everything as header,  
        # empty body, rather than raising.  
        return raw.decode("utf-8", "replace"), b""  
    header = raw[:sep].decode("utf-8", "replace")  
    body = raw[sep + header_len:]  
    return header, body  
  
  
def split_response(raw):  
    """Backwards-compatible text-only split: (status_line, body_text).  
  
    Kept for callers that only ever deal with text/gemini responses.  
    Prefer split_response_bytes() + get_mime_type() for MIME-aware handling.  
    """  
    header, body_bytes = split_response_bytes(raw)  
    return header, body_bytes.decode("utf-8", "replace")  
  
  
def get_mime_type(status_line):  
    """Extract the MIME type from a 2x status line's <META> field.  
  
    '20 text/gemini; charset=utf-8' -> 'text/gemini'  
    '20 image/png' -> 'image/png'  
    Returns None if not a 2x response or no META present.  
    """  
    try:  
        parts = status_line.split(None, 1)  
        code = int(parts[0])  
    except (ValueError, IndexError):  
        return None  
    if not (20 <= code <= 29):  
        return None  
    if len(parts) < 2:  
        return "text/gemini"  # spec default when META is omitted on 2x  
    meta = parts[1].strip()  
    return meta.split(";", 1)[0].strip().lower()  
  
  
def is_text_mime(mime_type):  
    """True if a MIME type should be decoded/rendered as text (gemtext or plain)."""  
    if not mime_type:  
        return True  # default to text/gemini per spec  
    return mime_type.startswith(_TEXT_MIME_PREFIXES)  
  
  
def extension_for_mime(mime_type, fallback=".bin"):  
    """Best-effort file extension for a MIME type, for writing a temp file  
    that AppManager's pathPattern-based Intent resolution can then route  
    (e.g. to Image View via its MANIFEST.JSON pathPattern list)."""  
    if not mime_type:  
        return fallback  
    return _MIME_TO_EXT.get(mime_type, fallback)  
  
  
def status_text(status_line):  
    """Human-readable description for a Gemini status header like '20 text/gemini'."""  
    try:  
        code = int(status_line.split(None, 1)[0])  
    except (ValueError, IndexError):  
        return "Unknown status: %s" % status_line  
    if 10 <= code <= 19:  
        return "Input required: %s" % status_line  
    if 20 <= code <= 29:  
        return None  # success  
    if 30 <= code <= 39:  
        return "Redirect: %s" % status_line  
    if 40 <= code <= 49:  
        return "Temporary error: %s" % status_line  
    if 50 <= code <= 59:  
        return "Permanent error: %s" % status_line  
    return "Unknown status: %s" % status_line  
  
  
def resolve_url(base_url, target):  
    """Resolve a Gemini link/redirect target, which may be relative or absolute."""  
    if "://" in target:  
        return target  
    host, port, _path = parse_url(base_url)  
    if target.startswith("/"):  
        return "gemini://%s%s" % (host, target)  
    base_path = base_url[len("gemini://") + len(host):]  
    base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""  
    return "gemini://%s%s/%s" % (host, base_dir, target)  
  
  
async def fetch(url, connect_timeout=_CONNECT_TIMEOUT_S, read_timeout=_READ_TIMEOUT_S,  
                allow_cross_protocol=False):  
    """Fetch a gemini:// URL, following redirects (max 5) per best practices.  
  
    Returns (status_line, body, final_url) where `body` is:  
      - a decoded `str` if the response's MIME type is text-like (text/gemini,  
        text/plain, etc.), suitable for format_gemtext()/direct display, or  
      - raw `bytes` for any other MIME type (images, audio, etc.) -- callers  
        must check get_mime_type(status_line) and is_text_mime() to decide,  
        then typically write the bytes to a temp file with  
        extension_for_mime() and hand it off via Intent(action="view", ...).  
  
    Raises CrossProtocolRedirect if a redirect leaves the gemini scheme and  
    allow_cross_protocol is False -- callers should ask the user to confirm,  
    then retry with allow_cross_protocol=True pointed at CrossProtocolRedirect.url.  
    Raises TooManyRedirects after 5 hops.  
    """  
    redirects = 0  
    current_url = url  
    while True:  
        raw = await _fetch_once(current_url, connect_timeout, read_timeout)  
        status_line, body_bytes = split_response_bytes(raw)  
        try:  
            code = int(status_line.split(None, 1)[0])  
        except (ValueError, IndexError):  
            code = 0  
  
        if 30 <= code <= 39:  
            redirects += 1  
            if redirects > _MAX_REDIRECTS:  
                raise TooManyRedirects(current_url)  
            target = status_line.split(None, 1)[1].strip() if " " in status_line else ""  
            target = resolve_url(current_url, target)  
            if not target.startswith("gemini://") and not allow_cross_protocol:  
                raise CrossProtocolRedirect(target)  
            current_url = target  
            continue  
  
        mime_type = get_mime_type(status_line)  
        if is_text_mime(mime_type):  
            body = body_bytes.decode("utf-8", "replace")  
        else:  
            body = body_bytes  # binary: leave as bytes for the caller  
  
        return status_line, body, current_url  
  
  
# Backwards-compatible alias used by earlier gemini.py revisions.  
async def fetch_raw(url, connect_timeout=_CONNECT_TIMEOUT_S, read_timeout=_READ_TIMEOUT_S):  
    """Fetch a gemini:// URL without following redirects. Returns raw bytes."""  
    return await _fetch_once(url, connect_timeout, read_timeout)  
  
  
def format_gemtext(content):  
    """Convert gemtext to a list of (line_type, text) tuples per the gemtext spec."""  
    if not content:  
        return [("text", "(empty content)")]  
  
    result = []  
    preformatted = False  
    blank_pending = False  # collapse consecutive blank lines into a single blank marker  
  
    for raw_line in content.split("\n"):  
        line = raw_line.rstrip("\r")  
  
        if line.startswith("```"):  
            preformatted = not preformatted  
            continue  
  
        if preformatted:  
            blank_pending = False  
            result.append(("pre", line))  
            continue  
  
        if not line:  
            blank_pending = True  
            continue  
  
        if blank_pending:  
            result.append(("blank", ""))  
            blank_pending = False  
  
        if line.startswith("=>"):  
            rest = line[2:].strip()  
            parts = rest.split(None, 1)  
            if parts:  
                url = parts[0]  
                label = parts[1] if len(parts) > 1 else url  
                result.append(("link", (url, label)))  
        elif line.startswith("###"):  
            result.append(("h3", line[3:].strip()))  
        elif line.startswith("##"):  
            result.append(("h2", line[2:].strip()))  
        elif line.startswith("#"):  
            result.append(("h1", line[1:].strip()))  
        elif line.startswith("* "):  
            result.append(("list", line[2:].strip()))  
        elif line.startswith(">"):  
            result.append(("quote", line[1:].strip()))  
        else:  
            result.append(("text", line))  
  
    return result
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fembimmo Display — single-file edition
Made By Jack De Wandel

Install:        Missing Python packages install automatically at startup (pip required)
                (ffmpeg, yt-dlp, Deno and cloudflared download automatically)
                Linux venv: pip install qtpy PyQt6 PyQt6-WebEngine   (or PyQtWebEngine for PyQt5)
Run:            python fembimmo_display.py      (on Windows use pythonw to hide the console)
Options:        --no-gui   only start the servers (open the printed URL in a browser)
                --debug    open the developer tools

Tabs: Upload (QR + short link via Cloudflare quick tunnel), Gallery, Library (watermark-free
photos/videos), Music (YouTube Music + Christmas presets), Remote (second QR to control the app
from a phone) and Settings.
"""
from __future__ import annotations

import json
import math
import mimetypes
import os
import platform
import queue
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import importlib.util as _ilu

# Keep these environment flags in place before importing Qt or pywebview.
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--autoplay-policy=no-user-gesture-required')
os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '--autoplay-policy=no-user-gesture-required')


def _module_available(name: str) -> bool:
    """Check an import name without allowing a broken optional install to abort startup."""
    try:
        return _ilu.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def ensure_python_dependencies() -> None:
    """Install this app's Python dependencies into the interpreter running this script.

    Native system components (for example, the Microsoft Edge WebView2 Runtime) cannot be
    installed reliably through pip and are not installed by this function. Helper executables
    such as ffmpeg, yt-dlp, Deno and cloudflared are handled by the app's existing downloader.
    """
    if getattr(sys, 'frozen', False):
        # Frozen apps should bundle Python dependencies; sys.executable is not a Python launcher.
        return

    if sys.version_info < (3, 10):
        print('[setup] Python 3.10 or newer is recommended/required by current pywebview releases; '
              f'you are running {sys.version.split()[0]}.')

    missing = []
    # This app explicitly selects pywebview's Qt backend on Windows. Ensure pywebview and
    # each module used by our Qt-backend check/clipboard bridge are present.
    if not _module_available('webview'):
        missing.append('pywebview[qt]')
    elif not (_module_available('qtpy') and _module_available('PyQt6.QtWebEngineWidgets')):
        # Re-request the Qt extra so pip can resolve any missing backend dependencies.
        missing.append('pywebview[qt]')
    if not _module_available('qtpy'):
        missing.append('qtpy')
    if not _module_available('PyQt6'):
        missing.append('PyQt6')
    if not _module_available('PyQt6.QtWebEngineWidgets'):
        missing.append('PyQt6-WebEngine')
    if not _module_available('segno'):
        missing.append('segno')
    if not _module_available('certifi'):
        missing.append('certifi')
    # Avoid redundant package arguments if the Qt extra and explicit checks requested the same one.
    missing = list(dict.fromkeys(missing))

    if not missing:
        return

    print('[setup] Installing missing Python packages: ' + ', '.join(missing))
    pip_cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', *missing]
    try:
        result = subprocess.run(pip_cmd, check=False)
        if result.returncode != 0:
            # A few Python distributions do not include pip by default. Try ensurepip once,
            # then retry the same install command.
            if not _module_available('pip'):
                print('[setup] pip is missing; attempting to enable it with ensurepip...')
                ensurepip = subprocess.run(
                    [sys.executable, '-m', 'ensurepip', '--upgrade'], check=False
                )
                if ensurepip.returncode == 0:
                    result = subprocess.run(pip_cmd, check=False)
        if result.returncode != 0:
            print('[setup] Automatic package installation did not complete. Check the error above, '
                  'your internet connection, and whether this Python environment allows package installs.')
        else:
            print('[setup] Python package setup finished.')
    except Exception as exc:
        print(f'[setup] Could not run pip automatically: {exc}')


# Install missing dependencies before importing them. Installation is one-time per environment.
ensure_python_dependencies()

# Prefer Qt 6 (newer Chromium) when both Qt 5 and Qt 6 are installed, and allow autoplay of music.
try:
    if 'QT_API' not in os.environ and _module_available('PyQt6') and _module_available('PyQt6.QtWebEngineWidgets'):
        os.environ['QT_API'] = 'pyqt6'
except Exception:
    pass

try:
    import segno  # optional: nicer PNG QR codes (a built-in encoder is used otherwise)
except ImportError:  # pragma: no cover
    segno = None

try:
    import webview  # pywebview: native window with Edge WebView2 / WebKit
except ImportError:  # pragma: no cover
    webview = None

APP_NAME = 'Fembimmo Display'
VERSION = '2.0.0'
LOGO_URL = ('https://storage.googleapis.com/zabun-cms-prod/933dde50-ee49-41c4-8bfb-f23e28d63da7'
            '/theme/logoUrl_o%7C1682077815')
PDFJS_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/'
YTM = 'https://music.youtube.com/'
IS_WIN = sys.platform == 'win32'
NO_WINDOW = 0x08000000 if IS_WIN else 0

# --------------------------------------------------------------------------------------------
# Paths & storage
# --------------------------------------------------------------------------------------------
def data_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
        return base / 'FembimmoDisplay'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'FembimmoDisplay'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'fembimmo-display'


DATA = data_dir()
UPLOADS = DATA / 'uploads'
PREVIEWS = UPLOADS / 'previews'
TMP = DATA / 'tmp'
BIN = DATA / 'bin'
VENDOR = DATA / 'vendor'
OFFICE_PROFILE = DATA / 'office-profile'
LOGO_FILE = DATA / 'logo.bin'
LOGS = DATA / 'logs'
for _d in (UPLOADS, PREVIEWS, TMP, BIN, VENDOR, OFFICE_PROFILE, LOGS):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    'language': 'nl',
    'welcomeName': 'Febe',
    'theme': 'light',
    'accent': '#12424a',
    'autoStart': True,
    'fullscreen': True,
    'keepAwake': True,
    'snow': False,
    'linkRefreshMin': 15,
    'remoteRefreshMin': 0,
    'maxUploadMB': 4096,
    'switchToGallery': True,
    'autoShowNew': False,
    'slideshowSec': 8,
    'docArrowNav': True,
    'viewerFit': 'blur',
    'showLogo': True,
    'idleReturnMin': 0,
    'pauseMusicOnVideo': True,
    'musicAutoplay': False,
    'musicShuffle': False,
    'pexelsKey': 'ROYct510J2nAm65fJULcmOorvaJK7zXuivxFiWxB6WUkWbVmSv40g1Zz',
    'pixabayKey': '58296920-3de246969543dcf5a5647768f',
    'unsplashKey': '',
    'musicVolume': 80,
}


class JsonStore:
    """Small JSON store with atomic, debounced writes."""

    def __init__(self, path: Path, defaults: dict):
        self.path = Path(path)
        self.lock = threading.RLock()
        loaded = {}
        try:
            loaded = json.loads(self.path.read_text('utf-8'))
        except Exception:
            pass
        self.data = {**json.loads(json.dumps(defaults)), **loaded}
        self._timer = None

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def all(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(0.3, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        with self.lock:
            try:
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), 'utf-8')
                os.replace(tmp, self.path)
            except Exception:
                traceback.print_exc()


settings = JsonStore(DATA / 'settings.json', DEFAULTS)
db = JsonStore(DATA / 'library.json', {'items': []})
items_lock = threading.RLock()


def sanitize_setting(key, value):
    d = DEFAULTS[key]
    if isinstance(d, bool):
        return bool(value)
    if isinstance(d, int):
        try:
            return max(0, min(102400, int(float(value))))
        except Exception:
            return d
    if key == 'language':
        return value if value in ('nl', 'en') else d
    if key == 'theme':
        return value if value in ('light', 'dark') else d
    if key == 'viewerFit':
        return value if value in ('blur', 'contain', 'cover') else d
    if key == 'accent':
        return value if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else d
    return str(value if value is not None else '').strip()[:200]


# --------------------------------------------------------------------------------------------
# Event bus (Server-Sent Events to the UI)
# --------------------------------------------------------------------------------------------
class EventBus:
    def __init__(self):
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def emit(self, event: str, data=None):
        msg = f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


bus = EventBus()
SHUTDOWN = threading.Event()


def spawn(fn, *args, **kw):
    t = threading.Thread(target=fn, args=args, kwargs=kw, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------------------------
# Media helpers
# --------------------------------------------------------------------------------------------
UA = 'Mozilla/5.0 (compatible; FembimmoDisplay/2.0; +https://fembimmo.be)'
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/130.0.0.0 Safari/537.36')

EXT = {
    'image': ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'avif', 'ico', 'jfif'],
    'video': ['mp4', 'webm', 'mov', 'm4v', 'mkv', 'ogv', '3gp', 'avi', 'wmv'],
    'audio': ['mp3', 'wav', 'ogg', 'oga', 'm4a', 'aac', 'flac', 'opus', 'weba', 'wma'],
    'pdf': ['pdf'],
    'presentation': ['ppt', 'pptx', 'pps', 'ppsx', 'pptm', 'odp', 'key'],
    'document': ['doc', 'docx', 'odt', 'rtf', 'xls', 'xlsx', 'ods', 'csv'],
    'text': ['txt', 'md', 'json', 'log', 'xml'],
    'html': ['html', 'htm'],
}
MIME_EXT = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp', 'image/svg+xml': 'svg',
    'image/avif': 'avif', 'image/bmp': 'bmp', 'video/mp4': 'mp4', 'video/webm': 'webm', 'video/quicktime': 'mov',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/ogg': 'ogg',
    'audio/webm': 'weba', 'application/pdf': 'pdf', 'text/plain': 'txt',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'application/vnd.ms-powerpoint': 'ppt',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
}
CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css',
    '.json': 'application/json', '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.log': 'text/plain; charset=utf-8', '.csv': 'text/plain; charset=utf-8', '.xml': 'text/plain; charset=utf-8',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.jfif': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
    '.webp': 'image/webp', '.svg': 'image/svg+xml', '.avif': 'image/avif', '.bmp': 'image/bmp', '.ico': 'image/x-icon',
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
    '.mkv': 'video/x-matroska', '.ogv': 'video/ogg', '.3gp': 'video/3gpp',
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.m4a': 'audio/mp4',
    '.aac': 'audio/aac', '.flac': 'audio/flac', '.opus': 'audio/ogg', '.weba': 'audio/webm',
    '.pdf': 'application/pdf',
}


def ext_of(name) -> str:
    return os.path.splitext(str(name or ''))[1][1:].lower()


def detect_type(name, mime='') -> str:
    e = ext_of(name)
    for t, lst in EXT.items():
        if e in lst:
            return t
    m = str(mime or '').lower()
    if m.startswith('image/'):
        return 'image'
    if m.startswith('video/'):
        return 'video'
    if m.startswith('audio/'):
        return 'audio'
    if m == 'application/pdf':
        return 'pdf'
    if 'presentation' in m or 'powerpoint' in m:
        return 'presentation'
    if 'word' in m or 'spreadsheet' in m or 'excel' in m:
        return 'document'
    if m.startswith('text/'):
        return 'text'
    return 'other'


def safe_name(name) -> str:
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name or 'file')).lstrip('.').strip() or 'file'
    if len(n) > 120:
        root, e = os.path.splitext(n)
        e = e[:12]
        n = root[:120 - len(e)] + e
    return n


def ext_from_url(url) -> str:
    try:
        e = ext_of(urllib.parse.urlparse(url).path)
        return e if re.fullmatch(r'[a-z0-9]{2,5}', e) else ''
    except Exception:
        return ''


def ext_from_mime(mime) -> str:
    return MIME_EXT.get(str(mime or '').split(';')[0].strip().lower(), '')


def rid() -> str:
    return secrets.token_hex(6)


def _ssl_context():
    """System certificate store plus certifi's list (if bundled), so downloads work everywhere."""
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = _ssl_context()


def http_open(url, headers=None, timeout=15, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def get_json(url, headers=None, timeout=15):
    with http_open(url, {'Accept': 'application/json', **(headers or {})}, timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def download_to(url, dest, max_bytes=8 * 1024 ** 3, timeout=60, progress=None):
    tmp = str(dest) + '.part'
    size = 0
    try:
        with http_open(url, timeout=timeout) as r, open(tmp, 'wb') as f:
            mime = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            total = int(r.headers.get('Content-Length') or 0)
            if total > max_bytes:
                raise ValueError('TOO_LARGE')
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('TOO_LARGE')
                f.write(chunk)
                if progress:
                    progress(size, total)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return size, mime


def _decode_entities(s):
    import html as _h
    return re.sub(r'\s+', ' ', _h.unescape(s or '')).strip()


def _meta(html_text, names):
    for n in names:
        for pat in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']*)["\']',
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(n)}["\']'):
            m = re.search(pat, html_text, re.I)
            if m and m.group(1):
                return _decode_entities(m.group(1))
    return ''


def youtube_id(url):
    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or ''
        if host == 'youtu.be':
            return u.path[1:12]
        if host.endswith('youtube.com'):
            q = urllib.parse.parse_qs(u.query)
            if q.get('v'):
                return q['v'][0]
            m = re.search(r'/(shorts|embed|live)/([\w-]{6,})', u.path)
            if m:
                return m.group(2)
    except Exception:
        pass
    return None


def inspect_link(url) -> dict:
    """Returns {'direct', 'mime', 'fileName'} for direct files, else {'title', 'image'}."""
    yt = youtube_id(url)
    if yt:
        title = ''
        try:
            title = get_json('https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(url, safe=''),
                             timeout=6).get('title', '')
        except Exception:
            pass
        return {'title': title or 'YouTube', 'image': f'https://i.ytimg.com/vi/{yt}/hqdefault.jpg'}
    try:
        with http_open(url, {'User-Agent': BROWSER_UA, 'Accept': 'text/html,*/*',
                             'Accept-Language': 'nl-BE,nl;q=0.9,en;q=0.8'}, timeout=8) as r:
            mime = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            dt = detect_type('', mime)
            if dt in ('image', 'video', 'audio', 'pdf', 'presentation', 'document'):
                fn = ''
                m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', r.headers.get('Content-Disposition') or '', re.I)
                if m:
                    fn = urllib.parse.unquote(m.group(1))
                if not fn:
                    fn = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(r.geturl()).path))
                return {'direct': True, 'mime': mime, 'fileName': fn}
            if 'html' not in mime:
                return {}
            text = r.read(600 * 1024).decode('utf-8', 'ignore')
            final = r.geturl()
        title = _meta(text, ['og:title', 'twitter:title'])
        if not title:
            m = re.search(r'<title[^>]*>([^<]{1,300})</title>', text, re.I)
            title = _decode_entities(m.group(1)) if m else ''
        image = _meta(text, ['og:image', 'og:image:url', 'twitter:image', 'twitter:image:src'])
        if image:
            image = urllib.parse.urljoin(final, image)
        return {'title': title, 'image': image}
    except Exception:
        return {}


def shorten(url) -> str:
    for base in ('https://is.gd/create.php', 'https://v.gd/create.php'):
        try:
            with http_open(f'{base}?format=simple&url={urllib.parse.quote(url, safe="")}', timeout=5) as r:
                t = r.read().decode().strip()
                if re.fullmatch(r'https?://\S+', t):
                    return t
        except Exception:
            pass
    return url


def qr_data_uri(text):
    try:
        if segno:
            return segno.make_qr(text, error='m').png_data_uri(scale=14, border=1, dark='#0f1a18', light='#ffffff')
    except Exception:
        traceback.print_exc()
    return QRMini(text, ecl=1).svg_data_uri()


# --------------------------------------------------------------------------------------------
# Built-in QR code encoder (no dependencies). Byte mode, versions 1-40, ECC L/M/Q/H.
# --------------------------------------------------------------------------------------------
class QRMini:
    ECC_PER_BLOCK = (
        (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
        (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
        (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
        (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    )
    NUM_BLOCKS = (
        (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
        (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
        (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
        (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
    )
    FORMAT = (1, 0, 3, 2)  # format bits for L, M, Q, H

    def __init__(self, text, ecl=1, version=None, mask=None):
        data = text.encode('utf-8') if isinstance(text, str) else bytes(text)
        self.ecl = ecl
        for ver in range(1, 41):
            cap = self._data_codewords(ver, ecl) * 8
            if 4 + (8 if ver <= 9 else 16) + len(data) * 8 <= cap and (version is None or ver >= version):
                break
        else:
            raise ValueError('data too long for a QR code')
        self.version = ver
        self.size = ver * 4 + 17
        bits = []

        def put(val, n):
            for i in reversed(range(n)):
                bits.append((val >> i) & 1)
        put(4, 4)
        put(len(data), 8 if ver <= 9 else 16)
        for b in data:
            put(b, 8)
        put(0, min(4, cap - len(bits)))
        put(0, (-len(bits)) % 8)
        pad = 0xEC
        while len(bits) < cap:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        n = self.size
        self.m = [[False] * n for _ in range(n)]
        self.fn = [[False] * n for _ in range(n)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        if mask is None:
            best, mask = None, 0
            for msk in range(8):
                self._apply_mask(msk)
                self._draw_format(msk)
                p = self._penalty()
                if best is None or p < best:
                    best, mask = p, msk
                self._apply_mask(msk)
        self._apply_mask(mask)
        self._draw_format(mask)
        self.mask = mask

    @staticmethod
    def _raw_modules(ver):
        r = (16 * ver + 128) * ver + 64
        if ver >= 2:
            na = ver // 7 + 2
            r -= (25 * na - 10) * na - 55
            if ver >= 7:
                r -= 36
        return r

    @classmethod
    def _data_codewords(cls, ver, ecl):
        return cls._raw_modules(ver) // 8 - cls.ECC_PER_BLOCK[ecl][ver] * cls.NUM_BLOCKS[ecl][ver]

    @staticmethod
    def _gmul(x, y):
        z = 0
        for i in reversed(range(8)):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        return z

    def _rs_divisor(self, degree):
        res = [0] * (degree - 1) + [1]
        root = 1
        for _ in range(degree):
            for j in range(degree):
                res[j] = self._gmul(res[j], root)
                if j + 1 < degree:
                    res[j] ^= res[j + 1]
            root = self._gmul(root, 0x02)
        return res

    def _rs_remainder(self, data, div):
        res = [0] * len(div)
        for b in data:
            f = b ^ res.pop(0)
            res.append(0)
            for i, c in enumerate(div):
                res[i] ^= self._gmul(c, f)
        return res

    def _add_ecc(self, data):
        ver, ecl = self.version, self.ecl
        nblocks, ecclen = self.NUM_BLOCKS[ecl][ver], self.ECC_PER_BLOCK[ecl][ver]
        raw = self._raw_modules(ver) // 8
        nshort = nblocks - raw % nblocks
        shortlen = raw // nblocks
        div = self._rs_divisor(ecclen)
        blocks, k = [], 0
        for i in range(nblocks):
            dat = data[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
            k += len(dat)
            ecc = self._rs_remainder(dat, div)
            if i < nshort:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortlen - ecclen or j >= nshort:
                    out.append(blk[i])
        return out

    def _set(self, x, y, dark):
        self.m[y][x] = dark
        self.fn[y][x] = True

    def _align_positions(self):
        ver = self.version
        if ver == 1:
            return []
        na = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + na * 2 + 1) // (na * 2 - 2) * 2
        res = [self.size - 7 - i * step for i in range(na - 1)] + [6]
        return list(reversed(res))

    def _draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._align_positions()
        last = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _draw_format(self, mask):
        n = self.size
        data = self.FORMAT[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            self._set(8, i, g(i))
        self._set(8, 7, g(6))
        self._set(8, 8, g(7))
        self._set(7, 8, g(8))
        for i in range(9, 15):
            self._set(14 - i, 8, g(i))
        for i in range(8):
            self._set(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, g(i))
        self._set(8, n - 8, True)

    def _draw_codewords(self, data):
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    up = ((right + 1) & 2) == 0
                    y = n - 1 - vert if up else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.m[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, mask):
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.size):
            row, frow = self.m[y], self.fn[y]
            for x in range(self.size):
                if not frow[x] and f(x, y):
                    row[x] = not row[x]

    def _penalty(self):
        n, m, p = self.size, self.m, 0
        lines = m + [[m[y][x] for y in range(n)] for x in range(n)]
        pat1 = [True, False, True, True, True, False, True, False, False, False, False]
        pat2 = pat1[::-1]
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, v
            if run >= 5:
                p += run - 2
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    p += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(r) for r in m)
        p += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return p

    def svg_data_uri(self, border=2, dark='#0f1a18', light='#ffffff'):
        import base64
        size = self.size + border * 2
        path = ''.join(f'M{x + border} {y + border}h1v1h-1z'
                       for y in range(self.size) for x in range(self.size) if self.m[y][x])
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
               f'<rect width="100%" height="100%" fill="{light}"/><path fill="{dark}" d="{path}"/></svg>')
        return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()



# ---- Office -> PDF (LibreOffice, optional) ----
_soffice = None
_soffice_checked = False
_convert_lock = threading.Lock()


def find_soffice():
    global _soffice, _soffice_checked
    if _soffice_checked:
        return _soffice
    cands = []
    if IS_WIN:
        for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'), r'C:\Program Files'):
            if base:
                cands.append(os.path.join(base, 'LibreOffice', 'program', 'soffice.exe'))
    elif sys.platform == 'darwin':
        cands.append('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    else:
        cands += ['/usr/bin/soffice', '/usr/bin/libreoffice', '/usr/lib/libreoffice/program/soffice', '/snap/bin/libreoffice']
    _soffice = next((c for c in cands if os.path.exists(c)), None) or shutil.which('soffice') or shutil.which('libreoffice')
    _soffice_checked = True
    return _soffice


def convert_to_pdf(src: Path) -> Path:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError('no-office')
    with _convert_lock:
        subprocess.run([soffice, f'-env:UserInstallation={OFFICE_PROFILE.as_uri()}', '--headless', '--norestore',
                        '--nologo', '--convert-to', 'pdf', '--outdir', str(PREVIEWS), str(src)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, creationflags=NO_WINDOW)
    out = PREVIEWS / (src.stem + '.pdf')
    if not out.exists():
        raise RuntimeError('convert-failed')
    return out


# --------------------------------------------------------------------------------------------
# Portable helper programs (ffmpeg, yt-dlp, Deno)
# --------------------------------------------------------------------------------------------
FROZEN = bool(getattr(sys, 'frozen', False))
# Our own bin folder comes first, so downloaded tools are found without touching the system PATH.
os.environ['PATH'] = str(BIN) + os.pathsep + os.environ.get('PATH', '')


def _arch():
    m = platform.machine().lower()
    return 'arm64' if m in ('arm64', 'aarch64') else 'x64'


class Deps:
    """Downloads portable helper programs into the app's bin folder (works inside a frozen .exe)."""

    NAMES = ('ffmpeg', 'yt-dlp', 'deno')

    def __init__(self):
        self.state = {n: {'status': 'idle', 'progress': 0.0, 'error': None} for n in self.NAMES}
        self.locks = {n: threading.Lock() for n in self.NAMES}
        self._last_emit = 0.0

    @staticmethod
    def exe_name(name):
        return name + ('.exe' if IS_WIN else '')

    def path(self, name):
        local = BIN / self.exe_name(name)
        if local.exists():
            return str(local)
        return shutil.which(name)

    def spec(self, name):
        """(url, member inside archive or None for a plain binary)."""
        a = _arch()
        gh = 'https://github.com/'
        if name == 'ffmpeg':
            if IS_WIN:
                return gh + 'BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip', 'ffmpeg.exe'
            if sys.platform == 'darwin':
                return 'https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip', 'ffmpeg'
            return (gh + 'BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-'
                    + ('linuxarm64' if a == 'arm64' else 'linux64') + '-lgpl.tar.xz'), 'ffmpeg'
        if name == 'yt-dlp':
            base = gh + 'yt-dlp/yt-dlp/releases/latest/download/'
            if IS_WIN:
                return base + 'yt-dlp.exe', None
            if sys.platform == 'darwin':
                return base + 'yt-dlp_macos', None
            return base + ('yt-dlp_linux_aarch64' if a == 'arm64' else 'yt-dlp_linux'), None
        if name == 'deno':
            base = gh + 'denoland/deno/releases/latest/download/deno-'
            if IS_WIN:
                return base + 'x86_64-pc-windows-msvc.zip', 'deno.exe'
            if sys.platform == 'darwin':
                return base + ('aarch64' if a == 'arm64' else 'x86_64') + '-apple-darwin.zip', 'deno'
            return base + ('aarch64' if a == 'arm64' else 'x86_64') + '-unknown-linux-gnu.zip', 'deno'
        raise ValueError(name)

    def public(self):
        out = {}
        for n in self.NAMES:
            st = dict(self.state[n])
            st['installed'] = bool(self.path(n))
            st['local'] = (BIN / self.exe_name(n)).exists()
            out[n] = st
        return out

    def _emit(self, force=False):
        now = time.time()
        if force or now - self._last_emit > 0.5:
            self._last_emit = now
            bus.emit('deps', self.public())

    def ensure(self, name, force=False):
        """Returns the program path, downloading it first when needed. Thread-safe."""
        if not force:
            p = self.path(name)
            if p:
                return p
        with self.locks[name]:
            if not force:
                p = self.path(name)
                if p:
                    return p
            st = self.state[name]
            st.update(status='downloading', progress=0.0, error=None)
            self._emit(True)
            print(f'[deps] downloading {name}…')
            try:
                url, member = self.spec(name)
                target = BIN / self.exe_name(name)
                tmp = BIN / (name + '.download')

                def prog(done, total):
                    st['progress'] = (done / total) if total else 0.0
                    self._emit()
                download_to(url, tmp, 600 * 1024 * 1024, timeout=60, progress=prog)
                if member:
                    self._extract(tmp, member, target)
                    tmp.unlink(missing_ok=True)
                else:
                    os.replace(tmp, target)
                if not IS_WIN:
                    target.chmod(0o755)
                st.update(status='ready', progress=1.0)
                print(f'[deps] {name} ready: {target}')
                return str(target)
            except Exception as e:
                st.update(status='error', error=str(e)[:200])
                print(f'[deps] {name} failed: {e}')
                raise
            finally:
                self._emit(True)

    @staticmethod
    def _extract(archive: Path, member: str, target: Path):
        import zipfile
        tmp_target = target.with_suffix(target.suffix + '.new')
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                names = [n for n in z.namelist() if os.path.basename(n) == member]
                if not names:
                    raise RuntimeError(f'{member} not found in download')
                with z.open(sorted(names, key=len)[0]) as src, open(tmp_target, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        else:
            with tarfile.open(archive) as tf:
                mem = [m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name) == member]
                if not mem:
                    raise RuntimeError(f'{member} not found in download')
                with tf.extractfile(sorted(mem, key=lambda m: len(m.name))[0]) as src, open(tmp_target, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp_target, target)

    def ensure_async(self, name, force=False):
        def run():
            try:
                self.ensure(name, force)
            except Exception:
                pass
        return spawn(run)


deps = Deps()


# --------------------------------------------------------------------------------------------
# Cloudflare quick tunnels (no account, no login)
# --------------------------------------------------------------------------------------------
_cf_lock = threading.Lock()
TRY_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.I)


def ensure_cloudflared(on_download=None) -> str:
    with _cf_lock:
        exe = BIN / ('cloudflared.exe' if IS_WIN else 'cloudflared')
        if exe.exists():
            return str(exe)
        found = shutil.which('cloudflared')
        if found:
            return found
        if on_download:
            on_download()
        machine = platform.machine().lower()
        arch = 'arm64' if machine in ('arm64', 'aarch64') else ('386' if machine in ('i386', 'i686', 'x86') else 'amd64')
        rel = 'https://github.com/cloudflare/cloudflared/releases/latest/download/'
        if IS_WIN:
            download_to(rel + f'cloudflared-windows-{"386" if arch == "386" else "amd64"}.exe', exe, timeout=120)
        elif sys.platform == 'darwin':
            tgz = BIN / 'cloudflared.tgz'
            download_to(rel + f'cloudflared-darwin-{"arm64" if arch == "arm64" else "amd64"}.tgz', tgz, timeout=120)
            with tarfile.open(tgz) as tf:
                tf.extractall(BIN)
            tgz.unlink(missing_ok=True)
        else:
            download_to(rel + f'cloudflared-linux-{arch}', exe, timeout=120)
        if not IS_WIN:
            exe.chmod(0o755)
        return str(exe)


class Tunnel:
    def __init__(self, name: str, event: str):
        self.name = name
        self.event = event
        self.port = None
        self.url = None
        self.status = 'idle'
        self.error = None
        self.proc = None
        self.stopped = True
        self.wake = threading.Event()
        self.listeners = []

    def state(self):
        return {'status': self.status, 'url': self.url, 'error': self.error,
                'since': int(getattr(self, 'started_at', time.time()) * 1000)}

    def _set(self, status, error=None):
        self.status, self.error = status, error
        bus.emit(self.event, self.state())
        for fn in self.listeners:
            try:
                fn(self.state())
            except Exception:
                traceback.print_exc()

    def start(self, port):
        self.port = port
        self.stopped = False
        spawn(self._run)

    def _sleep(self, sec):
        self.wake.wait(sec)
        self.wake.clear()

    def _run(self):
        backoff = 2
        while not self.stopped and not SHUTDOWN.is_set():
            try:
                binary = ensure_cloudflared(lambda: self._set('downloading'))
            except Exception as e:
                print(f'[{self.name}] cloudflared download failed: {e}')
                self._set('error', f'download: {e}')
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self.url = None
            self.started_at = time.time()
            self._set('starting')
            log_path = LOGS / f'cloudflared-{self.name}.log'
            try:
                log = open(log_path, 'w', encoding='utf-8', errors='ignore')
            except OSError:
                log = None
            last_err = ''
            try:
                p = subprocess.Popen([binary, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{self.port}'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     creationflags=NO_WINDOW)
            except Exception as e:
                self._set('error', str(e))
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            self.proc = p

            def watchdog(proc=p):
                time.sleep(45)
                if self.proc is proc and not self.url:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            spawn(watchdog)

            for raw in p.stderr:
                line = raw.decode('utf-8', 'ignore')
                if log:
                    log.write(line)
                    log.flush()
                if ' ERR ' in line or 'error' in line.lower():
                    last_err = re.sub(r'^\S+\s+ERR\s+', '', line.strip())[:160]
                if self.url is None:
                    m = TRY_RE.search(line)
                    if m:
                        self.url = m.group(0)
                        backoff = 2
                        print(f'[{self.name}] tunnel online after {time.time() - self.started_at:.1f}s: {self.url}')
                        self._set('online')
            p.wait()
            if log:
                log.close()
            self.proc = None
            self.url = None
            if self.stopped or SHUTDOWN.is_set():
                break
            msg = last_err or f'cloudflared stopped (code {p.returncode})'
            if 'config' in msg.lower():
                msg += ' — remove ~/.cloudflared/config.yml (quick tunnels do not work with it)'
            print(f'[{self.name}] tunnel error: {msg}  (log: {log_path})')
            self._set('error', msg)
            self._sleep(backoff)
            backoff = min(backoff * 2, 60)

    def restart(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.wake.set()

    def stop(self):
        self.stopped = True
        self.wake.set()
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


class LinkSession:
    """A temporary token link (+ short link + QR) on top of a tunnel."""

    def __init__(self, tunnel: Tunnel, prefix: str, event: str, ttl_key: str, grace_min: int, revoke_old: bool):
        self.tunnel = tunnel
        self.prefix = prefix
        self.event = event
        self.ttl_key = ttl_key
        self.grace = grace_min * 60
        self.revoke_old = revoke_old
        self.tokens: dict[str, float] = {}
        self.token = None
        self.expires_at = 0.0
        self.url = self.short = self.qr = None
        self.seq = 0
        self.timer = None
        self.lock = threading.Lock()

    def valid(self, t) -> bool:
        exp = self.tokens.get(t)
        if not exp:
            return False
        if time.time() > exp:
            self.tokens.pop(t, None)
            return False
        return True

    def _new_token(self):
        ttl = settings.get(self.ttl_key, 0) * 60
        t = secrets.token_urlsafe(9)
        if self.revoke_old:
            self.tokens.clear()
        self.tokens[t] = time.time() + ttl + self.grace if ttl else math.inf
        self.token = t
        self.expires_at = time.time() + ttl if ttl else 0

    def public(self):
        return {'url': self.url, 'shortUrl': self.short, 'qr': self.qr,
                'expiresAt': int(self.expires_at * 1000), 'ttl': settings.get(self.ttl_key, 0) * 60000,
                'qrAvailable': True}

    def refresh(self, rotate=True):
        spawn(self._refresh, rotate)
        return self.public()

    def _refresh(self, rotate):
        with self.lock:
            self.seq += 1
            seq = self.seq
            if rotate or not self.token:
                self._new_token()
            if self.timer:
                self.timer.cancel()
            if self.expires_at:
                self.timer = threading.Timer(max(1, self.expires_at - time.time()), self.refresh)
                self.timer.daemon = True
                self.timer.start()
            if not self.tunnel.url:
                self.url = self.short = self.qr = None
                bus.emit(self.event, self.public())
                return
            full = f'{self.tunnel.url}{self.prefix}{self.token}'
            self.url, self.short, self.qr = full, None, qr_data_uri(full)
            bus.emit(self.event, self.public())
        short = shorten(full)
        if seq == self.seq:
            self.short = short
            bus.emit(self.event, self.public())


upload_tunnel = Tunnel('upload', 'tunnel')
remote_tunnel = Tunnel('remote', 'rtunnel')
upload_session = LinkSession(upload_tunnel, '/u/', 'session', 'linkRefreshMin', 10, revoke_old=False)
remote_session = LinkSession(remote_tunnel, '/r/', 'remote:session', 'remoteRefreshMin', 0, revoke_old=True)


def _on_tunnel(session):
    def fn(state):
        if state['status'] == 'online':
            session.refresh(True)
        elif session.url:
            session.refresh(False)
    return fn


upload_tunnel.listeners.append(_on_tunnel(upload_session))
remote_tunnel.listeners.append(_on_tunnel(remote_session))

# --------------------------------------------------------------------------------------------
# Items (gallery)
# --------------------------------------------------------------------------------------------
def items():
    with items_lock:
        return db.get('items') or []


def get_item(i):
    return next((x for x in items() if x['id'] == i), None)


def save_items(lst):
    with items_lock:
        db.set('items', lst)
    bus.emit('items', lst)


def add_item(it):
    with items_lock:
        save_items([it] + items())
    return it


def update_item(i, **patch):
    with items_lock:
        lst = [({**x, **patch} if x['id'] == i else x) for x in items()]
        save_items(lst)
    return get_item(i)


def delete_files_of(it):
    for p in ((UPLOADS / it['file']) if it.get('file') else None, (PREVIEWS / it['preview']) if it.get('preview') else None,
              (PREVIEWS / it['poster']) if it.get('poster') else None):
        if p:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def remove_item(i):
    it = get_item(i)
    if not it:
        return
    delete_files_of(it)
    with items_lock:
        save_items([x for x in items() if x['id'] != i])


def cleanup_items():
    lst = []
    for x in items():
        if x.get('type') == 'link' or (x.get('file') and (UPLOADS / x['file']).exists()):
            if x.get('status') == 'processing':
                x = {**x, 'status': 'ready'}
            lst.append(x)
    db.set('items', lst)


def host_of(u):
    try:
        return (urllib.parse.urlparse(u).hostname or '').removeprefix('www.')
    except Exception:
        return ''


def favicon_for(host):
    return f'https://www.google.com/s2/favicons?domain={urllib.parse.quote(host)}&sz=256'


def max_bytes():
    return settings.get('maxUploadMB') * 1024 * 1024


ENGINE = {'h264': True}
H264_EXT = ('mp4', 'mov', 'm4v', 'mkv', 'avi', 'wmv', '3gp')
_video_lock = threading.Lock()


def ffmpeg_path():
    return deps.path('ffmpeg')


def prepare_video_support():
    """Engine can't play H.264: fetch a portable ffmpeg (if needed) and convert existing videos."""
    try:
        deps.ensure('ffmpeg')
    except Exception:
        return
    convert_all_videos()


def make_poster(i):
    """Small JPEG frame for the gallery (no need to load the whole video for a thumbnail)."""
    it = get_item(i)
    ff = ffmpeg_path()
    if not it or it.get('type') != 'video' or it.get('poster') or not it.get('file') or not ff:
        return
    src = UPLOADS / it['file']
    out = PREVIEWS / (src.stem + '.poster.jpg')
    for ss in ('1', '0'):
        try:
            subprocess.run([ff, '-y', '-loglevel', 'error', '-ss', ss, '-i', str(src), '-frames:v', '1',
                            '-vf', "scale='min(640,iw)':-2", '-q:v', '4', str(out)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, creationflags=NO_WINDOW)
        except Exception:
            pass
        if out.exists() and out.stat().st_size > 500:
            update_item(i, poster=out.name)
            return


def live_video_cmd(source, ff):
    """ffmpeg command that streams any video as WebM (VP8/Vorbis) in real time."""
    cmd = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin']
    if str(source).startswith('https://'):
        cmd += ['-reconnect', '1', '-reconnect_streamed', '1', '-user_agent', BROWSER_UA]
    return cmd + ['-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-vf', "scale='min(1280,iw)':-2",
                  '-c:v', 'libvpx', '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '3M', '-maxrate', '4M',
                  '-bufsize', '8M', '-g', '50', '-threads', str(max(2, (os.cpu_count() or 4) - 1)),
                  '-c:a', 'libvorbis', '-b:a', '128k', '-f', 'webm', 'pipe:1']


def convert_video(i):
    """Make a WebM copy (VP8/Vorbis) for web engines that can't play H.264, e.g. the pip Qt build."""
    it = get_item(i)
    if not it or it.get('type') != 'video' or it.get('preview') or not it.get('file'):
        return
    if ENGINE['h264'] or ext_of(it['file']) not in H264_EXT:
        return
    if not ffmpeg_path():
        try:
            deps.ensure('ffmpeg')
        except Exception:
            update_item(i, previewError='video-convert-failed')
            return
    update_item(i, converting=True)
    src = UPLOADS / it['file']
    out = PREVIEWS / (src.stem + '.webm')
    with _video_lock:
        try:
            r = subprocess.run([ffmpeg_path(), '-y', '-loglevel', 'error', '-i', str(src),
                                '-vf', "scale='min(1920,iw)':-2", '-c:v', 'libvpx', '-b:v', '5M', '-deadline', 'realtime',
                                '-cpu-used', '8', '-threads', str(max(2, (os.cpu_count() or 4) - 1)),
                                '-c:a', 'libvorbis', '-b:a', '160k', str(out)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600, creationflags=NO_WINDOW)
            ok = r.returncode == 0 and out.exists() and out.stat().st_size > 1000
        except Exception:
            ok = False
    if ok:
        update_item(i, preview=out.name, converting=False)
        bus.emit('video:ready', get_item(i))
    else:
        out.unlink(missing_ok=True)
        update_item(i, converting=False, previewError='video-convert-failed')


def convert_all_videos():
    for it in items():
        if it.get('type') == 'video' and not it.get('poster'):
            make_poster(it['id'])
    for it in items():
        if it.get('type') == 'video' and not it.get('preview') and not it.get('previewError'):
            convert_video(it['id'])


def convert_if_needed(i):
    it = get_item(i)
    if it and it.get('type') == 'video':
        if not ENGINE['h264'] and not ffmpeg_path():
            try:
                deps.ensure('ffmpeg')
            except Exception:
                pass
        make_poster(i)
        return convert_video(i)
    if not it or it.get('type') not in ('presentation', 'document'):
        return
    update_item(i, status='processing')
    try:
        pdf = convert_to_pdf(UPLOADS / it['file'])
        update_item(i, status='ready', preview=pdf.name, previewError=None)
    except Exception as e:
        update_item(i, status='ready', previewError='no-office' if str(e) == 'no-office' else 'convert-failed')


def on_uploaded_file(file, name, mime, size):
    it = add_item({'id': rid(), 'type': detect_type(name, mime), 'name': name, 'file': file, 'mime': mime,
                   'size': size, 'createdAt': int(time.time() * 1000), 'status': 'ready', 'origin': 'upload'})
    bus.emit('item:added', it)
    spawn(convert_if_needed, it['id'])
    return it['id']


def on_uploaded_link(url):
    host = host_of(url)
    it = add_item({'id': rid(), 'type': 'link', 'name': host or url, 'url': url, 'createdAt': int(time.time() * 1000),
                   'status': 'processing', 'origin': 'upload', 'thumb': favicon_for(host), 'thumbIsIcon': True})
    bus.emit('item:added', it)

    def work():
        meta = inspect_link(url)
        if not meta.get('direct'):
            update_item(it['id'], status='ready', name=meta.get('title') or host or url,
                        thumb=meta.get('image') or favicon_for(host), thumbIsIcon=not meta.get('image'))
            return
        try:  # the link is a file: download it
            name = safe_name(meta.get('fileName') or host or 'file')
            file = f'{int(time.time() * 1000)}-{rid()}-{name}'
            size, mime = download_to(url, UPLOADS / file, max_bytes(), timeout=60)
            if not os.path.splitext(file)[1]:
                e = ext_from_mime(mime)
                if e:
                    os.replace(UPLOADS / file, UPLOADS / f'{file}.{e}')
                    file += '.' + e
            update_item(it['id'], type=detect_type(file, mime), file=file, mime=mime, size=size, name=name,
                        status='ready', thumb=None, thumbIsIcon=False)
            convert_if_needed(it['id'])  # documents -> PDF, videos -> WebM when needed
        except Exception:
            update_item(it['id'], status='ready')
    spawn(work)
    return it['id']


def import_from_library(r):
    url = str(resolve_library_url(r or {}) or '')
    if (r or {}).get('downloadLocation') and settings.get('unsplashKey'):
        try:
            get_json(r['downloadLocation'], {'Authorization': f'Client-ID {settings.get("unsplashKey")}'}, timeout=8)
        except Exception:
            pass
    if not url.startswith('https://'):
        raise ValueError('bad item')
    kind = 'video' if r.get('kind') == 'video' else 'image'
    base = re.sub(r'\.[a-z0-9]{2,5}$', '', safe_name(str(r.get('title') or kind)[:70]), flags=re.I)
    tmp = f'{int(time.time() * 1000)}-{rid()}-{base}'
    size, mime = download_to(url, UPLOADS / tmp, 2 * 1024 ** 3, timeout=60)
    e = ext_from_url(url) or ext_from_mime(mime) or ('mp4' if kind == 'video' else 'jpg')
    file = f'{tmp}.{e}'
    os.replace(UPLOADS / tmp, UPLOADS / file)
    it = add_item({'id': rid(), 'type': kind, 'name': r.get('title') or base, 'file': file, 'mime': mime,
                     'size': size, 'createdAt': int(time.time() * 1000), 'status': 'ready', 'origin': 'library',
                     'attribution': {k: r.get(v) or '' for k, v in
                                     (('creator', 'creator'), ('license', 'license'), ('source', 'sourceName'),
                                      ('pageUrl', 'pageUrl'))}})
    spawn(convert_if_needed, it['id'])
    return it


# --------------------------------------------------------------------------------------------
# Library: real stock photos & videos (Pexels, Pixabay, Unsplash) — free API keys, no watermarks
# --------------------------------------------------------------------------------------------
LIB_KEYS = {'pexels': 'pexelsKey', 'pixabay': 'pixabayKey', 'unsplash': 'unsplashKey'}
LIB_KINDS = {'pexels': ('image', 'video'), 'pixabay': ('image', 'video'), 'unsplash': ('image',)}


def _lib_get(name, url, headers=None):
    try:
        return get_json(url, headers, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise ValueError(f'INVALID_KEY:{name}')
        if e.code == 429:
            raise ValueError(f'RATE_LIMIT:{name}')
        raise


def lib_pexels(q, kind, page, key):
    h = {'Authorization': key}
    if kind == 'video':
        j = _lib_get('pexels', 'https://api.pexels.com/videos/search?' + urllib.parse.urlencode(
            {'query': q, 'per_page': 24, 'page': page}), h)
        res = []
        for v in j.get('videos', []):
            files = [f for f in v.get('video_files') or [] if f.get('link') and 'mp4' in (f.get('file_type') or 'mp4')]
            if not files:
                continue
            by = lambda t: min(files, key=lambda f: abs((f.get('height') or 0) - t))  # noqa: E731
            res.append({'id': f'px-v{v["id"]}', 'kind': 'video', 'title': q.capitalize(), 'thumb': v.get('image'),
                        'url': by(1080)['link'], 'preview': by(540)['link'],
                        'creator': (v.get('user') or {}).get('name', ''), 'license': 'Pexels License',
                        'sourceName': 'Pexels', 'pageUrl': v.get('url', ''), 'duration': v.get('duration')})
        return res, bool(j.get('next_page'))
    j = _lib_get('pexels', 'https://api.pexels.com/v1/search?' + urllib.parse.urlencode(
        {'query': q, 'per_page': 24, 'page': page}), h)
    res = [{'id': f'px-{p["id"]}', 'kind': 'image', 'title': p.get('alt') or q.capitalize(),
            'thumb': p['src'].get('medium'), 'url': p['src'].get('large2x') or p['src'].get('original'),
            'fallback': p['src'].get('large'), 'creator': p.get('photographer', ''), 'license': 'Pexels License',
            'sourceName': 'Pexels', 'pageUrl': p.get('url', '')} for p in j.get('photos', [])]
    return res, bool(j.get('next_page'))


def lib_pixabay(q, kind, page, key):
    video = kind == 'video'
    params = {'key': key, 'q': q[:100], 'per_page': 30, 'page': page, 'safesearch': 'true'}
    if not video:
        params['image_type'] = 'photo'
    j = _lib_get('pixabay', ('https://pixabay.com/api/videos/?' if video else 'https://pixabay.com/api/?')
                 + urllib.parse.urlencode(params))
    res = []
    for hit in j.get('hits', []):
        title = ', '.join(t.strip() for t in str(hit.get('tags') or q).split(',')[:3]).capitalize()
        base = {'id': f'pb-{"v" if video else ""}{hit["id"]}', 'kind': kind, 'title': title,
                'creator': hit.get('user') or '', 'license': 'Pixabay Content License',
                'sourceName': 'Pixabay', 'pageUrl': hit.get('pageURL') or ''}
        if video:
            v = hit.get('videos') or {}
            full = (v.get('large') or {}).get('url') or (v.get('medium') or {}).get('url')
            if not full:
                continue
            thumb = next((v[k]['thumbnail'] for k in ('medium', 'small', 'large', 'tiny')
                          if (v.get(k) or {}).get('thumbnail')), '')
            if not thumb and hit.get('picture_id'):
                thumb = f'https://i.vimeocdn.com/video/{hit["picture_id"]}_640x360.jpg'
            res.append({**base, 'url': full, 'preview': (v.get('small') or v.get('tiny') or {}).get('url') or full,
                        'thumb': thumb, 'duration': hit.get('duration')})
        else:
            res.append({**base, 'url': hit.get('largeImageURL') or hit.get('webformatURL'),
                        'thumb': hit.get('webformatURL'), 'fallback': hit.get('webformatURL')})
    return res, page * 30 < min(int(j.get('totalHits') or 0), 500)


def lib_unsplash(q, kind, page, key):
    j = _lib_get('unsplash', 'https://api.unsplash.com/search/photos?' + urllib.parse.urlencode(
        {'query': q, 'per_page': 24, 'page': page, 'content_filter': 'high'}),
        {'Authorization': f'Client-ID {key}', 'Accept-Version': 'v1'})
    res = []
    for p in j.get('results', []):
        u = p.get('urls') or {}
        raw = u.get('raw')
        res.append({'id': f'us-{p["id"]}', 'kind': 'image',
                    'title': (p.get('alt_description') or p.get('description') or q).capitalize(),
                    'thumb': u.get('small'), 'url': f'{raw}&w=2560&fit=max&q=85' if raw else u.get('full'),
                    'fallback': u.get('regular'), 'creator': (p.get('user') or {}).get('name', ''),
                    'license': 'Unsplash License', 'sourceName': 'Unsplash',
                    'pageUrl': ((p.get('links') or {}).get('html') or '') + '?utm_source=fembimmo_display&utm_medium=referral',
                    'downloadLocation': (p.get('links') or {}).get('download_location') or ''})
    return res, page < int(j.get('total_pages') or 0)


LIB_SOURCES = {'pexels': lib_pexels, 'pixabay': lib_pixabay, 'unsplash': lib_unsplash}


def library_keys():
    return {n: bool(str(settings.get(k) or '').strip()) for n, k in LIB_KEYS.items()}


def library_search(query='', kind='image', source='auto', page=1, **_):
    q = str(query or '').strip()[:100]
    kind = 'video' if kind == 'video' else 'image'
    page = max(1, min(50, int(page or 1)))
    keys = library_keys()
    if source in LIB_SOURCES:
        if not keys[source]:
            raise ValueError(f'NO_KEY:{source}')
        if kind not in LIB_KINDS[source]:
            raise ValueError(f'NO_VIDEO:{source}')
        names = [source]
    else:
        names = [n for n in LIB_SOURCES if keys[n] and kind in LIB_KINDS[n]]
        if not names:
            raise ValueError('NO_KEYS' if not any(keys.values()) else 'NO_VIDEO_SOURCE')
    if not q:
        return {'results': [], 'hasMore': False, 'source': '+'.join(names), 'errors': []}
    out, errors = {}, []

    def run(name):
        try:
            out[name] = LIB_SOURCES[name](q, kind, page, str(settings.get(LIB_KEYS[name])).strip())
        except Exception as e:
            errors.append(str(e) if str(e).startswith(('INVALID_KEY', 'RATE_LIMIT')) else f'ERROR:{name}')
    threads = [spawn(run, n) for n in names]
    for t in threads:
        t.join(20)
    if not out:
        raise ValueError(errors[0] if errors else 'ERROR')
    lists = [out[n][0] for n in names if n in out]
    merged = []
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst):
                merged.append(lst[i])
    return {'results': merged, 'hasMore': any(out[n][1] for n in out),
            'source': '+'.join(n for n in names if n in out), 'errors': errors}


def resolve_library_url(r):
    return r.get('url')


# --------------------------------------------------------------------------------------------
# Windows: main, YouTube Music, web viewer
# --------------------------------------------------------------------------------------------
main_window = None


class JsApi:
    """Exposed as window.pywebview.api in every app window. Only methods, no attributes."""

    def web_close(self):
        webviewer.close()


js_api = JsApi()


class YouTube:
    """Music via yt-dlp: search YouTube and stream the audio through the local server."""

    FORMAT = 'bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio/best'
    MIME = {'webm': 'audio/webm', 'weba': 'audio/webm', 'm4a': 'audio/mp4', 'mp4': 'audio/mp4',
            'mp3': 'audio/mpeg', 'ogg': 'audio/ogg', 'opus': 'audio/ogg'}

    def __init__(self):
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}
        self.installing = False
        self.install_error = None
        self._mod = None

    def module(self):
        if self._mod is None:
            try:
                import importlib
                self._mod = importlib.import_module('yt_dlp')
            except Exception:
                self._mod = None
        return self._mod

    @staticmethod
    def js_runtime():
        for name in ('deno', 'node', 'nodejs', 'qjs', 'quickjs'):
            if shutil.which(name):
                return name
        return None

    def mode(self):
        """'module' = yt_dlp Python package (pip); 'binary' = portable yt-dlp program (used in a frozen exe)."""
        return 'module' if (not FROZEN and self.module()) else 'binary'

    @staticmethod
    def binary():
        return deps.path('yt-dlp')

    _bin_version = (None, None)

    def version(self):
        if self.mode() == 'module':
            try:
                return self.module().version.__version__
            except Exception:
                return '?'
        b = self.binary()
        if not b:
            return None
        try:
            mtime = os.path.getmtime(b)
        except OSError:
            mtime = None
        if self._bin_version[0] != (b, mtime):
            try:
                r = subprocess.run([b, '--version'], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
                self._bin_version = ((b, mtime), r.stdout.strip() or '?')
            except Exception:
                self._bin_version = ((b, mtime), '?')
        return self._bin_version[1]

    def status(self):
        ds = deps.state
        downloading = any(ds[n]['status'] == 'downloading' for n in ('yt-dlp', 'deno'))
        available = self.mode() == 'module' or bool(self.binary())
        return {'available': True, 'ready': available,
                'version': self.version() if available else None, 'installing': self.installing or downloading,
                'error': self.install_error or ds['yt-dlp']['error'], 'jsRuntime': self.js_runtime(),
                'canInstall': True, 'mode': self.mode()}

    def ensure_ready(self):
        """Background: make sure yt-dlp and a JavaScript runtime are available."""
        for name, needed in (('yt-dlp', self.mode() == 'binary' and not self.binary()), ('deno', not self.js_runtime())):
            if needed:
                try:
                    deps.ensure(name)
                except Exception:
                    pass
        bus.emit('yt:status', self.status())

    def install(self, upgrade=False):
        """Install/update yt-dlp: pip for the Python package, self-update/download for the portable program."""
        if self.installing:
            return
        self.installing, self.install_error = True, None
        bus.emit('yt:status', self.status())
        if self.mode() == 'binary':
            try:
                b = self.binary()
                ok = False
                if b and upgrade:
                    r = subprocess.run([b, '-U'], capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
                    ok = r.returncode == 0
                if not ok:
                    deps.ensure('yt-dlp', force=bool(b))
                if not self.js_runtime():
                    deps.ensure('deno')
                self._bin_version = (None, None)
                with self.lock:
                    self.cache.clear()
            except Exception as e:
                self.install_error = str(e)[:300]
            finally:
                self.installing = False
                bus.emit('yt:status', self.status())
            return
        try:
            cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check'] + \
                  (['-U'] if upgrade else []) + ['yt-dlp[default]']
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
            if r.returncode != 0 and 'externally-managed' in (r.stderr or '') + (r.stdout or ''):
                r = subprocess.run(cmd + ['--break-system-packages'], capture_output=True, text=True, timeout=900,
                                   creationflags=NO_WINDOW)
            if r.returncode != 0:
                self.install_error = ((r.stderr or '') + (r.stdout or '')).strip()[-400:] or 'pip failed'
            else:
                import importlib
                for k in [k for k in sys.modules if k == 'yt_dlp' or k.startswith('yt_dlp.')]:
                    sys.modules.pop(k, None)
                importlib.invalidate_caches()
                self._mod = None
                with self.lock:
                    self.cache.clear()
        except Exception as e:
            self.install_error = str(e)
        finally:
            self.installing = False
            bus.emit('yt:status', self.status())

    def _opts(self, **extra):
        o = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'skip_download': True, 'socket_timeout': 15}
        rt = self.js_runtime()
        if rt and rt != 'deno':  # Deno is enabled by default in yt-dlp; others must be switched on
            o['js_runtimes'] = {{'nodejs': 'node', 'qjs': 'quickjs'}.get(rt, rt): {}}
        o.update(extra)
        return o

    def _run_binary(self, url, flat=False, fmt=None):
        b = self.binary() or deps.ensure('yt-dlp')
        if not self.js_runtime():
            deps.ensure_async('deno')
        base = [b, '-J', '--no-warnings', '--no-playlist', '--no-progress', '--socket-timeout', '15']
        if flat:
            base.append('--flat-playlist')
        if fmt:
            base += ['-f', fmt]
        rt = self.js_runtime()
        variants = [base + ['--js-runtimes', {'nodejs': 'node', 'qjs': 'quickjs'}.get(rt, rt)]] if rt and rt != 'deno' else []
        variants.append(base)
        last = 'yt-dlp failed'
        for args in variants:
            r = subprocess.run(args + [url], capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=120, creationflags=NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            lines = [ln for ln in (r.stderr or '').strip().splitlines() if ln.strip()]
            last = lines[-1][:300] if lines else last
        raise RuntimeError(last)

    def _extract(self, url, **extra):
        if self.mode() == 'binary':
            return self._run_binary(url, flat=bool(extra.get('extract_flat')), fmt=extra.get('format'))
        m = self.module()
        opts = self._opts(**extra)
        try:
            with m.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception:
            if 'js_runtimes' not in opts:
                raise
            opts.pop('js_runtimes')
            with m.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

    def search(self, q, n=25):
        q = str(q or '').strip()[:150]
        if not q:
            return []
        info = self._extract(f'ytsearch{max(1, min(50, n))}:{q}', extract_flat='in_playlist')
        out = []
        for e in info.get('entries') or []:
            vid = (e or {}).get('id')
            if not vid or len(vid) != 11 or e.get('live_status') in ('is_live', 'is_upcoming'):
                continue
            out.append({'id': vid, 'title': e.get('title') or '', 'artist': e.get('channel') or e.get('uploader') or '',
                        'duration': int(e.get('duration') or 0), 'thumb': f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'})
        return out

    def resolve(self, vid, fresh=False):
        with self.lock:
            c = self.cache.get(vid)
        if c and not fresh and c['expires'] > time.time() + 120:
            return c
        info = self._extract(f'https://www.youtube.com/watch?v={vid}', format=self.FORMAT)
        fmt = info if info.get('url') else (info.get('requested_formats') or [{}])[0]
        url = fmt.get('url')
        if not url:
            raise RuntimeError('no audio stream found')
        m = re.search(r'[?&]expire=(\d+)', url)
        ext = fmt.get('ext') or info.get('ext') or 'webm'
        c = {'url': url, 'headers': fmt.get('http_headers') or info.get('http_headers') or {},
             'expires': int(m.group(1)) if m else time.time() + 3 * 3600, 'mime': self.MIME.get(ext, 'audio/webm')}
        with self.lock:
            self.cache[vid] = c
            if len(self.cache) > 300:
                for k in list(self.cache)[:100]:
                    self.cache.pop(k, None)
        return c

    def prefetch(self, vid):
        try:
            self.resolve(vid)
        except Exception:
            pass


yt = YouTube()


class WebViewer:
    """Opens shared websites full screen in their own window. Any key closes it."""
    INJECT = """(function(){if(window.__fd)return;window.__fd=1;
      function close(){if(window.pywebview&&window.pywebview.api)window.pywebview.api.web_close()}
      window.addEventListener('keydown',function(e){e.preventDefault();e.stopPropagation();close()},true);
      window.open=function(u){if(u)location.href=u;return null};
      var b=document.createElement('button');b.textContent='\\u2715';b.title='Close';
      b.style.cssText='position:fixed;top:16px;right:16px;z-index:2147483647;width:52px;height:52px;border-radius:50%;border:0;background:rgba(0,0,0,.65);color:#fff;font:600 22px system-ui;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4)';
      b.onclick=close;document.documentElement.appendChild(b)})()"""

    def __init__(self):
        self.win = None

    def open(self, url) -> bool:
        if webview is None or main_window is None:
            return False
        self.close(emit=False)
        w = webview.create_window('Fembimmo Web', url, fullscreen=True, on_top=True, js_api=js_api)
        w.events.loaded += lambda: self._inject(w)
        w.events.closed += lambda: self._closed(w)
        self.win = w
        return True

    def _inject(self, w):
        try:
            w.evaluate_js(self.INJECT)
        except Exception:
            pass

    def _closed(self, w):
        if self.win is w:
            self.win = None
        bus.emit('web:closed', True)

    def close(self, emit=True):
        w, self.win = self.win, None
        if w:
            try:
                w.destroy()
            except Exception:
                pass
        if emit:
            bus.emit('web:closed', True)


webviewer = WebViewer()


def toggle_fullscreen():
    on = not settings.get('fullscreen')
    settings.set('fullscreen', on)
    if main_window:
        try:
            main_window.toggle_fullscreen()
        except Exception:
            pass
    bus.emit('settings', settings.all())


def open_path(p):
    try:
        if IS_WIN:
            os.startfile(str(p))  # noqa
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(p)])
        else:
            subprocess.Popen(['xdg-open', str(p)])
    except Exception:
        traceback.print_exc()


QT_BRIDGE = None  # set up in main() when the Qt backend is used


def _win_clipboard(text) -> bool:
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    data = text.encode('utf-16le') + b'\x00\x00'
    for _ in range(20):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(13, h))  # CF_UNICODETEXT
    finally:
        u32.CloseClipboard()


def copy_to_clipboard(text) -> bool:
    """Puts text on the system clipboard without needing any extra programs."""
    if not text:
        return False
    if IS_WIN:
        try:
            if _win_clipboard(text):
                return True
        except Exception:
            traceback.print_exc()
    if QT_BRIDGE is not None:
        try:
            QT_BRIDGE.copy.emit(text)  # runs in the Qt GUI thread
            return True
        except Exception:
            traceback.print_exc()
    if 'gi.repository.Gtk' in sys.modules:
        try:
            from gi.repository import Gdk, GLib, Gtk

            def do():
                cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                cb.set_text(text, -1)
                cb.store()
                return False
            GLib.idle_add(do)
            return True
        except Exception:
            traceback.print_exc()
    if IS_WIN:
        cmds = [['clip']]
    elif sys.platform == 'darwin':
        cmds = [['pbcopy']]
    else:
        cmds = [['wl-copy'], ['xclip', '-selection', 'clipboard'], ['xsel', '--clipboard', '--input']]
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW)
            p.stdin.write(text.encode('utf-16le' if IS_WIN else 'utf-8'))
            p.stdin.close()
            return True
        except Exception:
            pass
    return False


def play_external(p, url_only=False):
    """Full screen in a real media player (for codecs the app's web engine lacks, e.g. H.264 in Qt)."""
    for exe, args in (('mpv', ['--fs', '--no-terminal']), ('vlc', ['--fullscreen', '--play-and-exit']),
                      ('cvlc', ['--fullscreen', '--play-and-exit']), ('celluloid', [])):
        path = shutil.which(exe)
        if path:
            try:
                subprocess.Popen([path, *args, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW)
                return True
            except Exception:
                pass
    if url_only:
        return False
    open_path(p)
    return True


def present(it):
    p = UPLOADS / it['file']
    if IS_WIN and it.get('type') == 'presentation':
        try:
            subprocess.Popen(f'start "" powerpnt /s "{p}"', shell=True, creationflags=NO_WINDOW)
            return
        except Exception:
            pass
    open_path(p)


def shutdown():
    if SHUTDOWN.is_set():
        return
    SHUTDOWN.set()
    for t in (upload_tunnel, remote_tunnel):
        t.stop()
    settings.flush()
    db.flush()

    def bye():
        time.sleep(0.4)
        os._exit(0)
    spawn(bye)
    for w in (webviewer.win, main_window):
        try:
            if w:
                w.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------------------------
# System integration: auto-start, keep awake, network check, logo, pdf.js
# --------------------------------------------------------------------------------------------
def launch_command() -> list[str]:
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    exe = sys.executable
    if IS_WIN:
        pyw = Path(exe).with_name('pythonw.exe')
        if pyw.exists():
            exe = str(pyw)
    return [exe, os.path.abspath(__file__)]


def apply_autostart():
    on = bool(settings.get('autoStart'))
    cmd = launch_command()
    try:
        if IS_WIN:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run', 0,
                                 winreg.KEY_SET_VALUE)
            if on:
                winreg.SetValueEx(key, 'FembimmoDisplay', 0, winreg.REG_SZ, ' '.join(f'"{c}"' for c in cmd))
            else:
                try:
                    winreg.DeleteValue(key, 'FembimmoDisplay')
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        elif sys.platform == 'darwin':
            plist = Path.home() / 'Library' / 'LaunchAgents' / 'be.fembimmo.display.plist'
            if on:
                plist.parent.mkdir(parents=True, exist_ok=True)
                args = ''.join(f'<string>{c}</string>' for c in cmd)
                plist.write_text('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                                 '"http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>'
                                 '<key>Label</key><string>be.fembimmo.display</string><key>ProgramArguments</key>'
                                 f'<array>{args}</array><key>RunAtLoad</key><true/></dict></plist>')
            else:
                plist.unlink(missing_ok=True)
        else:
            f = Path.home() / '.config' / 'autostart' / 'fembimmo-display.desktop'
            if on:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text('[Desktop Entry]\nType=Application\nName=Fembimmo Display\nExec='
                             + ' '.join(f'"{c}"' for c in cmd) + '\nX-GNOME-Autostart-enabled=true\n')
            else:
                f.unlink(missing_ok=True)
    except Exception:
        traceback.print_exc()


def keep_awake_loop():
    caffeinate = None
    while not SHUTDOWN.is_set():
        on = settings.get('keepAwake')
        try:
            if IS_WIN:
                import ctypes
                if on:  # ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                    ctypes.windll.kernel32.SetThreadExecutionState(0x00000001 | 0x00000002)
            elif sys.platform == 'darwin':
                if on and not caffeinate:
                    caffeinate = subprocess.Popen(['caffeinate', '-d', '-i'])
                elif not on and caffeinate:
                    caffeinate.terminate()
                    caffeinate = None
            elif on and shutil.which('xdg-screensaver'):
                subprocess.run(['xdg-screensaver', 'reset'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        SHUTDOWN.wait(30)
    if caffeinate:
        caffeinate.terminate()


online = True


def probe(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def net_loop():
    """Checks the connection every 3 seconds."""
    global online
    while not SHUTDOWN.is_set():
        ok = probe('1.1.1.1', 443) or probe('8.8.8.8', 53)
        if ok != online:
            online = ok
            bus.emit('net', online)
            if ok:
                for t in (upload_tunnel, remote_tunnel):
                    if t.status == 'error':  # never interrupt a tunnel that is still starting up
                        t.restart()
        SHUTDOWN.wait(3)


def logo_type(b: bytes) -> str:
    if b[:2] == b'\x89P':
        return 'image/png'
    if b[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if b[:4] == b'RIFF':
        return 'image/webp'
    if b[:3] == b'GIF':
        return 'image/gif'
    if re.search(rb'<svg|<\?xml', b[:300], re.I):
        return 'image/svg+xml'
    return 'application/octet-stream'


def cache_logo():
    try:
        with http_open(LOGO_URL, timeout=10) as r:
            b = r.read()
        if len(b) > 100:
            LOGO_FILE.write_bytes(b)
    except Exception:
        pass


_vendor_lock = threading.Lock()


def vendor_file(name):
    p = VENDOR / name
    if p.exists():
        return p
    with _vendor_lock:
        if not p.exists():
            try:
                download_to(PDFJS_CDN + name, p, 20 * 1024 * 1024, timeout=30)
            except Exception:
                return None
    return p


# --------------------------------------------------------------------------------------------
# HTTP servers
# --------------------------------------------------------------------------------------------
class TooLarge(Exception):
    pass


class BaseHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Fembimmo'
    sys_version = ''

    def log_message(self, *a):
        pass

    def send_bytes(self, body: bytes, ctype='application/octet-stream', code=200, headers=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', code)

    def send_html(self, text, code=200):
        self.send_bytes(text.encode('utf-8'), 'text/html; charset=utf-8', code)

    def redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def read_body(self, limit: int) -> bytes:
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            out = bytearray()
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b'\r\n', b'\n', b''):
                        pass
                    break
                if len(out) + size > limit:
                    raise TooLarge()
                out += self.rfile.read(size)
                self.rfile.readline(65537)
            return bytes(out)
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit:
            raise TooLarge()
        return self.rfile.read(n) if n else b''

    def read_json(self) -> dict:
        try:
            v = json.loads(self.read_body(256 * 1024) or b'{}')
            return v if isinstance(v, dict) else {}
        except TooLarge:
            raise
        except Exception:
            return {}

    def serve_logo(self):
        try:
            b = LOGO_FILE.read_bytes()
            self.send_bytes(b, logo_type(b), headers={'Cache-Control': 'public, max-age=3600'})
        except OSError:
            self.redirect(LOGO_URL)

    def serve_file(self, p: Path, ctype=None):
        try:
            size = p.stat().st_size
        except OSError:
            return self.send_json({'error': 'not found'}, 404)
        ctype = ctype or CONTENT_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
        start, end, code = 0, size - 1, 200
        m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
        if m and size > 0:
            a, b = m.groups()
            if a == '':
                start, end = max(0, size - int(b or 0)), size - 1
            else:
                start, end = int(a), (int(b) if b else size - 1)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-cache')
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(p, 'rb') as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)

    def page(self, tpl, cfg, code=200):
        self.send_html(tpl.replace('__CFG__', json.dumps(cfg, ensure_ascii=False).replace('<', '\\u003c')), code)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except TooLarge:
            try:
                self.send_json({'error': 'too large'}, 413)
            except Exception:
                pass
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            self.close_connection = True


def media_path(rel: str):
    p = (UPLOADS / urllib.parse.unquote(rel)).resolve()
    try:
        p.relative_to(UPLOADS.resolve())
    except ValueError:
        return None
    return p


def normalize_url(s):
    s = str(s or '').strip()
    if not s or len(s) > 2000:
        return None
    if not re.match(r'^[a-z][a-z0-9+.-]*:', s, re.I):
        s = 'https://' + s
    try:
        u = urllib.parse.urlparse(s)
        if u.scheme not in ('http', 'https') or '.' not in (u.hostname or ''):
            return None
        return u.geturl()
    except Exception:
        return None


# ---------------- Public server (upload site, tunneled) ----------------
active_uploads: dict[str, dict] = {}
active_lock = threading.Lock()


class PublicHandler(BaseHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        lang = settings.get('language')
        if path == '/logo':
            return self.serve_logo()
        m = re.fullmatch(r'/u/([A-Za-z0-9_-]+)', path)
        if m and upload_session.valid(m.group(1)):
            return self.page(UPLOAD_HTML, {'token': m.group(1), 'lang': lang, 'maxMB': settings.get('maxUploadMB')})
        return self.page(EXPIRED_HTML, {'lang': lang, 'kind': 'upload'}, 410)

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r'/api/(chunk|link)/([A-Za-z0-9_-]+)', path)
        if not m:
            return self.send_json({'error': 'not found'}, 404)
        if not upload_session.valid(m.group(2)):
            self.read_body(32 * 1024 * 1024)
            return self.send_json({'error': 'expired'}, 410)
        if m.group(1) == 'link':
            url = normalize_url(self.read_json().get('url'))
            if not url:
                return self.send_json({'error': 'invalid url'}, 400)
            on_uploaded_link(url)
            return self.send_json({'ok': True})
        return self.chunk()

    def chunk(self):
        h = self.headers
        uid = h.get('X-Upload-Id') or ''
        try:
            index, total, size = int(h.get('X-Chunk-Index')), int(h.get('X-Chunk-Total')), int(h.get('X-File-Size'))
        except (TypeError, ValueError):
            self.read_body(16 * 1024 * 1024)
            return self.send_json({'error': 'bad chunk'}, 400)
        body = self.read_body(16 * 1024 * 1024)
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', uid) or total < 1 or total > 200000 or not 0 <= index < total or size < 0:
            return self.send_json({'error': 'bad chunk'}, 400)
        if size > max_bytes():
            return self.send_json({'error': 'too large'}, 413)
        name = safe_name(urllib.parse.unquote(h.get('X-File-Name') or 'file'))
        mime = urllib.parse.unquote(h.get('X-File-Type') or '')[:120]
        with active_lock:
            u = active_uploads.get(uid)
            if not u:
                if index != 0:
                    return self.send_json({'error': 'restart', 'expected': 0}, 409)
                u = {'name': name, 'mime': mime, 'size': size, 'total': total, 'next': 0, 'received': 0,
                     'tmp': TMP / f'{uid}.part', 'touched': time.time()}
                u['tmp'].write_bytes(b'')
                active_uploads[uid] = u
            if index < u['next']:
                return self.send_json({'ok': True, 'next': u['next']})
            if index > u['next']:
                return self.send_json({'error': 'order', 'expected': u['next']}, 409)
            if u['received'] + len(body) > u['size']:
                active_uploads.pop(uid, None)
                u['tmp'].unlink(missing_ok=True)
                return self.send_json({'error': 'size mismatch'}, 400)
            with open(u['tmp'], 'ab') as f:
                f.write(body)
            u['next'] += 1
            u['received'] += len(body)
            u['touched'] = time.time()
            done = u['next'] >= u['total']
            if done:
                active_uploads.pop(uid, None)
        if not done:
            bus.emit('upload:progress', {'id': uid, 'name': u['name'], 'received': u['received'], 'total': u['size']})
            return self.send_json({'ok': True, 'next': u['next']})
        file = f'{int(time.time() * 1000)}-{secrets.token_hex(3)}-{u["name"]}'
        try:
            os.replace(u['tmp'], UPLOADS / file)
        except OSError:
            bus.emit('upload:progress', {'id': uid, 'name': u['name'], 'received': u['received'], 'total': u['size'], 'failed': True})
            return self.send_json({'error': 'save failed'}, 500)
        bus.emit('upload:progress', {'id': uid, 'name': u['name'], 'received': u['size'], 'total': u['size'], 'done': True})
        item_id = on_uploaded_file(file, u['name'], u['mime'], u['size'])
        return self.send_json({'ok': True, 'done': True, 'itemId': item_id})


def stale_upload_loop():
    while not SHUTDOWN.wait(300):
        now = time.time()
        with active_lock:
            for uid, u in list(active_uploads.items()):
                if now - u['touched'] > 1800:
                    active_uploads.pop(uid, None)
                    u['tmp'].unlink(missing_ok=True)
                    bus.emit('upload:progress', {'id': uid, 'name': u['name'], 'received': u['received'],
                                                 'total': u['size'], 'failed': True})


# ---------------- Remote control server (tunneled) ----------------
ui_state: dict = {'tab': 'upload', 'viewer': None}
REMOTE_UI_CMDS = {'tab', 'open', 'viewer.close', 'viewer.next', 'viewer.prev', 'slideshow', 'gallery.filter'}


def remote_state():
    s = settings.all()
    return {
        'ui': {'tab': ui_state.get('tab'), 'viewer': ui_state.get('viewer')}, 'np': ui_state.get('np'), 'online': online,
        'webOpen': webviewer.win is not None,
        'settings': {k: s[k] for k in ('language', 'theme', 'snow', 'fullscreen', 'welcomeName')},
        'items': [{'id': i['id'], 'name': i.get('name'), 'type': i.get('type'), 'status': i.get('status'),
                   'thumb': i.get('type') == 'image' or (i.get('type') == 'link' and bool(i.get('thumb')))}
                  for i in sorted(items(), key=lambda x: -x.get('createdAt', 0))[:200]],
        'upload': {'shortUrl': upload_session.short or upload_session.url},
    }


def remote_cmd(cmd, arg):
    if cmd in ('music.toggle', 'music.next', 'music.prev', 'music.volume', 'music.preset', 'music.search',
               'music.shuffle', 'music.seek'):
        bus.emit('remote', {'cmd': cmd, 'arg': arg})
        return True
    if cmd == 'link.new':
        upload_session.refresh(True)
        return True
    if cmd == 'set' and isinstance(arg, dict) and arg.get('key') in ('language', 'theme', 'snow'):
        apply_setting(arg['key'], arg.get('value'))
        return True
    if cmd == 'app.fullscreen':
        toggle_fullscreen()
        return True
    if cmd == 'web.close':
        webviewer.close()
        return True
    if cmd == 'item.delete':
        remove_item(str(arg))
        return True
    if cmd in REMOTE_UI_CMDS:
        if cmd == 'open' and webviewer.win:
            webviewer.close()
        bus.emit('remote', {'cmd': cmd, 'arg': arg})
        return True
    return False


class RemoteHandler(BaseHandler):
    def _token(self, path):
        m = re.match(r'/(r|rapi)/([A-Za-z0-9_-]+)(/.*)?$', path)
        if m and remote_session.valid(m.group(2)):
            return m.group(1), m.group(2), m.group(3) or ''
        return None, None, None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        lang = settings.get('language')
        if path == '/logo':
            return self.serve_logo()
        kind, token, rest = self._token(path)
        if kind == 'r' and not rest:
            return self.page(REMOTE_HTML, {'token': token, 'lang': lang})
        if kind == 'rapi' and rest == '/state':
            return self.send_json(remote_state())
        if kind == 'rapi' and rest.startswith('/thumb/'):
            it = get_item(rest[7:])
            if it and it.get('type') == 'image' and it.get('file'):
                return self.serve_file(UPLOADS / it['file'])
            if it and it.get('type') == 'link' and it.get('thumb'):
                return self.redirect(it['thumb'])
            return self.send_json({'error': 'not found'}, 404)
        if kind == 'rapi':
            return self.send_json({'error': 'not found'}, 404)
        return self.page(EXPIRED_HTML, {'lang': lang, 'kind': 'remote'}, 410)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        kind, token, rest = self._token(path)
        body = self.read_json()
        if kind != 'rapi':
            return self.send_json({'error': 'expired'}, 410)
        if rest == '/cmd':
            ok = remote_cmd(str(body.get('cmd') or ''), body.get('arg'))
            return self.send_json({'ok': bool(ok) or ok is None})
        return self.send_json({'error': 'not found'}, 404)


# ---------------- Private server (app UI, 127.0.0.1 only) ----------------
def apply_setting(key, value):
    if key in ('pexelsKey', 'pixabayKey', 'unsplashKey'):
        value = re.sub(r'\s+', '', str(value or ''))
    if key not in DEFAULTS:
        return settings.all()
    old = settings.get(key)
    value = sanitize_setting(key, value)
    settings.set(key, value)
    if key == 'autoStart':
        apply_autostart()
    elif key == 'fullscreen' and value != old and main_window:
        try:
            main_window.toggle_fullscreen()
        except Exception:
            pass
    elif key == 'linkRefreshMin':
        upload_session.refresh(True)
    elif key == 'remoteRefreshMin':
        remote_session.refresh(True)
    s = settings.all()
    bus.emit('settings', s)
    return s


def storage_info():
    used = 0
    for root, _dirs, files in os.walk(UPLOADS):
        for f in files:
            try:
                used += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    try:
        free = shutil.disk_usage(UPLOADS).free
    except Exception:
        free = None
    return {'used': used, 'free': free, 'count': len(items()), 'path': str(UPLOADS), 'office': bool(find_soffice())}


PORTS = {}


class PrivateHandler(BaseHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            return self.send_html(APP_HTML)
        if path == '/logo':
            return self.serve_logo()
        if path.startswith('/vendor/') and path[8:] in ('pdf.min.js', 'pdf.worker.min.js'):
            p = vendor_file(path[8:])
            return self.serve_file(p) if p else self.send_bytes(b'/* pdf.js offline */', 'application/javascript', 503)
        if path.startswith('/media/'):
            p = media_path(path[7:])
            return self.serve_file(p) if p else self.send_json({'error': 'forbidden'}, 403)
        if path == '/api/state':
            return self.send_json({
                'settings': settings.all(), 'items': items(), 'session': upload_session.public(),
                'tunnel': upload_tunnel.state(), 'remote': remote_session.public(), 'rtunnel': remote_tunnel.state(),
                'online': online, 'version': VERSION, 'platform': sys.platform, 'logoUrl': LOGO_URL,
                'native': webview is not None and main_window is not None, 'yt': yt.status(),
                'ffmpeg': bool(ffmpeg_path()), 'libKeys': library_keys(), 'deps': deps.public(),
            })
        if path == '/api/storage':
            return self.send_json(storage_info())
        if path == '/api/events':
            return self.events()
        if path.startswith('/api/yt/stream/'):
            return self.yt_stream(path[15:])
        if path.startswith('/api/live/'):
            it = get_item(path[10:].split('.')[0])
            if not it or not it.get('file'):
                return self.send_json({'error': 'not found'}, 404)
            return self.live_stream(UPLOADS / it['file'])
        if path == '/api/live-url':
            u = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('u', [''])[0]
            if not re.match(r'^https://', u):
                return self.send_json({'error': 'bad url'}, 400)
            return self.live_stream(u)
        if path == '/api/yt/status':
            return self.send_json(yt.status())
        if path == '/api/deps':
            return self.send_json(deps.public())
        return self.send_json({'error': 'not found'}, 404)

    do_HEAD = do_GET

    def events(self):
        q = bus.subscribe()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            while not SHUTDOWN.is_set():
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b': ping\n\n'
                self.wfile.write(msg)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            bus.unsubscribe(q)

    def live_stream(self, source):
        """Real-time WebM stream so videos play inside the app even without H.264 support."""
        try:
            ff = ffmpeg_path() or deps.ensure('ffmpeg')
        except Exception as e:
            return self.send_json({'error': f'ffmpeg: {e}'}, 503)
        try:
            p = subprocess.Popen(live_video_cmd(source, ff), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except Exception as e:
            return self.send_json({'error': str(e)}, 500)
        first = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        if not first:
            p.kill()
            return self.send_json({'error': 'cannot decode video'}, 415)
        self.send_response(200)
        self.send_header('Content-Type', 'video/webm')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(first)
            while True:
                chunk = p.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                p.kill()
            except Exception:
                pass

    def yt_stream(self, vid):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            return self.send_json({'error': 'bad id'}, 400)
        up = None
        for attempt in range(2):
            try:
                info = yt.resolve(vid, fresh=attempt > 0)
            except Exception as e:
                return self.send_json({'error': str(e)[:300]}, 502)
            headers = {k: v for k, v in (info.get('headers') or {}).items() if k.lower() not in ('accept-encoding',)}
            if self.headers.get('Range'):
                headers['Range'] = self.headers['Range']
            try:
                up = urllib.request.urlopen(urllib.request.Request(info['url'], headers=headers), timeout=25, context=SSL_CTX)
                break
            except urllib.error.HTTPError as e:
                if e.code == 416:
                    return self.send_bytes(b'', 'text/plain', 416)
                if attempt:
                    return self.send_json({'error': f'upstream {e.code}'}, 502)
            except Exception as e:
                if attempt:
                    return self.send_json({'error': str(e)}, 502)
        with up:
            self.send_response(up.status)
            ctype = up.headers.get('Content-Type') or info.get('mime') or 'audio/webm'
            if ctype.startswith('video/'):
                ctype = ctype.replace('video/', 'audio/', 1)
            self.send_header('Content-Type', ctype)
            for h in ('Content-Length', 'Content-Range'):
                if up.headers.get(h):
                    self.send_header(h, up.headers[h])
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            if not up.headers.get('Content-Length'):
                self.close_connection = True
            self.end_headers()
            try:
                while True:
                    chunk = up.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                self.close_connection = True

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{PORTS.get("private")}', f'http://localhost:{PORTS.get("private")}'):
            return self.send_json({'error': 'forbidden'}, 403)
        path = urllib.parse.urlparse(self.path).path
        b = self.read_json()
        try:
            r = self.route(path, b)
        except Exception as e:
            return self.send_json({'error': str(e) or e.__class__.__name__}, 500)
        if r is NotImplemented:
            return self.send_json({'error': 'not found'}, 404)
        return self.send_json(r if isinstance(r, dict) else {'ok': True, 'result': r})

    def route(self, path, b):
        if path == '/api/settings':
            return {'settings': apply_setting(b.get('key'), b.get('value'))}
        if path == '/api/link/new':
            return upload_session.refresh(True)
        if path == '/api/remote/new':
            return remote_session.refresh(True)
        if path == '/api/tunnel/restart':
            upload_tunnel.restart()
            remote_tunnel.restart()
            return {'ok': True}
        if path == '/api/item/delete':
            remove_item(str(b.get('id')))
            return {'ok': True}
        if path == '/api/items/delete-all':
            for it in items():
                delete_files_of(it)
            save_items([])
            return {'ok': True}
        if path in ('/api/item/open-external', '/api/item/present'):
            it = get_item(str(b.get('id')))
            if not it:
                return {'ok': False}
            if path.endswith('present'):
                present(it)
            elif it.get('type') == 'link':
                import webbrowser
                webbrowser.open(it['url'])
            else:
                open_path(UPLOADS / it['file'])
            return {'ok': True}
        if path == '/api/library/search':
            return library_search(**{k: b.get(k) for k in ('query', 'kind', 'source', 'page')})
        if path == '/api/library/import':
            return {'item': import_from_library(b)}
        if path == '/api/app/quit':
            spawn(shutdown)
            return {'ok': True}
        if path == '/api/app/fullscreen':
            toggle_fullscreen()
            return {'ok': True}
        if path == '/api/app/open-folder':
            open_path(UPLOADS)
            return {'ok': True}
        if path == '/api/app/open-url':
            u = str(b.get('url') or '')
            if re.match(r'^https?://', u):
                import webbrowser
                webbrowser.open(u)
            return {'ok': True}
        if path == '/api/yt/search':
            return {'results': yt.search(str(b.get('q') or ''), int(b.get('n') or 25))}
        if path == '/api/yt/prefetch':
            vid = str(b.get('id') or '')
            if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
                spawn(yt.prefetch, vid)
            return {'ok': True}
        if path == '/api/yt/install':
            spawn(yt.install, bool(b.get('upgrade')))
            return {'ok': True}
        if path == '/api/deps/install':
            name = str(b.get('name') or '')
            if name == 'yt-dlp':
                spawn(yt.install, True)
            elif name in deps.NAMES:
                deps.ensure_async(name, force=True)
            elif name == 'cloudflared':
                upload_tunnel.restart()
                remote_tunnel.restart()
            return {'ok': True}
        if path == '/api/clipboard':
            return {'ok': copy_to_clipboard(str(b.get('text') or '')[:4000])}
        if path == '/api/library/resolve':
            return {'url': resolve_library_url(b)}
        if path == '/api/item/play-external':
            it = get_item(str(b.get('id')))
            if it and it.get('file'):
                play_external(UPLOADS / it['file'])
                spawn(convert_video, it['id'])
            return {'ok': True}
        if path == '/api/play-url':
            u = str(b.get('url') or '')
            return {'ok': bool(re.match(r'^https://', u)) and play_external(u, url_only=True)}
        if path == '/api/web/open':
            url = normalize_url(b.get('url'))
            return {'ok': bool(url) and webviewer.open(url)}
        if path == '/api/web/close':
            webviewer.close()
            return {'ok': True}
        if path == '/api/ui-state':
            for k in ('tab', 'viewer', 'np'):
                if k in b:
                    ui_state[k] = b[k]
            if 'h264' in b:
                was = ENGINE['h264']
                ENGINE['h264'] = bool(b['h264'])
                if was and not ENGINE['h264']:
                    spawn(prepare_video_support)
            return {'ok': True}
        return NotImplemented


def start_server(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    srv.daemon_threads = True
    spawn(srv.serve_forever)
    return srv.server_address[1]


def start_services():
    cleanup_items()
    for f in TMP.iterdir():
        try:
            f.unlink()
        except OSError:
            pass
    PORTS['private'] = start_server(PrivateHandler)
    PORTS['public'] = start_server(PublicHandler)
    PORTS['remote'] = start_server(RemoteHandler)
    upload_tunnel.start(PORTS['public'])
    remote_tunnel.start(PORTS['remote'])
    apply_autostart()
    spawn(cache_logo)
    spawn(vendor_file, 'pdf.min.js')
    spawn(vendor_file, 'pdf.worker.min.js')
    spawn(net_loop)
    spawn(keep_awake_loop)
    spawn(stale_upload_loop)
    spawn(yt.ensure_ready)


BROWSERS_CHROMIUM = ['chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'microsoft-edge',
                     'microsoft-edge-stable', 'brave-browser', 'vivaldi']


def browser_fallback(url) -> bool:
    """No native window available: open the app full screen in an installed browser."""
    profile = DATA / 'browser-profile'
    profile.mkdir(exist_ok=True)
    cmd = None
    for name in BROWSERS_CHROMIUM:
        exe = shutil.which(name)
        if exe:
            cmd = [exe, f'--app={url}', '--kiosk', '--start-fullscreen', f'--user-data-dir={profile}',
                   '--autoplay-policy=no-user-gesture-required', '--no-first-run', '--noerrdialogs',
                   '--disable-session-crashed-bubble', '--disable-infobars', '--password-store=basic']
            break
    if not cmd and shutil.which('firefox'):
        cmd = [shutil.which('firefox'), '--kiosk', '--new-window', url]
    if not cmd:
        return False
    print(f'No pywebview GUI toolkit found; opening {APP_NAME} in {os.path.basename(cmd[0])} (kiosk mode).')
    print('Tip: pip install "pywebview[qt]" for the full app window (needed for the music window).')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    try:
        while proc.poll() is None and not SHUTDOWN.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    shutdown()
    time.sleep(1)
    return True


def serve_forever_message(url):
    print(f'{APP_NAME} running at {url}  (Ctrl+C to stop)')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


GUI_HELP = '''
pywebview could not open a window: no GUI toolkit is installed.
Fix (pick one):
  pip install "pywebview[qt]"                                  (works in any Python / venv)
  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1   (system Python only)
'''


def diagnose():
    print('Python      :', sys.executable, sys.version.split()[0])
    print('Data folder :', DATA)
    print('Display     :', os.environ.get('DISPLAY'), os.environ.get('WAYLAND_DISPLAY'))
    print('pywebview   :', 'OK' if webview else 'missing')
    if webview is not None:
        print('webview file:', getattr(webview, '__file__', 'unknown'))
    print('QT_API      :', os.environ.get('QT_API'))
    try:
        from qtpy import QtWebEngineWidgets  # noqa: F401
        print('Qt WebEngine: OK (QtPy + WebEngine imports succeeded)')
    except Exception as e:
        print('Qt WebEngine:', repr(e))
    print('segno       :', 'OK' if segno else 'missing (built-in QR encoder is used)')
    st = yt.status()
    print('Frozen exe  :', FROZEN)
    print('yt-dlp      :', st['version'] or 'not yet downloaded', f'({st["mode"]})', '| JS runtime:', st['jsRuntime'] or 'none yet (auto-downloads Deno)')
    print('cloudflared :', shutil.which('cloudflared') or (BIN / ('cloudflared.exe' if IS_WIN else 'cloudflared')))
    print('Internet    :', 'OK' if (probe('1.1.1.1', 443) or probe('8.8.8.8', 53)) else 'NO')
    print('LibreOffice :', find_soffice() or 'not found')
    print('ffmpeg      :', ffmpeg_path() or 'not yet downloaded (auto-downloads when the window cannot play MP4)')
    print('Stock keys  :', ', '.join(f'{n}={"set" if v else "missing"}' for n, v in library_keys().items()))
    print('Media player:', next((shutil.which(x) for x in ('mpv', 'vlc', 'celluloid') if shutil.which(x)), None) or 'none')
    cfg = Path.home() / '.cloudflared' / 'config.yml'
    if cfg.exists():
        print('WARNING     :', cfg, 'exists: quick tunnels fail with it. Rename it.')


def qt_webengine_available() -> bool:
    """Return True only when pywebview's Qt backend dependencies can actually be imported.

    Do not test this with PyQt6 alone: pywebview's Qt backend also needs QtPy and Qt WebEngine.
    """
    try:
        from qtpy import QtWebEngineWidgets  # noqa: F401
        return True
    except Exception as exc:
        print(f'[gui] Qt WebEngine backend unavailable: {exc!r}')
        return False


def setup_qt_bridge():
    """On Linux with Qt: create the QApplication ourselves (pywebview reuses it) plus a small bridge
    object living in the GUI thread, so other threads can safely use the clipboard.

    Windows also supports pywebview's Qt backend, but the app should let pywebview create its own
    QApplication there. The bridge is only needed for the Linux clipboard fallback.
    """
    global QT_BRIDGE
    if IS_WIN or sys.platform == 'darwin' or os.environ.get('PYWEBVIEW_GUI', '').lower() == 'gtk':
        return False
    try:
        import gi  # GTK available -> pywebview prefers GTK; the clipboard then goes through GLib
        gi.require_version('Gtk', '3.0')
        import importlib
        importlib.import_module('gi.repository.Gtk')
        return False
    except Exception:
        pass
    try:
        from qtpy import QtWebEngineWidgets  # noqa: F401  (must be imported before QApplication)
        from qtpy.QtCore import QCoreApplication, QObject, Qt, Signal, Slot
        from qtpy.QtWidgets import QApplication
    except Exception:
        return False
    for attr in ('AA_ShareOpenGLContexts',):
        try:
            QCoreApplication.setAttribute(getattr(getattr(Qt, 'ApplicationAttribute', Qt), attr))
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)

    class Bridge(QObject):
        copy = Signal(str)

        @Slot(str)
        def do_copy(self, text):
            app.clipboard().setText(text)

    QT_BRIDGE = Bridge()
    QT_BRIDGE.copy.connect(QT_BRIDGE.do_copy)
    return True


def main():
    global main_window
    args = sys.argv[1:]
    no_gui = '--no-gui' in args
    if '--diagnose' in args:
        return diagnose()
    start_services()
    url = f'http://127.0.0.1:{PORTS["private"]}/'
    if no_gui:
        return serve_forever_message(url)
    if webview is None:
        print('pywebview is not installed. Run:  pip install pywebview segno')
        if not browser_fallback(url):
            serve_forever_message(url)
        return

    main_window = webview.create_window(
        APP_NAME, url, fullscreen=bool(settings.get('fullscreen')), js_api=js_api, width=1600, height=900,
        min_size=(420, 520), background_color='#0e1513' if settings.get('theme') == 'dark' else '#f2f3f0',
        text_select=True)
    main_window.events.closed += shutdown

    def after_start():
        pass

    (DATA / 'webview').mkdir(exist_ok=True)
    qt_bridge_ready = setup_qt_bridge()
    # Previously Qt was never selected on Windows because setup_qt_bridge() deliberately returns
    # False there. If the user installed pywebview[qt], explicitly select that backend instead of
    # letting pywebview try its default WinForms/WebView2 backend.
    use_qt = qt_bridge_ready or (IS_WIN and qt_webengine_available())
    gui = 'qt' if use_qt else None
    if IS_WIN and use_qt:
        print('[gui] Using pywebview Qt WebEngine backend on Windows.')
    try:
        webview.start(after_start, gui=gui, private_mode=False, storage_path=str(DATA / 'webview'), debug='--debug' in args)
    except Exception as e:  # e.g. "You must have either QT or GTK with Python extensions installed"
        main_window = None  # the UI then knows native extra windows (music, web) are unavailable
        print(f'{e}\n{GUI_HELP}')
        if not browser_fallback(url):
            print('No supported browser found either (chromium, chrome, edge, firefox).')
            serve_forever_message(url)
        return
    shutdown()


# ============================================================================================
# Embedded web assets (HTML / CSS / JS)
# ============================================================================================
APP_HTML = r'''<!doctype html>
<html lang="nl" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fembimmo Display</title>
<style>
/* Fembimmo Display — styles
   Palette: porcelain #f2f3f0 · ink #15201e · petrol accent #12424a (changeable) · brass #b0894b */
:root {
  --bg: #f2f3f0;
  --surface: #ffffff;
  --surface-2: #e9ebe7;
  --ink: #15201e;
  --ink-2: #4d5b57;
  --ink-3: #87938f;
  --line: #dadfdb;
  --accent: #12424a;
  --accent-ink: #ffffff;
  --accent-dark: #0d3238;
  --accent-light: #6fa7ad;
  --brass: #b0894b;
  --ok: #1d7a4c;
  --bad: #b42318;
  --shadow: 0 1px 2px rgba(15, 30, 28, .05), 0 12px 32px -12px rgba(15, 30, 28, .14);
  --r-lg: 28px;
  --r-md: 18px;
  --r-sm: 10px;
  --header-h: clamp(64px, 8vh, 112px);
  --pad: clamp(16px, 2.4vw, 56px);
  font-size: clamp(14px, .42vw + 8px, 26px);
  color-scheme: light;
}
:root[data-theme="dark"] {
  --bg: #0e1513;
  --surface: #16201e;
  --surface-2: #1d2a27;
  --ink: #eef2f0;
  --ink-2: #a9b6b2;
  --ink-3: #6c7a76;
  --line: #273532;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 14px 36px -14px rgba(0, 0, 0, .6);
  color-scheme: dark;
}

* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Roboto, sans-serif;
  background: var(--bg);
  color: var(--ink);
  display: grid;
  grid-template-rows: var(--header-h) 1fr;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
  user-select: none;
}
input, select, textarea { user-select: text; }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; }
h1, h2, h3 { font-family: "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif; margin: 0; }
.hidden { display: none !important; }
:focus-visible { outline: 3px solid var(--accent-light); outline-offset: 2px; }
.ic { display: inline-flex; width: 1.25em; height: 1.25em; flex: none; }
.ic svg { width: 100%; height: 100%; }

/* ---------------- Header ---------------- */
.topbar {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 1rem;
  padding: 0 var(--pad);
  position: relative;
  z-index: 5;
}
.brand { display: flex; align-items: center; min-width: 0; }
.brand img { height: calc(var(--header-h) * .48); max-width: 100%; width: auto; object-fit: contain; display: block; }
:root[data-theme="dark"] .brand img { background: #fff; padding: .35rem .6rem; border-radius: var(--r-sm); }
.brand-text { font-weight: 700; font-size: 1.5rem; letter-spacing: -.02em; }

.tabs {
  display: flex;
  gap: .25rem;
  padding: .3rem;
  background: var(--surface-2);
  border-radius: 999px;
}
.tab {
  display: flex; align-items: center; gap: .55rem;
  border: 0; background: transparent;
  padding: .65rem 1.15rem;
  border-radius: 999px;
  color: var(--ink-2);
  font-weight: 600;
  white-space: nowrap;
  transition: background .18s, color .18s;
}
.tab:hover { color: var(--ink); }
.tab.active { background: var(--surface); color: var(--ink); box-shadow: 0 1px 3px rgba(0, 0, 0, .08); }
.tab.active .ic { color: var(--accent); }
:root[data-theme="dark"] .tab.active .ic { color: var(--accent-light); }
.count {
  font-size: .72rem; min-width: 1.5em; padding: .1em .45em; border-radius: 999px;
  background: var(--accent); color: var(--accent-ink); font-weight: 700; text-align: center;
}
.count:empty { display: none; }

.status { display: flex; align-items: center; justify-content: flex-end; gap: 1rem; min-width: 0; }
.clock { display: flex; flex-direction: column; align-items: flex-end; line-height: 1.15; }
.clock b { font-size: 1.45rem; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.clock span { color: var(--ink-3); font-size: .8rem; text-transform: capitalize; }
.offline {
  display: flex; align-items: center; gap: .45rem;
  background: rgba(180, 35, 24, .12);
  color: var(--bad);
  padding: .5rem .85rem; border-radius: 999px; font-weight: 600; font-size: .9rem;
  animation: pulse 2s ease-in-out infinite;
}
.offline .ic { width: 1.4em; height: 1.4em; }
@keyframes pulse { 50% { opacity: .55; } }

.mini {
  display: flex; align-items: center; gap: .4rem;
  background: var(--surface); border-radius: 999px; padding: .3rem .4rem .3rem .3rem;
  box-shadow: var(--shadow); max-width: 22rem; min-width: 0; cursor: pointer;
}
.mini img { width: 2.2rem; height: 2.2rem; border-radius: 50%; object-fit: cover; flex: none; }
.mini-text { display: flex; flex-direction: column; min-width: 0; line-height: 1.2; padding: 0 .3rem; }
.mini-text b, .mini-text span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: .82rem; }
.mini-text span { color: var(--ink-3); }

/* ---------------- Generic controls ---------------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: .5rem;
  border: 1.5px solid var(--line); background: var(--surface); color: var(--ink);
  padding: .7rem 1.15rem; border-radius: var(--r-sm); font-weight: 600; white-space: nowrap;
  transition: border-color .15s, background .15s, transform .1s;
}
.btn:hover { border-color: var(--ink-3); }
.btn:active { transform: scale(.98); }
.btn:disabled { opacity: .55; pointer-events: none; }
.btn.accent { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.accent:hover { filter: brightness(1.1); }
.btn.danger { background: var(--bad); border-color: var(--bad); color: #fff; }
.btn.danger-ghost { color: var(--bad); }
.icon-btn {
  display: inline-grid; place-items: center; border: 0; background: transparent; color: inherit;
  width: 2.4rem; height: 2.4rem; border-radius: 50%; flex: none; padding: 0;
  transition: background .15s;
}
.icon-btn:hover { background: var(--surface-2); }
.icon-btn.play { background: var(--accent); color: var(--accent-ink); }
.icon-btn.lg { width: 3rem; height: 3rem; }
.icon-btn.xl { width: 4rem; height: 4rem; }
.icon-btn.xl .ic { width: 1.6em; height: 1.6em; }

.search {
  display: flex; align-items: center; gap: .6rem;
  background: var(--surface); border: 1.5px solid var(--line); border-radius: var(--r-sm);
  padding: 0 .9rem; min-width: 0; flex: 1 1 16rem; color: var(--ink-3);
}
.search:focus-within { border-color: var(--accent); }
.search input { border: 0; outline: 0; background: transparent; padding: .75rem 0; width: 100%; min-width: 0; color: var(--ink); }
.search.big input { padding: .95rem 0; font-size: 1.1rem; }

.seg { display: flex; background: var(--surface-2); padding: .25rem; border-radius: var(--r-sm); gap: .2rem; flex-wrap: wrap; }
.seg button {
  border: 0; background: transparent; padding: .55rem .9rem; border-radius: 7px; font-weight: 600; color: var(--ink-2);
}
.seg button.on { background: var(--surface); color: var(--ink); box-shadow: 0 1px 3px rgba(0, 0, 0, .08); }

select {
  background: var(--surface); border: 1.5px solid var(--line); border-radius: var(--r-sm);
  padding: .65rem .9rem; font-weight: 600; max-width: 100%;
}

.chips, .types { display: flex; flex-wrap: wrap; gap: .5rem; }
.chip {
  display: inline-flex; align-items: center; gap: .45rem;
  background: var(--surface); border: 1.5px solid var(--line); border-radius: 999px;
  padding: .45rem .85rem; font-weight: 600; font-size: .9rem; color: var(--ink-2);
}
button.chip:hover { border-color: var(--accent); color: var(--ink); }

.spinner {
  width: 2.2rem; height: 2.2rem; border-radius: 50%;
  border: 3px solid rgba(127, 127, 127, .25); border-top-color: currentColor;
  animation: spin .8s linear infinite;
}
.spinner.sm { width: 1.2rem; height: 1.2rem; border-width: 2px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ---------------- Panels ---------------- */
.panels { position: relative; overflow: hidden; }
.panel {
  position: absolute; inset: 0;
  overflow: auto;
  padding: .5rem var(--pad) var(--pad);
  visibility: hidden; opacity: 0; pointer-events: none;
  transition: opacity .2s;
}
.panel.active { visibility: visible; opacity: 1; pointer-events: auto; }

/* ---------------- Upload ---------------- */
.upload-layout {
  min-height: 100%;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.05fr);
  gap: clamp(1.5rem, 4vw, 6rem);
  align-items: center;
}
.qr-stage {
  background: var(--accent);
  color: var(--accent-ink);
  border-radius: var(--r-lg);
  padding: clamp(1.5rem, 3.5vw, 4.5rem);
  display: flex; flex-direction: column; align-items: center; gap: 1.4rem;
  height: 100%;
  justify-content: center;
  position: relative;
  overflow: hidden;
}
.qr-stage::before {
  content: ""; position: absolute; inset: auto -20% -45% -20%; height: 70%;
  background: radial-gradient(closest-side, rgba(255, 255, 255, .09), transparent);
  pointer-events: none;
}
.qr-frame {
  background: #fff; border-radius: var(--r-md);
  width: min(100%, 62vh, 40vw); aspect-ratio: 1;
  padding: 4%;
  display: grid; place-items: center;
  box-shadow: 0 30px 60px -20px rgba(0, 0, 0, .45);
  position: relative;
}
.qr-frame img { width: 100%; height: 100%; image-rendering: pixelated; display: block; }
.qr-wait { display: flex; flex-direction: column; align-items: center; gap: 1rem; color: #4d5b57; text-align: center; padding: 1rem; }
.qr-wait p { margin: 0; max-width: 30ch; line-height: 1.45; white-space: pre-line; font-size: .95rem; word-break: break-word; }
.short-link {
  font-size: clamp(1.3rem, 2.2vw, 3rem); font-weight: 650; letter-spacing: -.01em;
  text-align: center; word-break: break-all; max-width: 100%;
}
.short-link { cursor: pointer; border-radius: var(--r-sm); padding: .2rem .6rem; transition: background .15s; position: relative; }
.short-link:hover, .short-link:focus-visible { background: rgba(255, 255, 255, .12); }
.short-link::after {
  content: ""; display: inline-block; width: .8em; height: .8em; margin-left: .45em; vertical-align: .05em; opacity: .7;
  background: currentColor;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='9' y='9' width='13' height='13' rx='2'/%3E%3Cpath d='M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'/%3E%3C/svg%3E") center / contain no-repeat;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='9' y='9' width='13' height='13' rx='2'/%3E%3Cpath d='M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'/%3E%3C/svg%3E") center / contain no-repeat;
}
.short-link:empty::after, .short-link.blank::after { display: none; }
.short-link.copied { background: rgba(255, 255, 255, .22); }
.short-link.copied::after { -webkit-mask: none; mask: none; background: none; content: "✓"; width: auto; height: auto; opacity: 1; }
.short-link.long { font-size: clamp(.95rem, 1.2vw, 1.6rem); font-weight: 600; opacity: .9; }
.expiry { width: min(100%, 26rem); display: flex; flex-direction: column; align-items: center; gap: .55rem; }
.expiry-bar { width: 100%; height: 4px; background: rgba(255, 255, 255, .2); border-radius: 4px; overflow: hidden; }
.expiry-bar i { display: block; height: 100%; background: var(--brass); transition: width 1s linear; }
.expiry span { font-size: .88rem; opacity: .8; font-variant-numeric: tabular-nums; }

.welcome {
  margin: 0 0 1.2rem; font-size: clamp(1.3rem, 2vw, 2.6rem); font-weight: 650; letter-spacing: -.02em;
  color: var(--brass); font-family: "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif;
}
.remote-stage { background: var(--accent-dark); }
.remote-warn { color: var(--ink-3); font-size: .9rem; max-width: 46ch; margin-top: 1.2rem; line-height: 1.5; }
.text-input { background: var(--bg); border: 1.5px solid var(--line); border-radius: var(--r-sm); padding: .6rem .8rem; width: 14rem; max-width: 50%; }
.upload-copy h1 { font-size: clamp(2.2rem, 4.2vw, 5.6rem); line-height: 1.02; letter-spacing: -.035em; font-weight: 700; max-width: 12ch; }
.lead { font-size: 1.15rem; line-height: 1.55; color: var(--ink-2); max-width: 40ch; margin: 1.2rem 0 2rem; }
.steps { list-style: none; padding: 0; margin: 0 0 2rem; display: grid; gap: 1.1rem; }
.steps li { display: flex; gap: 1rem; align-items: flex-start; }
.step-n {
  flex: none; width: 2.3rem; height: 2.3rem; border-radius: 50%;
  display: grid; place-items: center; font-weight: 700;
  border: 2px solid var(--accent); color: var(--accent);
}
:root[data-theme="dark"] .step-n { color: var(--accent-light); border-color: currentColor; }
.steps b { font-size: 1.1rem; }
.steps p { margin: .2rem 0 0; color: var(--ink-2); }
.upload-actions { margin-top: 1.6rem; }
.incoming { margin-top: 1.6rem; background: var(--surface); border-radius: var(--r-md); padding: 1rem 1.2rem; box-shadow: var(--shadow); max-width: 34rem; }
.incoming h3 { font-size: 1rem; margin-bottom: .6rem; }
.incoming ul { list-style: none; margin: 0; padding: 0; display: grid; gap: .7rem; }
.in-top { display: flex; justify-content: space-between; gap: 1rem; font-size: .92rem; }
.in-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.in-pct { color: var(--ink-3); font-variant-numeric: tabular-nums; }
.in-bar { height: 5px; border-radius: 5px; background: var(--surface-2); margin-top: .35rem; overflow: hidden; }
.in-bar i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .25s; }
.incoming li.done .in-bar i { background: var(--ok); }
.incoming li.done .in-pct { color: var(--ok); }
.incoming li.failed .in-bar i { background: var(--bad); }

/* ---------------- Grid / cards ---------------- */
.toolbar, .lib-search { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin-bottom: 1.25rem; position: sticky; top: 0; z-index: 2; padding: .5rem 0; background: var(--bg); }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(clamp(150px, 15vw, 380px), 1fr));
  gap: clamp(.75rem, 1.2vw, 1.6rem);
}
.card {
  position: relative; background: var(--surface); border-radius: var(--r-md); overflow: hidden;
  box-shadow: var(--shadow); cursor: pointer; transition: transform .15s;
  outline-offset: 3px;
}
.card:hover { transform: translateY(-2px); }
.card.fresh { box-shadow: 0 0 0 3px var(--brass), var(--shadow); }
.thumb {
  position: relative; aspect-ratio: 4 / 3; background: var(--surface-2);
  display: grid; place-items: center; overflow: hidden; color: var(--ink-3);
}
.thumb.g-audio { background: linear-gradient(135deg, var(--accent), var(--accent-dark)); color: rgba(255, 255, 255, .8); }
.thumb-icon { width: 2.6rem; height: 2.6rem; }
.thumb img, .thumb video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .3s; background: var(--surface-2); }
.thumb img.loaded, .thumb video.loaded { opacity: 1; }
.thumb img.fav { object-fit: contain; padding: 22%; background: var(--surface-2); }
.thumb img.doc-page { object-fit: contain; object-position: center; padding: 6%; background: var(--surface-2); filter: drop-shadow(0 4px 10px rgba(0, 0, 0, .15)); }
.play-badge {
  position: absolute; width: 2.8rem; height: 2.8rem; border-radius: 50%; display: grid; place-items: center;
  background: rgba(0, 0, 0, .5); color: #fff; backdrop-filter: blur(4px); z-index: 1; pointer-events: none;
}
.thumb-busy { position: absolute; inset: 0; display: grid; place-items: center; background: rgba(0, 0, 0, .35); color: #fff; z-index: 2; }
.card-body { padding: .75rem .9rem .85rem; }
.card-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-meta { display: flex; flex-wrap: wrap; gap: .25rem .7rem; color: var(--ink-3); font-size: .8rem; margin-top: .25rem; }
.card-meta span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
.type-tag { color: var(--ink-2); font-weight: 600; }
.card-del, .card-add {
  position: absolute; top: .5rem; right: .5rem; z-index: 3;
  background: rgba(0, 0, 0, .55); color: #fff; backdrop-filter: blur(4px);
  opacity: 0; transition: opacity .15s;
}
.card:hover .card-del, .card:focus-within .card-del, .card-add { opacity: 1; }
.card-del:hover { background: var(--bad); }
.card-add { background: var(--accent); }
.card-add.done { background: var(--ok); }
@media (hover: none) { .card-del { opacity: 1; } }

.empty { text-align: center; padding: 12vh 1rem; display: flex; flex-direction: column; align-items: center; gap: .8rem; }
.empty h2 { font-size: 1.8rem; letter-spacing: -.02em; }
.empty p { color: var(--ink-2); margin: 0 0 .6rem; max-width: 40ch; }

/* ---------------- Library ---------------- */
.lib-search .search { flex: 1 1 22rem; }
#libSuggest { margin-bottom: 1rem; }
.lib-note { color: var(--ink-2); margin: 0 0 1rem; min-height: 1.4em; }
.lib-foot { display: flex; justify-content: center; padding: 2rem 0; }
.hover-preview { z-index: 1; }

/* ---------------- Music ---------------- */
.music-layout { display: grid; grid-template-columns: minmax(16rem, 24rem) 1fr; gap: clamp(1rem, 2vw, 2.5rem); align-items: start; }
.music-side { display: flex; flex-direction: column; gap: 1rem; min-height: 0; overflow: auto; }
.now { position: sticky; top: .5rem; background: var(--surface); border-radius: var(--r-md); padding: 1.2rem; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: .7rem; }
.np-art { position: relative; width: 100%; padding-top: 56.25%; border-radius: var(--r-sm); overflow: hidden; background: linear-gradient(135deg, var(--accent), var(--accent-dark)); }
.np-art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.np-art-ph { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: rgba(255, 255, 255, .75); }
.np-art-ph .ic { width: 3rem; height: 3rem; }
.icon-btn.on { color: var(--accent); background: var(--surface-2); }
:root[data-theme="dark"] .icon-btn.on { color: var(--accent-light); }
.yt-status { background: var(--surface); border-left: 4px solid var(--brass); border-radius: var(--r-sm); padding: 1rem 1.2rem; box-shadow: var(--shadow); display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; justify-content: space-between; }
.yt-status p { margin: 0; color: var(--ink-2); max-width: 70ch; line-height: 1.5; }
.results-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-top: .5rem; }
.results-actions { display: flex; gap: .5rem; }
.tracks { list-style: none; margin: 0; padding: 0; display: grid; gap: .4rem; }
.track { display: grid; grid-template-columns: 6.5rem 1fr auto; gap: 1rem; align-items: center; background: var(--surface); border-radius: var(--r-sm); padding: .45rem; box-shadow: var(--shadow); cursor: pointer; border: 2px solid transparent; }
.track:hover { border-color: var(--line); }
.track.playing { border-color: var(--accent); }
.track img { width: 6.5rem; height: 3.65rem; object-fit: cover; border-radius: 6px; background: var(--surface-2); display: block; }
.track b { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track span { color: var(--ink-3); font-size: .85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }
.track .dur { color: var(--ink-3); font-variant-numeric: tabular-nums; padding-right: .6rem; }
.track > div { min-width: 0; }
.now-text { display: flex; flex-direction: column; min-width: 0; }
.now-text b { font-size: 1.15rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.now-text span { color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.np-progress { height: 6px; background: var(--surface-2); border-radius: 6px; overflow: hidden; cursor: pointer; }
.np-progress i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .9s linear; }
.np-times { display: flex; justify-content: space-between; color: var(--ink-3); font-size: .8rem; font-variant-numeric: tabular-nums; }
.np-controls { display: flex; justify-content: center; align-items: center; gap: .8rem; }
.volume { display: flex; align-items: center; gap: .6rem; color: var(--ink-3); }
.volume input { flex: 1; accent-color: var(--accent); }
.music-side h3 { font-size: 1rem; padding: 0 .2rem; }
.presets { display: grid; gap: .5rem; }
.preset {
  text-align: left; border: 0; background: var(--surface); border-radius: var(--r-sm); padding: .8rem 1rem;
  display: flex; flex-direction: column; gap: .1rem; box-shadow: var(--shadow); border-left: 4px solid transparent;
  transition: border-color .15s;
}
.preset:hover { border-left-color: var(--accent); }
.preset span { color: var(--ink-3); font-size: .85rem; }
.music-main { display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
.music-main > .search { flex: none; }
.music-main h3 { font-size: 1.1rem; }
.yt-card { background: var(--surface); border-radius: var(--r-md); padding: 1.5rem; box-shadow: var(--shadow); display: grid; gap: 1.1rem; }
.yt-card h2 { font-size: 1.6rem; letter-spacing: -.02em; }
.yt-card p { margin: .35rem 0 0; color: var(--ink-2); max-width: 60ch; line-height: 1.5; }
.yt-actions { display: flex; flex-wrap: wrap; gap: .6rem; }
.btn.big { padding: .95rem 1.4rem; font-size: 1.05rem; }
.music-note { color: var(--bad) !important; font-weight: 600; }
.presets { grid-template-columns: repeat(auto-fill, minmax(min(100%, 15rem), 1fr)); }
.music-bar { display: flex; align-items: center; gap: .3rem; padding: .6rem; border-bottom: 1px solid var(--line); }
.music-bar .search { margin-left: .4rem; }
.yt-wrap { position: relative; flex: 1; min-height: 0; }
#ytm { position: absolute; inset: 0; width: 100%; height: 100%; }
.yt-cover { position: absolute; inset: 0; display: grid; place-items: center; background: #030303; color: #fff; }

/* ---------------- Settings ---------------- */
.settings { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 30rem), 1fr)); gap: 1.25rem; align-items: start; }
.set-card { background: var(--surface); border-radius: var(--r-md); padding: 1.3rem 1.4rem; box-shadow: var(--shadow); }
.set-card h2 { font-size: 1.2rem; margin-bottom: .6rem; letter-spacing: -.01em; }
.set-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: .8rem 0; border-top: 1px solid var(--line); }
.set-row:first-of-type { border-top: 0; }
.set-label { display: flex; flex-direction: column; min-width: 0; }
.set-label span { color: var(--ink-3); font-size: .85rem; margin-top: .15rem; }
.set-row input[type=password] { background: var(--bg); border: 1.5px solid var(--line); border-radius: var(--r-sm); padding: .6rem .8rem; width: 14rem; max-width: 50%; }
.switch {
  flex: none; width: 3.1rem; height: 1.8rem; border-radius: 999px; border: 0; background: var(--line);
  position: relative; transition: background .2s;
}
.switch::after {
  content: ""; position: absolute; top: .2rem; left: .2rem; width: 1.4rem; height: 1.4rem; border-radius: 50%;
  background: #fff; box-shadow: 0 1px 3px rgba(0, 0, 0, .25); transition: transform .2s;
}
.switch.on { background: var(--accent); }
.switch.on::after { transform: translateX(1.3rem); }
.swatches { display: flex; flex-wrap: wrap; gap: .4rem; justify-content: flex-end; align-items: center; }
.swatch { width: 1.8rem; height: 1.8rem; border-radius: 50%; border: 0; background: var(--c); box-shadow: inset 0 0 0 2px rgba(255, 255, 255, .25); }
.swatch.on { box-shadow: 0 0 0 2px var(--surface), 0 0 0 4px var(--c); }
.swatches input[type=color] { width: 1.9rem; height: 1.9rem; border: 0; padding: 0; background: transparent; cursor: pointer; }
.storage { display: grid; grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); gap: .8rem; margin-bottom: 1rem; }
.storage div { background: var(--bg); border-radius: var(--r-sm); padding: .8rem; display: flex; flex-direction: column; }
.storage b { font-size: 1.2rem; }
.storage span { color: var(--ink-3); font-size: .82rem; }
.sys-btns { display: flex; flex-wrap: wrap; gap: .6rem; }
.shortcuts { color: var(--ink-3); font-size: .85rem; margin: 1rem 0 0; }
.about { grid-column: 1 / -1; display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap; background: var(--accent); color: var(--accent-ink); }
.about-logo { background: #fff; border-radius: var(--r-sm); padding: .6rem 1rem; }
.about-logo img { height: 2.6rem; display: block; }
.made-by { font-size: 1.5rem; font-weight: 700; letter-spacing: -.02em; margin: 0; flex: 1; }
.version { margin: 0; opacity: .75; }

/* ---------------- Viewer ---------------- */
.viewer { position: fixed; inset: 0; z-index: 100; background: #000; color: #fff; opacity: 0; transition: opacity .2s; cursor: none; }
.viewer.show { opacity: 1; }
.viewer.ui { cursor: default; }
.viewer-stage { position: absolute; top: 0; right: 0; bottom: 0; left: 0; display: flex; align-items: center; justify-content: center; overflow: hidden; transition: opacity .25s; }
.viewer-stage.fade { opacity: 0; }
/* Photos and videos always fit the screen completely (never cropped or zoomed) */
.v-media { position: absolute; top: 0; left: 0; width: 100%; height: 100%; max-width: 100%; max-height: 100%; object-fit: contain; object-position: center; display: block; background: transparent; z-index: 1; }
.v-web { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: 0; background: #fff; }
.v-pdf { background: #fff; box-shadow: 0 0 60px rgba(0, 0, 0, .5); max-width: 100%; max-height: 100%; }
.v-zone { position: absolute; top: 0; bottom: 0; width: 30%; z-index: 2; }
.v-zone.left { left: 0; cursor: w-resize; }
.v-zone.right { right: 0; cursor: e-resize; }
.v-text { width: min(90ch, 92%); height: 92%; overflow: auto; background: #111; color: #e8e8e8; padding: 2rem; border-radius: var(--r-sm); font: 1rem/1.6 ui-monospace, Consolas, monospace; white-space: pre-wrap; user-select: text; }
.v-audio { display: flex; flex-direction: column; align-items: center; gap: 1.8rem; text-align: center; padding: 2rem; }
.v-audio h2 { font-size: 2rem; max-width: 30ch; }
.v-audio audio { width: min(36rem, 90vw); }
.eq { display: flex; gap: .5rem; align-items: flex-end; height: 7rem; }
.eq i { width: 1.1rem; background: var(--accent); border-radius: 4px; animation: eq 1.1s ease-in-out infinite; filter: brightness(1.6); }
.eq i:nth-child(2) { animation-delay: -.3s; } .eq i:nth-child(3) { animation-delay: -.6s; }
.eq i:nth-child(4) { animation-delay: -.15s; } .eq i:nth-child(5) { animation-delay: -.45s; }
@keyframes eq { 0%, 100% { height: 20%; } 50% { height: 100%; } }
.v-message { display: flex; flex-direction: column; align-items: center; gap: 1rem; text-align: center; max-width: 40rem; padding: 2rem; }
.v-message p { color: #c9d1cf; line-height: 1.5; margin: 0; }
.v-msg-icon { width: 4rem; height: 4rem; color: #8fa5a1; }
.v-msg-actions { display: flex; gap: .7rem; flex-wrap: wrap; justify-content: center; margin-top: .6rem; }
.v-msg-actions .btn:not(.accent) { background: transparent; color: #fff; border-color: rgba(255, 255, 255, .3); }

.viewer-ui {
  position: absolute; top: 0; left: 0; right: 0; z-index: 3;
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 1rem 1.4rem 2.5rem;
  background: linear-gradient(rgba(0, 0, 0, .7), transparent);
  opacity: 0; transform: translateY(-8px); transition: opacity .25s, transform .25s; pointer-events: none;
}
.viewer.ui .viewer-ui { opacity: 1; transform: none; pointer-events: auto; }
.viewer-title { font-weight: 600; font-size: 1.1rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.viewer-actions { display: flex; gap: .5rem; flex: none; }
.v-btn {
  display: inline-flex; align-items: center; gap: .45rem; border: 0; border-radius: 999px;
  background: rgba(255, 255, 255, .14); color: #fff; padding: .55rem 1rem; font-weight: 600; backdrop-filter: blur(6px);
}
.v-btn:hover { background: rgba(255, 255, 255, .26); }
.viewer-pager {
  position: absolute; bottom: 1.2rem; left: 50%; transform: translateX(-50%); z-index: 3;
  background: rgba(0, 0, 0, .6); padding: .4rem 1rem; border-radius: 999px; font-variant-numeric: tabular-nums; font-weight: 600;
}
.viewer-hint {
  position: absolute; bottom: 4rem; left: 50%; transform: translate(-50%, 10px); z-index: 4;
  background: rgba(20, 20, 20, .8); padding: .7rem 1.3rem; border-radius: 999px;
  opacity: 0; transition: opacity .3s, transform .3s; pointer-events: none; white-space: nowrap; backdrop-filter: blur(6px);
}
.viewer-hint.show { opacity: 1; transform: translate(-50%, 0); }

/* ---------------- Modal, toasts, snow ---------------- */
.modal { position: fixed; inset: 0; z-index: 200; background: rgba(0, 0, 0, .45); display: grid; place-items: center; padding: 1rem; }
.modal-card { background: var(--surface); border-radius: var(--r-md); padding: 1.6rem; width: min(28rem, 100%); box-shadow: var(--shadow); }
.modal-card h3 { font-size: 1.25rem; }
.modal-card p { color: var(--ink-2); margin: .6rem 0 1.4rem; word-break: break-word; }
.modal-card p:empty { margin: .6rem 0; }
.modal-actions { display: flex; justify-content: flex-end; gap: .6rem; }
.toasts { position: fixed; bottom: 1.4rem; left: 50%; transform: translateX(-50%); z-index: 300; display: flex; flex-direction: column; gap: .5rem; align-items: center; pointer-events: none; }
.toast {
  background: var(--ink); color: var(--bg); padding: .8rem 1.3rem; border-radius: var(--r-sm); font-weight: 600;
  box-shadow: var(--shadow); opacity: 0; transform: translateY(10px); transition: opacity .25s, transform .25s; max-width: 80vw; text-align: center;
}
.toast.show { opacity: 1; transform: none; }
.toast.ok { background: var(--ok); color: #fff; }
.toast.bad { background: var(--bad); color: #fff; }
#snow { position: fixed; inset: 0; z-index: 50; pointer-events: none; }
:root:not([data-theme="dark"]) #snow { mix-blend-mode: normal; filter: drop-shadow(0 0 1px rgba(0, 40, 50, .35)); }

/* ---------------- Responsive ---------------- */
@media (max-width: 1200px) {
  .clock span { display: none; }
  .mini-text { display: none; }
}
@media (max-width: 980px) {
  .tab > span:not(.ic) { display: none; }
  .tab { padding: .7rem .9rem; }
  .upload-layout { grid-template-columns: 1fr; align-items: start; }
  .qr-stage { height: auto; }
  .qr-frame { width: min(100%, 50vh, 26rem); }
  .welcome {
  margin: 0 0 1.2rem; font-size: clamp(1.3rem, 2vw, 2.6rem); font-weight: 650; letter-spacing: -.02em;
  color: var(--brass); font-family: "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif;
}
.remote-stage { background: var(--accent-dark); }
.remote-warn { color: var(--ink-3); font-size: .9rem; max-width: 46ch; margin-top: 1.2rem; line-height: 1.5; }
.text-input { background: var(--bg); border: 1.5px solid var(--line); border-radius: var(--r-sm); padding: .6rem .8rem; width: 14rem; max-width: 50%; }
.upload-copy h1 { max-width: none; }
  .music-layout { grid-template-columns: 1fr; }
  .now { position: static; }
  .now img { max-width: 12rem; align-self: center; }
}
@media (max-width: 640px) {
  :root { --header-h: auto; }
  body { grid-template-rows: auto 1fr; }
  .topbar { grid-template-columns: 1fr auto; padding-top: .6rem; padding-bottom: .6rem; row-gap: .6rem; }
  .brand img { height: 2.2rem; }
  .tabs { grid-column: 1 / -1; grid-row: 2; justify-content: space-between; }
  .tab { flex: 1; justify-content: center; padding: .6rem; }
  .clock b { font-size: 1.1rem; }
  .mini { display: none !important; }
  .offline > span:not(.ic) { display: none; }
  .viewer-actions .v-btn > span:not(.ic) { display: none; }
  .set-row { flex-wrap: wrap; }
}
@media (min-width: 2400px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(min(100%, 420px), 1fr)); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

/* Older web engines (e.g. Qt 5 WebEngine) without aspect-ratio */
@supports not (aspect-ratio: 1 / 1) {
  .qr-frame { width: min(62vh, 40vw); height: min(62vh, 40vw); }
  .thumb { height: 0; padding-top: 75%; }
  .thumb > .thumb-icon, .thumb > .play-badge { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); }
  @media (max-width: 980px) { .qr-frame { width: min(50vh, 26rem, 80vw); height: min(50vh, 26rem, 80vw); } }
}

.thumb-badge { position: absolute; left: .5rem; bottom: .5rem; z-index: 2; background: rgba(0, 0, 0, .65); color: #fff; font-size: .75rem; font-weight: 600; padding: .25rem .55rem; border-radius: 999px; display: flex; align-items: center; gap: .35rem; }
.thumb-badge .spinner.sm { width: .8rem; height: .8rem; }
.lib-setup { background: var(--surface); border-radius: var(--r-md); box-shadow: var(--shadow); padding: 1.6rem; display: grid; gap: 1rem; max-width: 60rem; }
.lib-setup h2 { font-size: 1.5rem; letter-spacing: -.02em; }
.lib-setup p { margin: 0; color: var(--ink-2); line-height: 1.55; max-width: 70ch; }
.lib-providers { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr)); gap: .8rem; }
.lib-provider { border: 1.5px solid var(--line); border-radius: var(--r-sm); padding: 1rem; display: grid; gap: .6rem; align-content: start; }
.lib-provider b { font-size: 1.1rem; }
.lib-provider span { color: var(--ink-3); font-size: .88rem; }
.lib-provider input { background: var(--bg); border: 1.5px solid var(--line); border-radius: var(--r-sm); padding: .6rem .8rem; width: 100%; }
.lib-provider .row { display: flex; gap: .5rem; flex-wrap: wrap; }
.lib-provider.ok { border-color: var(--ok); }
.lib-credit { color: var(--ink-3); font-size: .85rem; margin: -.4rem 0 1rem; }
.lib-credit a { color: inherit; cursor: pointer; text-decoration: underline; }
.key-row { display: flex; gap: .5rem; align-items: center; justify-content: flex-end; flex-wrap: wrap; }
.key-row .text-input { max-width: 16rem; width: 16rem; }

.deps-title { font-size: 1rem; margin: 1.2rem 0 .4rem; }
.deps { display: grid; gap: .5rem; }
.dep-row { display: grid; grid-template-columns: 1fr auto auto; gap: .8rem; align-items: center; background: var(--bg); border-radius: var(--r-sm); padding: .6rem .8rem; }
.dep-row b { display: block; }
.dep-row span { color: var(--ink-3); font-size: .82rem; }
.dep-row .dep-state { font-weight: 600; font-size: .85rem; color: var(--ink-2); white-space: nowrap; }
.dep-row.ok .dep-state { color: var(--ok); }
.dep-row.bad .dep-state { color: var(--bad); }

.v-wait { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 2; display: flex; flex-direction: column; align-items: center; gap: 1rem; color: #fff; pointer-events: none; text-align: center; }
.v-wait p { margin: 0; color: #c9d1cf; }

/* Colour-matched blurred edges behind photos and videos */
.v-blur {
  position: absolute; top: -8%; left: -8%; width: 116%; height: 116%; z-index: 0;
  filter: blur(28px) saturate(1.25) brightness(.62); transform: translateZ(0);
}
.viewer-stage.fade .v-blur { transition: opacity .25s; }

/* Zoom to fill the screen (may crop the edges) */
.v-media.cover { object-fit: cover; }
/* Fembimmo logo on photos and videos */
.v-logo {
  position: absolute; z-index: 3; height: auto; background: rgba(255, 255, 255, .92); padding: .45em .7em;
  border-radius: 10px; box-shadow: 0 4px 18px rgba(0, 0, 0, .28); pointer-events: none; box-sizing: border-box;
  transition: transform .25s;
}
.viewer.ui .v-logo.under-ui { transform: translateY(64px); }
.thumb-logo {
  position: absolute !important; top: .5rem; right: .5rem; left: auto !important; bottom: auto !important;
  width: 26% !important; max-width: 84px; height: auto !important; z-index: 2; opacity: 1 !important;
  background: rgba(255, 255, 255, .92) !important; padding: 4px 6px; border-radius: 6px; box-sizing: border-box;
  box-shadow: 0 2px 8px rgba(0, 0, 0, .25); object-fit: contain !important;
}
/* delete button moves to the top-left so it never covers the logo */
.card-del { right: auto; left: .5rem; }

</style>
</head>
<body>
<canvas id="snow" aria-hidden="true"></canvas>

<header class="topbar">
  <div class="brand">
    <img id="logo" src="/logo" alt="Fembimmo">
  </div>

  <nav class="tabs" role="tablist" aria-label="Menu">
    <button class="tab active" role="tab" data-tab="upload" data-icon="upload"><span data-i18n="tab.upload"></span></button>
    <button class="tab" role="tab" data-tab="gallery" data-icon="grid"><span data-i18n="tab.gallery"></span><b class="count" id="galleryCount"></b></button>
    <button class="tab" role="tab" data-tab="library" data-icon="search"><span data-i18n="tab.library"></span></button>
    <button class="tab" role="tab" data-tab="music" data-icon="music"><span data-i18n="tab.music"></span></button>
    <button class="tab" role="tab" data-tab="remote" data-icon="remote"><span data-i18n="tab.remote"></span></button>
    <button class="tab" role="tab" data-tab="settings" data-icon="settings"><span data-i18n="tab.settings"></span></button>
  </nav>

  <div class="status">
    <div class="mini hidden" id="mini">
      <img id="miniArt" alt="">
      <div class="mini-text"><b id="miniTitle"></b><span id="miniArtist"></span></div>
      <button class="icon-btn" data-icon="prev" data-music="prev" data-i18n-title="music.prev"></button>
      <button class="icon-btn play" id="miniPlay" data-music="toggle" data-i18n-title="music.playPause"></button>
      <button class="icon-btn" data-icon="next" data-music="next" data-i18n-title="music.next"></button>
    </div>
    <div class="offline hidden" id="offline" role="status" data-icon="wifiOff"><span data-i18n="net.offline"></span></div>
    <div class="clock" id="clock"><b></b><span></span></div>
  </div>
</header>

<main class="panels">
  <!-- UPLOAD -->
  <section class="panel active" id="tab-upload" role="tabpanel">
    <div class="upload-layout">
      <div class="qr-stage">
        <div class="qr-frame">
          <img id="qrImg" class="hidden" alt="QR code">
          <div class="qr-wait" id="qrWait"><div class="spinner"></div><p id="qrWaitText"></p></div>
        </div>
        <div class="short-link" id="shortLink">&nbsp;</div>
        <div class="expiry">
          <div class="expiry-bar"><i id="expiryBar"></i></div>
          <span id="expiryText"></span>
        </div>
      </div>

      <div class="upload-copy">
        <p class="welcome" id="welcome"></p>
        <h1 data-i18n="upload.title"></h1>
        <p class="lead" data-i18n="upload.lead"></p>
        <ol class="steps">
          <li><span class="step-n">1</span><div><b data-i18n="upload.s1"></b><p data-i18n="upload.s1d"></p></div></li>
          <li><span class="step-n">2</span><div><b data-i18n="upload.s2"></b><p data-i18n="upload.s2d"></p></div></li>
          <li><span class="step-n">3</span><div><b data-i18n="upload.s3"></b><p data-i18n="upload.s3d"></p></div></li>
        </ol>
        <div class="types" id="typeChips"></div>
        <div class="upload-actions">
          <button class="btn" id="btnNewLink" data-icon="refresh"><span data-i18n="upload.newLink"></span></button>
        </div>
        <div class="incoming hidden" id="incoming">
          <h3 data-i18n="upload.incoming"></h3>
          <ul id="incomingList"></ul>
        </div>
      </div>
    </div>
  </section>

  <!-- GALLERY -->
  <section class="panel" id="tab-gallery" role="tabpanel">
    <div class="toolbar">
      <label class="search" data-icon="search"><input id="gallerySearch" type="search" data-i18n-ph="gallery.search"></label>
      <div class="seg" id="galleryFilter">
        <button data-f="all" class="on" data-i18n="gallery.all"></button>
        <button data-f="image" data-i18n="gallery.image"></button>
        <button data-f="video" data-i18n="gallery.video"></button>
        <button data-f="audio" data-i18n="gallery.audio"></button>
        <button data-f="doc" data-i18n="gallery.doc"></button>
        <button data-f="link" data-i18n="gallery.link"></button>
      </div>
      <button class="btn accent" id="btnSlideshow" data-icon="play"><span data-i18n="gallery.slideshow"></span></button>
    </div>
    <div class="grid" id="galleryGrid"></div>
    <div class="empty hidden" id="galleryEmpty">
      <h2 data-i18n="gallery.empty"></h2>
      <p data-i18n="gallery.emptySub"></p>
      <button class="btn accent" data-goto="upload" data-icon="upload"><span data-i18n="gallery.goUpload"></span></button>
    </div>
  </section>

  <!-- LIBRARY -->
  <section class="panel" id="tab-library" role="tabpanel">
    <form class="lib-search" id="libForm">
      <label class="search big" data-icon="search"><input id="libQuery" type="search" autocomplete="off" data-i18n-ph="lib.search"></label>
      <div class="seg" id="libKind">
        <button type="button" data-k="image" class="on" data-i18n="lib.images"></button>
        <button type="button" data-k="video" data-i18n="lib.videos"></button>
      </div>
      <select id="libSource" aria-label="Source">
        <option value="auto" data-i18n="lib.auto"></option>
        <option value="pexels">Pexels</option>
        <option value="pixabay">Pixabay</option>
        <option value="unsplash">Unsplash</option>
      </select>
      <button class="btn accent" type="submit" data-i18n="lib.go"></button>
    </form>
    <div class="lib-setup hidden" id="libSetup"></div>
    <div class="chips" id="libSuggest"></div>
    <p class="lib-credit" id="libCredit"></p>
    <p class="lib-note" id="libNote"></p>
    <div class="grid" id="libGrid"></div>
    <div class="lib-foot">
      <button class="btn hidden" id="libMore" data-i18n="lib.more"></button>
      <div class="spinner hidden" id="libSpin"></div>
    </div>
  </section>

  <!-- MUSIC -->
  <section class="panel" id="tab-music" role="tabpanel">
    <div class="music-layout">
      <aside class="now">
        <div class="np-art"><img id="npArt" alt=""><span class="np-art-ph" data-icon="music"></span></div>
        <div class="now-text">
          <b id="npTitle"></b>
          <span id="npArtist"></span>
        </div>
        <div class="np-progress" id="npProgress"><i></i></div>
        <div class="np-times"><span id="npCur">0:00</span><span id="npDur">0:00</span></div>
        <div class="np-controls">
          <button class="icon-btn lg" id="npShuffle" data-icon="shuffle" data-i18n-title="music.shuffle"></button>
          <button class="icon-btn lg" data-icon="prev" data-music="prev" data-i18n-title="music.prev"></button>
          <button class="icon-btn xl play" id="npPlay" data-music="toggle" data-i18n-title="music.playPause"></button>
          <button class="icon-btn lg" data-icon="next" data-music="next" data-i18n-title="music.next"></button>
          <button class="icon-btn lg" id="npStop" data-icon="stop" data-i18n-title="music.stop"></button>
        </div>
        <label class="volume" data-icon="volume"><input type="range" id="npVol" min="0" max="100" value="80"></label>
      </aside>
      <div class="music-main">
        <div class="yt-status hidden" id="ytStatus">
          <p id="ytStatusText"></p>
          <button class="btn accent" id="ytInstall" data-icon="download"><span data-i18n="music.install"></span></button>
        </div>
        <form id="ytSearch" class="search big" data-icon="search"><input id="ytQuery" type="search" autocomplete="off" data-i18n-ph="music.search"></form>
        <h3 data-i18n="music.presets"></h3>
        <div class="presets" id="presets"></div>
        <div class="results-head">
          <h3 id="resultsTitle"></h3>
          <div class="results-actions hidden" id="resultsActions">
            <button class="btn" id="btnPlayAll" data-icon="play"><span data-i18n="music.playAll"></span></button>
            <button class="btn" id="btnShuffleAll" data-icon="shuffle"><span data-i18n="music.shuffleAll"></span></button>
          </div>
        </div>
        <div class="spinner hidden" id="ytSpin"></div>
        <ol class="tracks" id="tracks"></ol>
      </div>
    </div>
  </section>

  <!-- REMOTE -->
  <section class="panel" id="tab-remote" role="tabpanel">
    <div class="upload-layout">
      <div class="qr-stage remote-stage">
        <div class="qr-frame">
          <img id="rqrImg" class="hidden" alt="QR code">
          <div class="qr-wait" id="rqrWait"><div class="spinner"></div><p id="rqrWaitText"></p></div>
        </div>
        <div class="short-link" id="rShortLink">&nbsp;</div>
        <div class="expiry">
          <span id="rExpiryText"></span>
        </div>
      </div>
      <div class="upload-copy">
        <h1 data-i18n="remote.title"></h1>
        <p class="lead" data-i18n="remote.lead"></p>
        <ol class="steps">
          <li><span class="step-n">1</span><div><b data-i18n="remote.s1"></b><p data-i18n="remote.s1d"></p></div></li>
          <li><span class="step-n">2</span><div><b data-i18n="remote.s2"></b><p data-i18n="remote.s2d"></p></div></li>
          <li><span class="step-n">3</span><div><b data-i18n="remote.s3"></b><p data-i18n="remote.s3d"></p></div></li>
        </ol>
        <div class="upload-actions">
          <button class="btn" id="btnNewRemote" data-icon="refresh"><span data-i18n="remote.newLink"></span></button>
        </div>
        <p class="remote-warn" data-i18n="remote.warn"></p>
      </div>
    </div>
  </section>

  <!-- SETTINGS -->
  <section class="panel" id="tab-settings" role="tabpanel">
    <div class="settings" id="settingsRoot"></div>
  </section>
</main>

<!-- Full screen viewer -->
<div class="viewer hidden" id="viewer" aria-modal="true" role="dialog">
  <div class="viewer-stage" id="viewerStage"></div>
  <div class="viewer-ui" id="viewerUi">
    <div class="viewer-title" id="viewerTitle"></div>
    <div class="viewer-actions" id="viewerActions"></div>
  </div>
  <div class="viewer-pager hidden" id="viewerPager"></div>
  <div class="viewer-hint" id="viewerHint"></div>
</div>

<div class="modal hidden" id="modal">
  <div class="modal-card">
    <h3 id="modalTitle"></h3>
    <p id="modalText"></p>
    <div class="modal-actions">
      <button class="btn" id="modalCancel"></button>
      <button class="btn danger" id="modalOk"></button>
    </div>
  </div>
</div>

<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
/* Translations — nl (default) and en */
window.I18N = {
  dict: {
    nl: {
      'music.shuffle': 'Shuffle', 'music.stop': 'Stoppen', 'music.install': 'Installeren',
      'music.installing': 'yt-dlp en Deno worden gedownload of bijgewerkt… dit kan een minuut duren.',
      'music.installFailed': 'Installeren van yt-dlp is mislukt: {err}', 'music.installFailedShort': 'Installeren mislukt',
      'music.installed': 'yt-dlp is klaar', 'music.update': 'yt-dlp bijwerken',
      'music.missing': 'yt-dlp wordt automatisch gedownload.',
      'music.noJs': 'Deno (nodig voor YouTube) is nog niet gedownload. Klik op de knop om het te installeren.',
      'music.playAll': 'Alles afspelen', 'music.shuffleAll': 'Shuffle', 'music.noResults': 'Niets gevonden.',
      'music.searchFailed': 'Zoeken op YouTube lukt niet. Werk yt-dlp bij in Instellingen.',
      'music.trackFailed': 'Dit nummer kan niet afgespeeld worden, volgende…',
      'viewer.externalPlayer': 'Deze video wordt afgespeeld in de mediaspeler van de computer.',
      'set.musicShuffle': 'Shuffle', 'set.musicShuffle.d': 'Speel lijsten in willekeurige volgorde.',
      'viewer.preparing': 'Video wordt geladen…',
      'set.viewerFit': 'Volledig scherm', 'set.viewerFit.d': 'Hoe foto’s en video’s het scherm vullen.',
      'set.fit.blur': 'Passend met wazige randen', 'set.fit.contain': 'Passend met zwarte randen', 'set.fit.cover': 'Inzoomen (scherm helemaal vol)',
      'set.showLogo': 'Fembimmo-logo tonen', 'set.showLogo.d': 'Logo rechtsboven op foto’s en video’s in de galerij en op volledig scherm.',
      'gallery.converting': 'Omzetten…', 'gallery.videoReady': 'Video klaar om af te spelen: {name}',
      'viewer.converting': 'Deze video wordt omgezet zodat hij in de app speelt. Ondertussen speelt hij in de mediaspeler.',
      'viewer.addToPlay': 'Voeg deze video toe aan de galerij om hem af te spelen.',
      'lib.setupTitle': 'Echte stockfoto’s en -video’s', 'lib.setupText': 'Koppel minstens één gratis stockdienst. Een API-sleutel aanvragen is gratis en duurt een minuut: maak een account, kopieer de sleutel en plak hem hieronder. Alle foto’s en video’s zijn zonder watermerk en vrij te gebruiken.',
      'lib.pasteKey': 'Plak je API-sleutel', 'lib.saveKey': 'Opslaan', 'lib.getKey': 'Gratis sleutel',
      'lib.kind.photos+videos': 'Foto’s en video’s, gratis', 'lib.kind.photos': 'Foto’s, gratis',
      'lib.credit': 'Foto’s en video’s via',
      'lib.err.NO_KEY': 'Vul eerst een API-sleutel voor {name} in bij Instellingen.',
      'lib.err.INVALID_KEY': 'De API-sleutel van {name} klopt niet. Controleer hem in Instellingen.',
      'lib.err.RATE_LIMIT': '{name} heeft even te veel zoekopdrachten gehad. Probeer het zo opnieuw.',
      'lib.err.NO_VIDEO': '{name} heeft geen video’s. Kies Pexels of Pixabay.',
      'lib.err.NO_VIDEO_SOURCE': 'Voor video’s is een sleutel van Pexels of Pixabay nodig.',
      'set.pexelsKey': 'Pexels API-sleutel', 'set.pexelsKey.d': 'Gratis. Foto’s en video’s.',
      'set.pixabayKey': 'Pixabay API-sleutel', 'set.pixabayKey.d': 'Gratis. Foto’s en video’s.',
      'set.unsplashKey': 'Unsplash Access Key', 'set.unsplashKey.d': 'Gratis. Alleen foto’s.',
      'deps.title': 'Onderdelen (worden automatisch gedownload)', 'deps.installed': 'Geïnstalleerd', 'deps.missing': 'Niet aanwezig',
      'deps.downloading': 'Downloaden… {pct}%', 'deps.error': 'Mislukt', 'deps.update': 'Bijwerken', 'deps.install': 'Installeren',
      'deps.ytdlp': 'Muziek van YouTube', 'deps.deno': 'Nodig voor yt-dlp en YouTube', 'deps.ffmpeg': 'Zet video’s om als het venster MP4 niet kan afspelen',
      'copy.done': 'Link gekopieerd', 'copy.failed': 'Kopiëren lukte niet: {url}', 'copy.hint': 'Klik om de link te kopiëren',
      'tab.remote': 'Afstandsbediening',
      'upload.welcome': 'Welkom, {name}', 'upload.welcomeAnon': 'Welkom',
      'qr.missing': 'QR-module ontbreekt. Voer uit: pip install segno',
      'remote.title': 'Bedien dit scherm met je gsm',
      'remote.lead': 'Scan deze code en gebruik je gsm als afstandsbediening: wissel van tabblad, open items uit de galerij, blader door presentaties en bedien de muziek.',
      'remote.s1': 'Scan de QR-code', 'remote.s1d': 'De afstandsbediening opent in je browser.',
      'remote.s2': 'Kies wat er op het scherm komt', 'remote.s2d': 'Tik op een foto, video of document uit de galerij.',
      'remote.s3': 'Bedien alles', 'remote.s3d': 'Volgende, vorige, sluiten, diavoorstelling en muziek.',
      'remote.newLink': 'Nieuwe afstandsbedieningslink', 'remote.made': 'Nieuwe afstandsbedieningslink gemaakt. Oude links werken niet meer.',
      'remote.valid': 'Geldig tot je een nieuwe link maakt',
      'remote.warn': 'Iedereen met deze link kan het scherm bedienen. Maak een nieuwe link om oude toegang in te trekken.',
      'music.ytTitle': 'YouTube Music', 'music.ytText': 'Kies hieronder een kerstlijst of zoek een nummer. De muziek speelt verder op de achtergrond, ook in andere tabbladen.',
      'music.open': 'YouTube Music openen', 'music.noNative': 'Muziek werkt alleen in het app-venster (niet in een gewone browser).',
      'set.welcomeName': 'Naam op het startscherm', 'set.welcomeName.d': 'Voor de begroeting „Welkom, …”.', 'set.welcomeName.ph': 'Naam',
      'set.group.remote': 'Afstandsbediening', 'set.remoteRefreshMin': 'Nieuwe afstandsbedieningslink elke', 'set.remoteRefreshMin.d': 'Een nieuwe link maakt oude links meteen ongeldig.',
      'tab.upload': 'Upload', 'tab.gallery': 'Galerij', 'tab.library': 'Bibliotheek', 'tab.music': 'Muziek', 'tab.settings': 'Instellingen',

      'upload.title': 'Zet het op het scherm',
      'upload.lead': 'Scan de code met je gsm en stuur foto’s, video’s, presentaties of links rechtstreeks naar dit scherm. Inloggen is niet nodig.',
      'upload.s1': 'Scan de QR-code', 'upload.s1d': 'Met de camera van je gsm of tablet.',
      'upload.s2': 'Kies wat je wil tonen', 'upload.s2d': 'Bestanden, een foto die je net maakt of een websitelink.',
      'upload.s3': 'Klaar', 'upload.s3d': 'Het verschijnt meteen in de galerij op dit scherm.',
      'upload.newLink': 'Nieuwe link maken', 'upload.incoming': 'Wordt nu ontvangen',
      'upload.expiresIn': 'Nieuwe code over {time}', 'upload.noExpiry': 'Deze code blijft geldig',
      'upload.failed': 'Mislukt', 'upload.received': 'Ontvangen',
      'chip.images': 'Foto’s', 'chip.videos': 'Video’s', 'chip.audio': 'Audio', 'chip.links': 'Websites', 'chip.more': 'En meer',

      'tunnel.starting': 'Uploadlink wordt klaargezet…', 'tunnel.downloading': 'Eerste start: verbindingsprogramma wordt gedownload…',
      'tunnel.error': 'Geen verbinding met de uploadserver. Er wordt automatisch opnieuw geprobeerd.', 'tunnel.link': 'QR-code wordt gemaakt…',
      'net.offline': 'Geen wifi', 'net.back': 'Weer online', 'net.offlineUpload': 'Geen internetverbinding. De QR-code verschijnt zodra de wifi terug is.',

      'gallery.search': 'Zoeken in galerij', 'gallery.all': 'Alles', 'gallery.image': 'Foto’s', 'gallery.video': 'Video’s',
      'gallery.audio': 'Audio', 'gallery.doc': 'Documenten', 'gallery.link': 'Links', 'gallery.slideshow': 'Diavoorstelling',
      'gallery.empty': 'Nog niets geüpload', 'gallery.emptySub': 'Scan de QR-code op het Upload-tabblad om iets op het scherm te zetten.',
      'gallery.goUpload': 'Naar Upload', 'gallery.processing': 'Dit bestand wordt nog verwerkt.',
      'gallery.delete': 'Verwijderen', 'gallery.deleteConfirm': 'Dit item verwijderen?', 'gallery.deleted': 'Verwijderd',
      'gallery.newUpload': 'Nieuw: {name}', 'gallery.noSlides': 'Geen foto’s of video’s voor een diavoorstelling.',

      'type.image': 'Foto', 'type.video': 'Video', 'type.audio': 'Audio', 'type.pdf': 'PDF', 'type.presentation': 'Presentatie',
      'type.document': 'Document', 'type.text': 'Tekst', 'type.html': 'Webpagina', 'type.link': 'Link', 'type.other': 'Bestand',

      'viewer.hint': 'Druk op een toets om terug te gaan', 'viewer.hintSlides': 'Pijltjes om te bladeren, andere toets om te stoppen',
      'viewer.close': 'Sluiten', 'viewer.external': 'Openen met app', 'viewer.present': 'Presenteren in PowerPoint',
      'viewer.source': 'Bron', 'viewer.loading': 'Laden…',
      'viewer.unsupported': 'Dit bestand kan hier niet getoond worden. Open het met de app op deze pc.',
      'viewer.noPreview': 'Voor dit bestandstype is geen voorbeeld beschikbaar.',
      'viewer.noOffice': 'Installeer LibreOffice (gratis) om presentaties en documenten hier te tonen, of open ze in PowerPoint.',
      'viewer.convertFailed': 'Omzetten is mislukt. Open het bestand met PowerPoint of Word.',

      'lib.search': 'Zoek stockfoto’s en -video’s', 'lib.images': 'Foto’s', 'lib.videos': 'Video’s',
      'lib.auto': 'Automatische bron', 'lib.go': 'Zoeken', 'lib.more': 'Meer laden',
      'lib.start': 'Zoek een onderwerp of kies een suggestie. Klik op een resultaat om het te bekijken, + om het aan de galerij toe te voegen.',
      'lib.empty': 'Niets gevonden. Probeer een ander woord, bij voorkeur in het Engels.',
      'lib.error': 'Zoeken lukt niet. Controleer de internetverbinding en probeer opnieuw.',
      'lib.needKey': 'Voor Pexels is een gratis API-sleutel nodig. Vul die in bij Instellingen.',
      'lib.via': 'Resultaten via {source}. Klik om te bekijken, + om aan de galerij toe te voegen.',
      'lib.add': 'Toevoegen aan galerij', 'lib.adding': 'Wordt gedownload…', 'lib.added': 'Toegevoegd aan de galerij',
      'lib.addFailed': 'Toevoegen mislukt',

      'music.presets': 'Kerstmuziek', 'music.nothing': 'Er speelt niets', 'music.pick': 'Kies een kerstlijst of zoek een nummer',
      'music.prev': 'Vorige', 'music.next': 'Volgende', 'music.playPause': 'Afspelen / pauzeren', 'music.back': 'Terug',
      'music.forward': 'Vooruit', 'music.home': 'YouTube Music start', 'music.search': 'Zoek een nummer of artiest op YouTube',
      'music.loading': '{name} wordt geladen…',

      'set.group.general': 'Algemeen', 'set.group.upload': 'Uploaden', 'set.group.display': 'Weergave', 'set.group.music': 'Muziek',
      'set.group.library': 'Bibliotheek (stockfoto’s en -video’s)', 'set.group.system': 'Systeem',
      'set.language': 'Taal', 'set.language.d': 'Taal van de app en de uploadpagina.',
      'set.theme': 'Thema', 'set.theme.d': 'Licht of donker.', 'set.light': 'Licht', 'set.dark': 'Donker',
      'set.accent': 'Accentkleur', 'set.accent.d': 'Kleur van knoppen en het QR-vlak.', 'set.custom': 'Eigen kleur',
      'set.autoStart': 'Starten met de pc', 'set.autoStart.d': 'Opent automatisch wanneer de computer opstart.',
      'set.fullscreen': 'Volledig scherm', 'set.fullscreen.d': 'Ook met F11. Afsluiten met Ctrl+Shift+Q.',
      'set.kiosk': 'Kioskmodus', 'set.kiosk.d': 'Vergrendeld volledig scherm. Afsluiten met Ctrl+Shift+Q.',
      'set.keepAwake': 'Scherm wakker houden', 'set.keepAwake.d': 'Voorkomt dat het scherm uitgaat.',
      'set.snow': 'Sneeuw', 'set.snow.d': 'Vallende sneeuw over het scherm.',
      'set.linkRefreshMin': 'Nieuwe QR-code elke', 'set.linkRefreshMin.d': 'Oude links werken daarna nog 10 minuten.',
      'set.maxUploadMB': 'Maximale bestandsgrootte', 'set.maxUploadMB.d': 'Per geüpload bestand.',
      'set.switchToGallery': 'Naar galerij na upload', 'set.switchToGallery.d': 'Toont de galerij zodra er iets binnenkomt.',
      'set.autoShowNew': 'Nieuwe uploads meteen tonen', 'set.autoShowNew.d': 'Opent nieuwe foto’s en video’s volledig scherm.',
      'set.slideshowSec': 'Tijd per dia', 'set.slideshowSec.d': 'Video’s spelen altijd volledig af.',
      'set.docArrowNav': 'Bladeren met pijltjes', 'set.docArrowNav.d': 'In PDF’s en presentaties. Andere toetsen sluiten.',
      'set.idleReturnMin': 'Terug naar Upload na', 'set.idleReturnMin.d': 'Zonder activiteit keert de app terug naar de QR-code.',
      'set.pauseMusicOnVideo': 'Muziek pauzeren bij video', 'set.pauseMusicOnVideo.d': 'Gaat daarna vanzelf verder.',
      'set.musicAutoplay': 'Kerstmuziek bij opstarten', 'set.musicAutoplay.d': 'Start Kersthits zodra de app opent.',
      'set.pexelsKey': 'Pexels API-sleutel', 'set.pexelsKey.d': 'Optioneel, gratis via pexels.com/api. Geeft meer en betere resultaten.',
      'set.pexelsKey.ph': 'Plak hier je sleutel',
      'set.minutes': '{n} min', 'set.seconds': '{n} sec', 'set.never': 'Nooit', 'set.off': 'Uit', 'set.saved': 'Opgeslagen',
      'set.restartTunnel': 'Verbinding herstarten', 'set.openFolder': 'Uploadmap openen', 'set.toggleFs': 'Volledig scherm aan/uit',
      'set.deleteAll': 'Alles verwijderen', 'set.deleteAllQ': 'Alle uploads verwijderen?', 'set.deleteAllD': 'Dit kan niet ongedaan gemaakt worden.',
      'set.quit': 'Afsluiten', 'set.quitQ': 'Fembimmo Display afsluiten?', 'set.linkMade': 'Nieuwe QR-code gemaakt',
      'set.storageUsed': 'gebruikt door {n} items', 'set.storageFree': 'vrij op de schijf', 'set.office': 'LibreOffice voor presentaties',
      'set.yes': 'Ja', 'set.no': 'Nee',
      'set.shortcuts': 'Sneltoetsen: F11 volledig scherm, Ctrl+Shift+Q afsluiten.',
      'set.version': 'Versie {v}',
      'common.cancel': 'Annuleren', 'common.delete': 'Verwijderen',
    },
    en: {
      'music.shuffle': 'Shuffle', 'music.stop': 'Stop', 'music.install': 'Install',
      'music.installing': 'Downloading or updating yt-dlp and Deno… this can take a minute.',
      'music.installFailed': 'Installing yt-dlp failed: {err}', 'music.installFailedShort': 'Install failed',
      'music.installed': 'yt-dlp is ready', 'music.update': 'Update yt-dlp',
      'music.missing': 'yt-dlp is downloaded automatically.',
      'music.noJs': 'Deno (needed for YouTube) has not been downloaded yet. Click the button to install it.',
      'music.playAll': 'Play all', 'music.shuffleAll': 'Shuffle', 'music.noResults': 'Nothing found.',
      'music.searchFailed': 'YouTube search failed. Update yt-dlp in Settings.',
      'music.trackFailed': 'This track can’t be played, skipping…',
      'viewer.externalPlayer': 'This video is playing in the computer’s media player.',
      'set.musicShuffle': 'Shuffle', 'set.musicShuffle.d': 'Play lists in random order.',
      'viewer.preparing': 'Loading video…',
      'set.viewerFit': 'Full screen', 'set.viewerFit.d': 'How photos and videos fill the screen.',
      'set.fit.blur': 'Fit with blurred edges', 'set.fit.contain': 'Fit with black edges', 'set.fit.cover': 'Zoom to fill the screen',
      'set.showLogo': 'Show Fembimmo logo', 'set.showLogo.d': 'Logo in the top-right corner of photos and videos in the gallery and full screen.',
      'gallery.converting': 'Converting…', 'gallery.videoReady': 'Video ready to play: {name}',
      'viewer.converting': 'This video is being converted so it plays in the app. Meanwhile it plays in the media player.',
      'viewer.addToPlay': 'Add this video to the gallery to play it.',
      'lib.setupTitle': 'Real stock photos and videos', 'lib.setupText': 'Connect at least one free stock service. Getting an API key is free and takes a minute: create an account, copy the key and paste it below. All photos and videos are watermark-free and free to use.',
      'lib.pasteKey': 'Paste your API key', 'lib.saveKey': 'Save', 'lib.getKey': 'Free key',
      'lib.kind.photos+videos': 'Photos and videos, free', 'lib.kind.photos': 'Photos, free',
      'lib.credit': 'Photos and videos provided by',
      'lib.err.NO_KEY': 'Add an API key for {name} in Settings first.',
      'lib.err.INVALID_KEY': 'The {name} API key is not valid. Check it in Settings.',
      'lib.err.RATE_LIMIT': '{name} received too many searches. Try again in a moment.',
      'lib.err.NO_VIDEO': '{name} has no videos. Pick Pexels or Pixabay.',
      'lib.err.NO_VIDEO_SOURCE': 'Videos need a Pexels or Pixabay key.',
      'set.pexelsKey': 'Pexels API key', 'set.pexelsKey.d': 'Free. Photos and videos.',
      'set.pixabayKey': 'Pixabay API key', 'set.pixabayKey.d': 'Free. Photos and videos.',
      'set.unsplashKey': 'Unsplash Access Key', 'set.unsplashKey.d': 'Free. Photos only.',
      'deps.title': 'Components (downloaded automatically)', 'deps.installed': 'Installed', 'deps.missing': 'Not present',
      'deps.downloading': 'Downloading… {pct}%', 'deps.error': 'Failed', 'deps.update': 'Update', 'deps.install': 'Install',
      'deps.ytdlp': 'Music from YouTube', 'deps.deno': 'Needed by yt-dlp for YouTube', 'deps.ffmpeg': 'Converts videos when the window can’t play MP4',
      'copy.done': 'Link copied', 'copy.failed': 'Couldn’t copy: {url}', 'copy.hint': 'Click to copy the link',
      'tab.remote': 'Remote',
      'upload.welcome': 'Welcome, {name}', 'upload.welcomeAnon': 'Welcome',
      'qr.missing': 'QR module missing. Run: pip install segno',
      'remote.title': 'Control this screen with your phone',
      'remote.lead': 'Scan this code and use your phone as a remote: switch tabs, open gallery items, flip through presentations and control the music.',
      'remote.s1': 'Scan the QR code', 'remote.s1d': 'The remote opens in your browser.',
      'remote.s2': 'Pick what goes on screen', 'remote.s2d': 'Tap a photo, video or document from the gallery.',
      'remote.s3': 'Control everything', 'remote.s3d': 'Next, previous, close, slideshow and music.',
      'remote.newLink': 'New remote link', 'remote.made': 'New remote link created. Old links no longer work.',
      'remote.valid': 'Valid until you make a new link',
      'remote.warn': 'Anyone with this link can control the screen. Make a new link to revoke old access.',
      'music.ytTitle': 'YouTube Music', 'music.ytText': 'Pick a Christmas list below or search a song. Music keeps playing in the background, also on other tabs.',
      'music.open': 'Open YouTube Music', 'music.noNative': 'Music only works in the app window (not in a regular browser).',
      'set.welcomeName': 'Name on the start screen', 'set.welcomeName.d': 'For the “Welcome, …” greeting.', 'set.welcomeName.ph': 'Name',
      'set.group.remote': 'Remote control', 'set.remoteRefreshMin': 'New remote link every', 'set.remoteRefreshMin.d': 'A new link makes old links invalid right away.',
      'tab.upload': 'Upload', 'tab.gallery': 'Gallery', 'tab.library': 'Library', 'tab.music': 'Music', 'tab.settings': 'Settings',

      'upload.title': 'Put it on the screen',
      'upload.lead': 'Scan the code with your phone and send photos, videos, presentations or links straight to this screen. No login needed.',
      'upload.s1': 'Scan the QR code', 'upload.s1d': 'With your phone or tablet camera.',
      'upload.s2': 'Choose what to show', 'upload.s2d': 'Files, a photo you take now, or a website link.',
      'upload.s3': 'Done', 'upload.s3d': 'It appears in the gallery on this screen right away.',
      'upload.newLink': 'Make a new link', 'upload.incoming': 'Receiving now',
      'upload.expiresIn': 'New code in {time}', 'upload.noExpiry': 'This code stays valid',
      'upload.failed': 'Failed', 'upload.received': 'Received',
      'chip.images': 'Photos', 'chip.videos': 'Videos', 'chip.audio': 'Audio', 'chip.links': 'Websites', 'chip.more': 'And more',

      'tunnel.starting': 'Preparing the upload link…', 'tunnel.downloading': 'First start: downloading the connection program…',
      'tunnel.error': 'Can’t reach the upload server. Retrying automatically.', 'tunnel.link': 'Creating QR code…',
      'net.offline': 'No Wi-Fi', 'net.back': 'Back online', 'net.offlineUpload': 'No internet connection. The QR code appears as soon as Wi-Fi is back.',

      'gallery.search': 'Search the gallery', 'gallery.all': 'All', 'gallery.image': 'Photos', 'gallery.video': 'Videos',
      'gallery.audio': 'Audio', 'gallery.doc': 'Documents', 'gallery.link': 'Links', 'gallery.slideshow': 'Slideshow',
      'gallery.empty': 'Nothing uploaded yet', 'gallery.emptySub': 'Scan the QR code on the Upload tab to put something on the screen.',
      'gallery.goUpload': 'Go to Upload', 'gallery.processing': 'This file is still being processed.',
      'gallery.delete': 'Delete', 'gallery.deleteConfirm': 'Delete this item?', 'gallery.deleted': 'Deleted',
      'gallery.newUpload': 'New: {name}', 'gallery.noSlides': 'No photos or videos for a slideshow.',

      'type.image': 'Photo', 'type.video': 'Video', 'type.audio': 'Audio', 'type.pdf': 'PDF', 'type.presentation': 'Presentation',
      'type.document': 'Document', 'type.text': 'Text', 'type.html': 'Web page', 'type.link': 'Link', 'type.other': 'File',

      'viewer.hint': 'Press any key to go back', 'viewer.hintSlides': 'Arrows to browse, any other key to stop',
      'viewer.close': 'Close', 'viewer.external': 'Open with app', 'viewer.present': 'Present in PowerPoint',
      'viewer.source': 'Source', 'viewer.loading': 'Loading…',
      'viewer.unsupported': 'This file can’t be shown here. Open it with the app on this PC.',
      'viewer.noPreview': 'No preview is available for this file type.',
      'viewer.noOffice': 'Install LibreOffice (free) to show presentations and documents here, or open them in PowerPoint.',
      'viewer.convertFailed': 'Conversion failed. Open the file with PowerPoint or Word.',

      'lib.search': 'Search stock photos and videos', 'lib.images': 'Photos', 'lib.videos': 'Videos',
      'lib.auto': 'Automatic source', 'lib.go': 'Search', 'lib.more': 'Load more',
      'lib.start': 'Search a topic or pick a suggestion. Click a result to view it, + to add it to the gallery.',
      'lib.empty': 'Nothing found. Try another word.',
      'lib.error': 'Search failed. Check the internet connection and try again.',
      'lib.needKey': 'Pexels needs a free API key. Add it in Settings.',
      'lib.via': 'Results from {source}. Click to view, + to add to the gallery.',
      'lib.add': 'Add to gallery', 'lib.adding': 'Downloading…', 'lib.added': 'Added to the gallery',
      'lib.addFailed': 'Couldn’t add it',

      'music.presets': 'Christmas music', 'music.nothing': 'Nothing playing', 'music.pick': 'Pick a Christmas list or search a song',
      'music.prev': 'Previous', 'music.next': 'Next', 'music.playPause': 'Play / pause', 'music.back': 'Back',
      'music.forward': 'Forward', 'music.home': 'YouTube Music home', 'music.search': 'Search a song or artist on YouTube',
      'music.loading': 'Loading {name}…',

      'set.group.general': 'General', 'set.group.upload': 'Uploading', 'set.group.display': 'Display', 'set.group.music': 'Music',
      'set.group.library': 'Library (stock photos and videos)', 'set.group.system': 'System',
      'set.language': 'Language', 'set.language.d': 'Language of the app and the upload page.',
      'set.theme': 'Theme', 'set.theme.d': 'Light or dark.', 'set.light': 'Light', 'set.dark': 'Dark',
      'set.accent': 'Accent colour', 'set.accent.d': 'Colour of buttons and the QR panel.', 'set.custom': 'Custom colour',
      'set.autoStart': 'Start with the PC', 'set.autoStart.d': 'Opens automatically when the computer starts.',
      'set.fullscreen': 'Full screen', 'set.fullscreen.d': 'Also with F11. Quit with Ctrl+Shift+Q.',
      'set.kiosk': 'Kiosk mode', 'set.kiosk.d': 'Locked full screen. Quit with Ctrl+Shift+Q.',
      'set.keepAwake': 'Keep screen awake', 'set.keepAwake.d': 'Stops the display from turning off.',
      'set.snow': 'Snow', 'set.snow.d': 'Falling snow across the screen.',
      'set.linkRefreshMin': 'New QR code every', 'set.linkRefreshMin.d': 'Old links keep working for 10 more minutes.',
      'set.maxUploadMB': 'Maximum file size', 'set.maxUploadMB.d': 'Per uploaded file.',
      'set.switchToGallery': 'Go to gallery after upload', 'set.switchToGallery.d': 'Shows the gallery as soon as something arrives.',
      'set.autoShowNew': 'Show new uploads right away', 'set.autoShowNew.d': 'Opens new photos and videos full screen.',
      'set.slideshowSec': 'Time per slide', 'set.slideshowSec.d': 'Videos always play to the end.',
      'set.docArrowNav': 'Browse with arrow keys', 'set.docArrowNav.d': 'In PDFs and presentations. Other keys close.',
      'set.idleReturnMin': 'Back to Upload after', 'set.idleReturnMin.d': 'Without activity the app returns to the QR code.',
      'set.pauseMusicOnVideo': 'Pause music for videos', 'set.pauseMusicOnVideo.d': 'Resumes automatically afterwards.',
      'set.musicAutoplay': 'Christmas music on start', 'set.musicAutoplay.d': 'Starts Christmas hits when the app opens.',
      'set.pexelsKey': 'Pexels API key', 'set.pexelsKey.d': 'Optional, free at pexels.com/api. Gives more and better results.',
      'set.pexelsKey.ph': 'Paste your key here',
      'set.minutes': '{n} min', 'set.seconds': '{n} sec', 'set.never': 'Never', 'set.off': 'Off', 'set.saved': 'Saved',
      'set.restartTunnel': 'Restart connection', 'set.openFolder': 'Open upload folder', 'set.toggleFs': 'Toggle full screen',
      'set.deleteAll': 'Delete everything', 'set.deleteAllQ': 'Delete all uploads?', 'set.deleteAllD': 'This can’t be undone.',
      'set.quit': 'Quit', 'set.quitQ': 'Quit Fembimmo Display?', 'set.linkMade': 'New QR code created',
      'set.storageUsed': 'used by {n} items', 'set.storageFree': 'free on disk', 'set.office': 'LibreOffice for presentations',
      'set.yes': 'Yes', 'set.no': 'No',
      'set.shortcuts': 'Shortcuts: F11 full screen, Ctrl+Shift+Q quit.',
      'set.version': 'Version {v}',
      'common.cancel': 'Cancel', 'common.delete': 'Delete',
    },
  },
  t(lang, key, vars) {
    const d = this.dict[lang] || this.dict.nl;
    let s = d[key] ?? this.dict.en[key] ?? key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(String(v));
    return s;
  },
};

</script>
<script>
/* Fembimmo Display — renderer. Made By Jack De Wandel */
(() => {
  'use strict';

  /* Backend bridge: HTTP + Server-Sent Events to the Python app */
  const api = (() => {
    const post = (path, body) => fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    }).then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      return j;
    });
    const get = (path) => fetch(path).then((r) => r.json());
    const handlers = {};
    let es = null;
    const source = () => {
      if (!es) {
        es = new EventSource('/api/events');
        let dropped = false;
        es.addEventListener('error', () => { dropped = true; });
        es.addEventListener('open', () => { if (dropped) { dropped = false; (handlers.__reconnect || []).forEach((cb) => cb()); } });
      }
      return es;
    };
    return {
      connect: () => { source(); },
      getState: () => get('/api/state'),
      setSetting: (key, value) => post('/api/settings', { key, value }).then((r) => r.settings),
      newLink: () => post('/api/link/new'),
      newRemote: () => post('/api/remote/new'),
      restartTunnel: () => post('/api/tunnel/restart'),
      deleteItem: (id) => post('/api/item/delete', { id }),
      deleteAll: () => post('/api/items/delete-all'),
      openExternal: (id) => post('/api/item/open-external', { id }),
      present: (id) => post('/api/item/present', { id }),
      librarySearch: (q) => post('/api/library/search', q),
      libraryImport: (r) => post('/api/library/import', r),
      storageInfo: () => get('/api/storage'),
      quit: () => post('/api/app/quit'),
      toggleFullscreen: () => post('/api/app/fullscreen'),
      openFolder: () => post('/api/app/open-folder'),
      openUrl: (url) => post('/api/app/open-url', { url }),
      libResolve: (r) => post('/api/library/resolve', r).then((x) => x.url),
      playExternal: (id) => post('/api/item/play-external', { id }),
      playUrl: (url) => post('/api/play-url', { url }).then((r) => r.ok).catch(() => false),
      webOpen: (url) => post('/api/web/open', { url }).then((r) => r.ok).catch(() => false),
      uiState: (st) => post('/api/ui-state', st).catch(() => {}),
      on: (ch, cb) => {
        if (ch === 'reconnect') { (handlers.__reconnect = handlers.__reconnect || []).push(cb); source(); return; }
        if (!handlers[ch]) {
          handlers[ch] = [];
          source().addEventListener(ch, (e) => {
            let data = null;
            try { data = JSON.parse(e.data); } catch { /* ignore */ }
            handlers[ch].forEach((fn) => fn(data));
          });
        }
        handlers[ch].push(cb);
      },
    };
  })();
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  }

  /* ---------------------------------------------------------------- */
  /* Icons (stroke icons, 24px grid)                                   */
  /* ---------------------------------------------------------------- */
  const P = {
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    video: '<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    audio: '<path d="M2 10v3"/><path d="M6 6v11"/><path d="M10 3v18"/><path d="M14 8v7"/><path d="M18 5v13"/><path d="M22 10v3"/>',
    doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M8 13h8"/><path d="M8 17h5"/>',
    slides: '<path d="M2 3h20"/><path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3"/><path d="m7 21 5-5 5 5"/>',
    link: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    play: '<path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z" fill="currentColor"/>',
    pause: '<rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/><rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/>',
    next: '<path d="M5 4.5v15l11-7.5z" fill="currentColor"/><path d="M19 5v14"/>',
    prev: '<path d="M19 4.5v15L8 12z" fill="currentColor"/><path d="M5 5v14"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M8 16H3v5"/>',
    wifiOff: '<path d="M12 20h.01"/><path d="M8.5 16.4a5 5 0 0 1 7 0"/><path d="M2 8.8a15 15 0 0 1 4.2-2.6"/><path d="M22 8.8A15 15 0 0 0 11 4.9"/><path d="M5 12.8a10 10 0 0 1 5.2-2.7"/><path d="M19 12.8a10 10 0 0 0-2.1-1.6"/><path d="m2 2 20 20"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9L9.6 3.9A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
    maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    volume: '<path d="M11 4.7a.7.7 0 0 0-1.2-.5L6.4 7.6A1.4 1.4 0 0 1 5.4 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.4a1.4 1.4 0 0 1 1 .4l3.4 3.4a.7.7 0 0 0 1.2-.5z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.4 18.4a9 9 0 0 0 0-12.8"/>',
    home: '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .7-1.5l7-6a2 2 0 0 1 2.6 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    back: '<path d="m15 18-6-6 6-6"/>',
    forward: '<path d="m9 18 6-6-6-6"/>',
    remote: '<rect x="6" y="2" width="12" height="20" rx="3"/><circle cx="12" cy="8" r="2.5"/><path d="M10 14h.01"/><path d="M14 14h.01"/><path d="M10 17.5h.01"/><path d="M14 17.5h.01"/>',
    shuffle: '<path d="m18 14 4 4-4 4"/><path d="m18 2 4 4-4 4"/><path d="M2 18h1.97a4 4 0 0 0 3.3-1.7l5.45-8.6a4 4 0 0 1 3.3-1.7H22"/><path d="M2 6h1.97a4 4 0 0 1 3.3 1.7l.53.83"/><path d="M15.56 16.3a4 4 0 0 0 1.25.7H22"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  };
  function icon(name, cls = 'ic') {
    const s = document.createElement('span');
    s.className = cls;
    s.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || P.file}</svg>`;
    return s;
  }
  const setIcon = (el, name) => { el.innerHTML = ''; el.append(icon(name)); };

  /* ---------------------------------------------------------------- */
  /* State                                                             */
  /* ---------------------------------------------------------------- */
  let S = {};
  let items = [];
  let session = {};
  let tunnel = {};
  let online = true;
  let rsession = {};
  let rtunnel = {};
  let appInfo = {};
  let currentTab = 'upload';
  const t = (k, vars) => window.I18N.t(S.language, k, vars);

  const GROUP = {
    image: 'image', video: 'video', audio: 'audio', pdf: 'doc', presentation: 'doc', document: 'doc',
    text: 'doc', other: 'doc', html: 'link', link: 'link',
  };
  const TYPE_ICON = {
    image: 'image', video: 'video', audio: 'audio', pdf: 'doc', presentation: 'slides', document: 'doc',
    text: 'doc', html: 'link', link: 'link', other: 'file',
  };
  const mediaUrl = (file) => '/media/' + encodeURIComponent(file);
  const CAN_H264 = (() => {
    try { return !!document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'); } catch { return true; }
  })();
  const videoSrc = (i) => (i.preview && i.type === 'video' ? '/media/previews/' + encodeURIComponent(i.preview) : mediaUrl(i.file));
  const previewUrl = (file) => '/media/previews/' + encodeURIComponent(file);

  /* ---------------------------------------------------------------- */
  /* Helpers                                                           */
  /* ---------------------------------------------------------------- */
  function fmtSize(n) {
    if (!n && n !== 0) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 ** 2) return Math.round(n / 1024) + ' KB';
    if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
    return (n / 1024 ** 3).toFixed(2) + ' GB';
  }
  function timeAgo(ts) {
    const rtf = new Intl.RelativeTimeFormat(S.language, { numeric: 'auto' });
    const s = (ts - Date.now()) / 1000;
    if (Math.abs(s) < 45) return rtf.format(0, 'second');
    const units = [['year', 31536000], ['month', 2592000], ['week', 604800], ['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [u, sec] of units) if (Math.abs(s) >= sec) return rtf.format(Math.round(s / sec), u);
    return rtf.format(0, 'second');
  }
  const fmtTime = (sec) => {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60);
    return `${m}:${String(sec % 60).padStart(2, '0')}`;
  };

  function toast(msg, kind = '') {
    const el = h('div', { class: 'toast ' + kind }, msg);
    $('#toasts').append(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 3800);
  }

  function confirmBox(title, text, okLabel) {
    return new Promise((resolve) => {
      const m = $('#modal');
      $('#modalTitle').textContent = title;
      $('#modalText').textContent = text || '';
      $('#modalOk').textContent = okLabel || t('common.delete');
      $('#modalCancel').textContent = t('common.cancel');
      m.classList.remove('hidden');
      const done = (v) => {
        m.classList.add('hidden');
        $('#modalOk').onclick = $('#modalCancel').onclick = null;
        document.removeEventListener('keydown', onKey);
        resolve(v);
      };
      const onKey = (e) => { if (e.key === 'Escape') done(false); if (e.key === 'Enter') done(true); };
      $('#modalOk').onclick = () => done(true);
      $('#modalCancel').onclick = () => done(false);
      document.addEventListener('keydown', onKey);
      $('#modalOk').focus();
    });
  }

  /* ---------------------------------------------------------------- */
  /* Theme, language, clock                                            */
  /* ---------------------------------------------------------------- */
  function applyTheme() {
    document.documentElement.dataset.theme = S.theme;
    const acc = S.accent || '#12424a';
    const mix = (hex, to, amt) => {
      const n = parseInt(hex.slice(1), 16);
      const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => Math.round(c + (to - c) * amt));
      return '#' + ch.map((c) => c.toString(16).padStart(2, '0')).join('');
    };
    const root = document.documentElement.style;
    root.setProperty('--accent', acc);
    root.setProperty('--accent-dark', mix(acc, 0, 0.3));
    root.setProperty('--accent-light', mix(acc, 255, 0.5));
  }

  function applyLanguage() {
    document.documentElement.lang = S.language;
    $$('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
    $$('[data-i18n-ph]').forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
    $$('[data-i18n-title]').forEach((el) => { el.title = t(el.dataset.i18nTitle); el.setAttribute('aria-label', t(el.dataset.i18nTitle)); });
    renderTypeChips();
    renderWelcome();
    renderSession();
    renderRemote();
    renderGallery(true);
    renderPresets();
    renderLibSuggest();
    renderSettings();
    renderNowPlaying();
  }

  function startClock() {
    const tick = () => {
      const d = new Date();
      const loc = S.language === 'en' ? 'en-GB' : 'nl-BE';
      $('#clock b').textContent = d.toLocaleTimeString(loc, { hour: '2-digit', minute: '2-digit' });
      $('#clock span').textContent = d.toLocaleDateString(loc, { weekday: 'long', day: 'numeric', month: 'long' });
    };
    tick();
    setInterval(tick, 1000);
  }

  function renderOnline() {
    $('#offline').classList.toggle('hidden', online);
    renderSession();
    renderRemote();
  }

  /* ---------------------------------------------------------------- */
  /* Tabs                                                              */
  /* ---------------------------------------------------------------- */
  function switchTab(name) {
    currentTab = name;
    $$('.tab').forEach((b) => {
      const on = b.dataset.tab === name;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on);
    });
    $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'settings') refreshStorage();
    reportUi();
  }

  /* ---------------------------------------------------------------- */
  /* Upload tab                                                        */
  /* ---------------------------------------------------------------- */
  function renderTypeChips() {
    const chips = [['doc', 'PDF'], ['image', t('chip.images')], ['video', t('chip.videos')], ['audio', t('chip.audio')],
      ['slides', 'PowerPoint'], ['link', t('chip.links')], ['file', t('chip.more')]];
    $('#typeChips').replaceChildren(...chips.map(([i, l]) => h('span', { class: 'chip' }, icon(i), l)));
  }

  function renderWelcome() {
    const name = String(S.welcomeName || '').trim();
    $('#welcome').textContent = name ? t('upload.welcome', { name }) : t('upload.welcomeAnon');
  }

  function tunnelMsg(st) {
    if (!online) return t('net.offlineUpload');
    if (st.status === 'downloading') return t('tunnel.downloading');
    if (st.status === 'error') return t('tunnel.error') + (st.error ? '\n\n' + st.error : '');
    if (st.status === 'online') return t('tunnel.link');
    const secs = st.since ? Math.max(0, Math.round((Date.now() - st.since) / 1000)) : 0;
    return t('tunnel.starting') + (secs >= 2 ? ` (${secs}s)` : '');
  }

  function renderRemote() {
    if (!S.language) return;
    const ready = rtunnel.status === 'online' && rsession.qr;
    $('#rqrImg').classList.toggle('hidden', !ready);
    $('#rqrWait').classList.toggle('hidden', !!ready);
    if (ready) {
      if ($('#rqrImg').src !== rsession.qr) $('#rqrImg').src = rsession.qr;
      const txt = String(rsession.shortUrl || rsession.url || '').replace(/^https?:\/\//, '');
      $('#rShortLink').textContent = txt;
      $('#rShortLink').classList.remove('blank');
      $('#rShortLink').classList.toggle('long', txt.length > 28);
      $('#rShortLink').title = t('copy.hint');
      $('#rExpiryText').textContent = rsession.expiresAt ? t('upload.expiresIn', { time: fmtTime((rsession.expiresAt - Date.now()) / 1000) }) : t('remote.valid');
    } else {
      $('#rShortLink').innerHTML = '&nbsp;';
      $('#rShortLink').classList.add('blank');
      $('#rExpiryText').textContent = '';
      $('#rqrWaitText').textContent = rtunnel.status === 'online' && rsession.qrAvailable === false ? t('qr.missing') : tunnelMsg(rtunnel);
    }
  }

  /* Click a link to copy it */
  async function nativeCopy(text) {
    try {
      const r = await fetch('/api/clipboard', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
      }).then((x) => x.json());
      return !!r.ok;
    } catch { return false; }
  }

  async function copyText(text) {
    if (!text) return false;
    if (appInfo.native && await nativeCopy(text)) return true;
    try {
      await Promise.race([navigator.clipboard.writeText(text),
        new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 800))]);
      return true;
    } catch { /* try next */ }
    try {
      const ta = h('textarea', { style: 'position:fixed;left:-9999px;top:0;opacity:0' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      if (ok) return true;
    } catch { /* try next */ }
    return appInfo.native ? false : nativeCopy(text);
  }

  function bindCopy(el, getUrl) {
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    const go = async () => {
      const url = getUrl();
      if (!url) return;
      const ok = await copyText(url);
      toast(ok ? t('copy.done') : t('copy.failed', { url }), ok ? 'ok' : 'bad');
      if (ok) { el.classList.add('copied'); setTimeout(() => el.classList.remove('copied'), 1400); }
    };
    el.addEventListener('click', go);
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  }

  function renderSession() {
    if (!S.language) return;
    const img = $('#qrImg');
    const ready = tunnel.status === 'online' && session.qr;
    img.classList.toggle('hidden', !ready);
    $('#qrWait').classList.toggle('hidden', !!ready);
    if (ready) {
      if (img.src !== session.qr) img.src = session.qr;
      const txt = String(session.shortUrl || session.url || '').replace(/^https?:\/\//, '');
      $('#shortLink').textContent = txt;
      $('#shortLink').classList.remove('blank');
      $('#shortLink').classList.toggle('long', txt.length > 28);
      $('#shortLink').title = t('copy.hint');
    } else {
      $('#shortLink').innerHTML = '&nbsp;';
      $('#shortLink').classList.add('blank');
      $('#qrWaitText').textContent = tunnel.status === 'online' && session.qrAvailable === false ? t('qr.missing') : tunnelMsg(tunnel);
    }
    renderExpiry();
  }

  function renderExpiry() {
    const bar = $('#expiryBar');
    const live = tunnel.status === 'online' && session.qr;
    if (!session.expiresAt || !live) {
      bar.style.width = live ? '100%' : '0%';
      $('#expiryText').textContent = live ? t('upload.noExpiry') : '';
      return;
    }
    const left = Math.max(0, session.expiresAt - Date.now());
    bar.style.width = (session.ttl ? (left / session.ttl) * 100 : 100) + '%';
    $('#expiryText').textContent = t('upload.expiresIn', { time: fmtTime(left / 1000) });
  }

  const incoming = new Map();
  function renderProgress(p) {
    let row = incoming.get(p.id);
    if (!row) {
      const li = h('li', {}, h('div', { class: 'in-top' }, h('span', { class: 'in-name' }, p.name), h('span', { class: 'in-pct' })),
        h('div', { class: 'in-bar' }, h('i')));
      $('#incomingList').prepend(li);
      row = { li };
      incoming.set(p.id, row);
    }
    const pct = p.total ? Math.round((p.received / p.total) * 100) : 100;
    row.li.querySelector('.in-bar i').style.width = pct + '%';
    row.li.querySelector('.in-pct').textContent = p.failed ? t('upload.failed') : p.done ? t('upload.received') : pct + '%';
    row.li.classList.toggle('done', !!p.done);
    row.li.classList.toggle('failed', !!p.failed);
    $('#incoming').classList.remove('hidden');
    if (p.done || p.failed) {
      setTimeout(() => {
        row.li.remove();
        incoming.delete(p.id);
        if (!incoming.size) $('#incoming').classList.add('hidden');
      }, 8000);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Gallery                                                           */
  /* ---------------------------------------------------------------- */
  let gFilter = 'all';
  let gQuery = '';
  const cardCache = new Map();
  let highlightId = null;
  let webOpenId = null;

  const sorted = () => [...items].sort((a, b) => b.createdAt - a.createdAt);
  function filteredItems() {
    return sorted().filter((i) => (gFilter === 'all' || GROUP[i.type] === gFilter)
      && (!gQuery || String(i.name || '').toLowerCase().includes(gQuery)));
  }

  // pdf.js is downloaded once by the app and loaded on demand (retries if it was offline).
  let pdfjsPromise = null;
  function loadPdfjs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (!pdfjsPromise) {
      pdfjsPromise = new Promise((resolve, reject) => {
        const sc = document.createElement('script');
        sc.src = '/vendor/pdf.min.js?' + Date.now();
        sc.onload = () => {
          if (!window.pdfjsLib) { pdfjsPromise = null; return reject(new Error('pdfjs')); }
          window.pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.js';
          resolve(window.pdfjsLib);
        };
        sc.onerror = () => { pdfjsPromise = null; sc.remove(); reject(new Error('pdfjs')); };
        document.head.append(sc);
      });
    }
    return pdfjsPromise;
  }

  // First page of PDFs (and converted PowerPoints) as thumbnails.
  const pdfThumbCache = new Map();
  let thumbQueue = Promise.resolve();
  function pdfThumb(url, img) {
    if (pdfThumbCache.has(url)) { img.src = pdfThumbCache.get(url); img.classList.add('loaded'); return; }
    thumbQueue = thumbQueue.then(async () => {
      try {
        const lib = await loadPdfjs();
        const doc = await lib.getDocument({ url }).promise;
        const page = await doc.getPage(1);
        const vp0 = page.getViewport({ scale: 1 });
        const vp = page.getViewport({ scale: 520 / vp0.width });
        const c = document.createElement('canvas');
        c.width = vp.width; c.height = vp.height;
        const ctx = c.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
        await page.render({ canvasContext: ctx, viewport: vp }).promise;
        const data = c.toDataURL('image/jpeg', 0.82);
        pdfThumbCache.set(url, data);
        img.src = data;
        img.classList.add('loaded');
        doc.destroy();
      } catch { /* keep icon */ }
    });
  }

  // One video at a time: grab a frame for the thumbnail, then release the connection.
  const posterCache = new Map();
  let posterQueue = Promise.resolve();
  function capturePoster(src, img) {
    if (posterCache.has(src)) { img.src = posterCache.get(src); return; }
    posterQueue = posterQueue.then(() => new Promise((resolve) => {
      const v = document.createElement('video');
      v.muted = true;
      v.preload = 'auto';
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        v.removeAttribute('src');
        v.load();
        resolve();
      };
      v.addEventListener('loadeddata', () => { try { v.currentTime = Math.min(1, (v.duration || 2) / 2); } catch { finish(); } });
      v.addEventListener('seeked', () => {
        try {
          const c = document.createElement('canvas');
          c.width = 480;
          c.height = Math.round(480 * (v.videoHeight || 9) / (v.videoWidth || 16));
          c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
          const d = c.toDataURL('image/jpeg', 0.8);
          posterCache.set(src, d);
          img.src = d;
        } catch { /* keep icon */ }
        finish();
      });
      v.addEventListener('error', finish);
      setTimeout(finish, 8000);
      v.src = src;
    }));
  }

  function itemDocUrl(i) {
    if (i.type === 'pdf') return mediaUrl(i.file);
    if (i.preview) return previewUrl(i.preview);
    return null;
  }

  function buildThumb(i) {
    const box = h('div', { class: 'thumb g-' + GROUP[i.type] });
    box.append(icon(TYPE_ICON[i.type], 'ic thumb-icon'));
    if (i.type === 'image' && i.file) {
      const img = h('img', { loading: 'lazy', alt: '', src: mediaUrl(i.file) });
      img.onload = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      box.append(img);
    } else if (i.type === 'video' && i.file) {
      const img = h('img', { alt: '', loading: 'lazy' });
      img.onload = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      if (i.poster) img.src = '/media/previews/' + encodeURIComponent(i.poster);
      else if (CAN_H264 || i.preview) capturePoster(videoSrc(i), img);
      box.append(img, h('span', { class: 'play-badge' }, icon('play')));
    } else if (i.type === 'link' && i.thumb) {
      const img = h('img', { loading: 'lazy', alt: '', src: i.thumb, class: i.thumbIsIcon ? 'fav' : '' });
      img.onload = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      box.append(img);
    } else if (itemDocUrl(i)) {
      const img = h('img', { alt: '', class: 'doc-page' });
      box.append(img);
      pdfThumb(itemDocUrl(i), img);
    }
    if (S.showLogo && (i.type === 'image' || i.type === 'video')) {
      const lg = h('img', { class: 'thumb-logo', alt: '', src: '/logo', loading: 'lazy' });
      lg.onerror = () => lg.remove();
      box.append(lg);
    }
    if (i.status === 'processing') box.append(h('div', { class: 'thumb-busy' }, h('div', { class: 'spinner sm' })));
    if (i.converting) box.append(h('span', { class: 'thumb-badge' }, h('span', { class: 'spinner sm' }), t('gallery.converting')));
    return box;
  }

  function cardSig(i) { return [i.status, i.name, i.type, i.thumb, i.preview, i.poster, i.file, i.converting].join('|'); }

  function buildCard(i) {
    const del = h('button', {
      class: 'icon-btn card-del', title: t('gallery.delete'), 'aria-label': t('gallery.delete'),
      onclick: async (e) => {
        e.stopPropagation();
        const latest = items.find((x) => x.id === i.id) || i;
        if (await confirmBox(t('gallery.deleteConfirm'), latest.name)) {
          await api.deleteItem(i.id);
          toast(t('gallery.deleted'));
        }
      },
    }, icon('trash'));
    const card = h('article', { class: 'card', tabindex: '0', 'data-id': i.id },
      buildThumb(i),
      h('div', { class: 'card-body' },
        h('div', { class: 'card-name' }, i.name || ''),
        h('div', { class: 'card-meta' })),
      del);
    card.addEventListener('click', () => openItem(i.id));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter') openItem(i.id); });
    return card;
  }

  function cardMeta(i) {
    const parts = [h('span', { class: 'type-tag' }, t('type.' + i.type))];
    parts.push(h('span', {}, timeAgo(i.createdAt)));
    if (i.size) parts.push(h('span', {}, fmtSize(i.size)));
    return parts;
  }

  function renderGallery(force = false) {
    if (!S.language) return;
    if (force) cardCache.clear();
    const list = filteredItems();
    const grid = $('#galleryGrid');
    const keep = new Set(list.map((i) => i.id));
    for (const [id, c] of cardCache) if (!keep.has(id)) { c.el.remove(); cardCache.delete(id); }
    const els = list.map((i) => {
      let c = cardCache.get(i.id);
      const sig = cardSig(i);
      if (!c || c.sig !== sig) {
        const el = buildCard(i);
        if (c) c.el.replaceWith(el);
        c = { el, sig };
        cardCache.set(i.id, c);
      }
      c.el.querySelector('.card-meta').replaceChildren(...cardMeta(i));
      c.el.classList.toggle('fresh', i.id === highlightId);
      return c.el;
    });
    if (force) grid.replaceChildren(...els);
    else els.forEach((el, idx) => { if (grid.children[idx] !== el) grid.insertBefore(el, grid.children[idx] || null); });
    $('#galleryEmpty').classList.toggle('hidden', list.length > 0);
    $('#galleryCount').textContent = items.length ? String(items.length) : '';
  }

  function onItemAdded(item) {
    toast(t('gallery.newUpload', { name: item.name }), 'ok');
    highlightId = item.id;
    setTimeout(() => { if (highlightId === item.id) { highlightId = null; renderGallery(); } }, 6000);
    if (S.switchToGallery && !V.open) {
      gFilter = 'all';
      gQuery = '';
      $('#gallerySearch').value = '';
      $$('#galleryFilter button').forEach((b) => b.classList.toggle('on', b.dataset.f === 'all'));
      switchTab('gallery');
      renderGallery();
      $('#galleryGrid').scrollTop = 0;
      $('#tab-gallery').scrollTop = 0;
    }
    if (S.autoShowNew && !V.open && ['image', 'video', 'pdf', 'link'].includes(item.type)) {
      setTimeout(() => openItem(item.id), 600);
    }
  }

  function openItem(id) {
    const i = items.find((x) => x.id === id);
    if (!i) return;
    if (i.status === 'processing') return toast(t('gallery.processing'));
    if (i.type === 'link' && appInfo.native) {
      api.webOpen(i.url).then((ok) => { if (!ok) openViewer(i); else { webOpenId = i.id; reportUi(); } });
      return;
    }
    openViewer(i);
  }

  /* ---------------------------------------------------------------- */
  /* Viewer (full screen, any key returns)                             */
  /* ---------------------------------------------------------------- */
  const V = { open: false, item: null, mode: null, pdf: null, page: 1, pages: 0, renderTask: null, slideshow: null, resumeMusic: false, uiTimer: null };

  function viewerButton(iconName, label, fn) {
    return h('button', { class: 'v-btn', onclick: (e) => { e.stopPropagation(); fn(); } }, icon(iconName), h('span', {}, label));
  }

  function openViewer(item) {
    if (!V.open) {
      V.open = true;
      const v = $('#viewer');
      v.classList.remove('hidden');
      requestAnimationFrame(() => v.classList.add('show'));
      if (document.activeElement) document.activeElement.blur();
    }
    renderViewerItem(item);
    reportUi();
    const hint = $('#viewerHint');
    hint.textContent = V.slideshow ? t('viewer.hintSlides') : t('viewer.hint');
    hint.classList.add('show');
    clearTimeout(V.hintTimer);
    V.hintTimer = setTimeout(() => hint.classList.remove('show'), 3200);
    pokeUi();
  }

  /* Blurred, colour-matched fill for the empty edges around photos and videos */
  function blurLayer() {
    const c = h('canvas', { class: 'v-blur', 'aria-hidden': 'true' });
    c.width = 64;
    c.height = Math.max(8, Math.round(64 * innerHeight / Math.max(1, innerWidth)));
    return c;
  }
  function paintBlur(c, src, sw, sh) {
    if (!sw || !sh || !c.isConnected) return;
    const ctx = c.getContext('2d');
    const k = Math.max(c.width / sw, c.height / sh);
    const w = sw * k;
    const hh = sh * k;
    try { ctx.drawImage(src, (c.width - w) / 2, (c.height - hh) / 2, w, hh); } catch { /* not ready */ }
  }

  /* Fembimmo logo in the top-right corner of the visible photo/video */
  function logoBadge() {
    const img = h('img', { class: 'v-logo', alt: 'Fembimmo', src: '/logo' });
    img.addEventListener('error', () => {
      if (!img.dataset.fb && appInfo.logoUrl) { img.dataset.fb = '1'; img.src = appInfo.logoUrl; } else img.remove();
    });
    return img;
  }
  function placeBadge(badge, nw, nh) {
    const st = $('#viewerStage');
    if (!badge || !badge.isConnected) return;
    const W = st.clientWidth;
    const H = st.clientHeight;
    let x = 0, y = 0, w = W, hh = H;
    if (S.viewerFit !== 'cover' && nw && nh) {
      const k = Math.min(W / nw, H / nh);
      w = nw * k; hh = nh * k; x = (W - w) / 2; y = (H - hh) / 2;
    }
    const size = Math.round(Math.min(Math.max(w * 0.12, Math.min(W, H) * 0.14, 90), w * 0.32, 280));
    const m = Math.round(Math.max(10, Math.min(w, hh) * 0.025));
    badge.style.width = size + 'px';
    badge.style.top = Math.round(y + m) + 'px';
    badge.style.right = Math.round(W - (x + w) + m) + 'px';
    badge.classList.toggle('under-ui', y + m < 80);
  }
  function attachBadge(st, media, getSize) {
    if (!S.showLogo) return;
    const b = logoBadge();
    st.append(b);
    const place = () => { const [nw, nh] = getSize(); placeBadge(b, nw, nh); };
    media.addEventListener(media.tagName === 'IMG' ? 'load' : 'loadedmetadata', place);
    V.placeBadge = place;
    place();
  }

  function clearStage() {
    const st = $('#viewerStage');
    st.querySelectorAll('video, audio').forEach((m) => { try { m.pause(); m.removeAttribute('src'); m.load(); } catch { /* ignore */ } });
    if (V.renderTask) { try { V.renderTask.cancel(); } catch { /* ignore */ } V.renderTask = null; }
    if (V.pdf) { try { V.pdf.destroy(); } catch { /* ignore */ } V.pdf = null; }
    if (V.pdfTask) { try { V.pdfTask.destroy(); } catch { /* ignore */ } V.pdfTask = null; }
    clearTimeout(V.slideTimer);
    clearTimeout(V.blurTimer);
    V.placeBadge = null;
    st.replaceChildren();
    V.mode = null;
    $('#viewerPager').classList.add('hidden');
  }

  function renderViewerItem(item) {
    clearStage();
    V.item = item;
    const st = $('#viewerStage');
    $('#viewerTitle').textContent = item.name || '';
    const actions = [];
    const src = item.remoteUrl || (item.file ? (item.type === 'video' ? videoSrc(item) : mediaUrl(item.file)) : null);

    if (item.libResult) actions.push(viewerButton('plus', t('lib.add'), () => importLib(item.libResult)));
    if (item.type === 'presentation' && !item.remoteUrl) actions.push(viewerButton('slides', t('viewer.present'), () => api.present(item.id)));
    if (item.id && !item.remoteUrl) actions.push(viewerButton('external', t('viewer.external'), () => api.openExternal(item.id)));
    if (item.pageUrl) actions.push(viewerButton('external', t('viewer.source'), () => api.openUrl(item.pageUrl)));
    actions.push(viewerButton('x', t('viewer.close'), closeViewer));
    $('#viewerActions').replaceChildren(...actions);

    const unsupported = (msg) => {
      V.mode = 'message';
      const box = h('div', { class: 'v-message' }, icon(TYPE_ICON[item.type] || 'file', 'ic v-msg-icon'),
        h('h2', {}, item.name || ''), h('p', {}, msg));
      const btns = h('div', { class: 'v-msg-actions' });
      if (item.type === 'presentation') btns.append(h('button', { class: 'btn accent', onclick: () => api.present(item.id) }, icon('slides'), t('viewer.present')));
      if (item.id && !item.remoteUrl) btns.append(h('button', { class: 'btn', onclick: () => api.openExternal(item.id) }, icon('external'), t('viewer.external')));
      box.append(btns);
      st.append(box);
    };

    switch (item.type) {
      case 'image': {
        V.mode = 'image';
        const img = h('img', { class: 'v-media' + (S.viewerFit === 'cover' ? ' cover' : ''), src, alt: '' });
        if (S.viewerFit === 'blur') {
          const bl = blurLayer();
          st.append(bl);
          img.addEventListener('load', () => paintBlur(bl, img, img.naturalWidth, img.naturalHeight));
        }
        img.onerror = () => {
          if (item.fallback && img.src !== item.fallback) { img.src = item.fallback; return; }
          if (!V.slideshow) unsupported(t('viewer.unsupported')); else slideStep(1);
        };
        st.append(img);
        attachBadge(st, img, () => [img.naturalWidth, img.naturalHeight]);
        if (V.slideshow) V.slideTimer = setTimeout(() => slideStep(1), Math.max(2, S.slideshowSec) * 1000);
        break;
      }
      case 'video': {
        V.mode = 'video';
        const needsH264 = !CAN_H264 && /\.(mp4|mov|m4v|3gp|avi|wmv|mkv)(\?|$)/i.test(item.remoteUrl || (item.preview ? '' : item.file || ''));
        let vsrc = src;
        if (needsH264) vsrc = item.remoteUrl ? '/api/live-url?u=' + encodeURIComponent(item.remoteUrl) : '/api/live/' + item.id + '.webm';
        const v = h('video', { class: 'v-media' + (S.viewerFit === 'cover' ? ' cover' : ''), src: vsrc, autoplay: true, playsinline: true, controls: !V.slideshow });
        const wait = h('div', { class: 'v-wait' }, h('div', { class: 'spinner' }), h('p', {}, t('viewer.preparing')));
        v.addEventListener('playing', () => wait.remove());
        v.addEventListener('ended', () => { if (needsH264 && !V.slideshow) v.controls = true; });
        v.addEventListener('play', async () => {
          if (S.pauseMusicOnVideo && (await musicPauseIfPlaying())) V.resumeMusic = true;
        });
        v.addEventListener('error', () => {
          wait.remove();
          if (V.slideshow) { slideStep(1); return; }
          unsupported(t('viewer.unsupported'));
        });
        if (V.slideshow) v.addEventListener('ended', () => slideStep(1));
        if (S.viewerFit === 'blur') {
          const bl = blurLayer();
          st.append(bl);
          const tick = () => {
            if (!v.isConnected) return;
            if (v.readyState >= 2) paintBlur(bl, v, v.videoWidth, v.videoHeight);
            V.blurTimer = setTimeout(tick, 120);
          };
          V.blurTimer = setTimeout(tick, 50);  // starts once the video is on the page
        }
        st.append(v, wait);
        attachBadge(st, v, () => [v.videoWidth, v.videoHeight]);
        break;
      }
      case 'audio': {
        V.mode = 'audio';
        const a = h('audio', { src, autoplay: true, controls: true });
        a.addEventListener('play', async () => {
          if (await musicPauseIfPlaying()) V.resumeMusic = true;
        });
        st.append(h('div', { class: 'v-audio' },
          h('div', { class: 'eq' }, h('i'), h('i'), h('i'), h('i'), h('i')),
          h('h2', {}, item.name || ''), a));
        break;
      }
      case 'pdf':
      case 'presentation':
      case 'document': {
        const url = itemDocUrl(item);
        if (url) openPdf(url);
        else unsupported(item.previewError === 'convert-failed' ? t('viewer.convertFailed') : t('viewer.noOffice'));
        break;
      }
      case 'text': {
        V.mode = 'text';
        const pre = h('pre', { class: 'v-text' }, t('viewer.loading'));
        st.append(pre);
        fetch(src).then((r) => r.text()).then((txt) => { pre.textContent = txt.slice(0, 400000); }).catch(() => { pre.textContent = '—'; });
        break;
      }
      case 'html': {
        V.mode = 'web';
        st.append(h('iframe', { class: 'v-web', src, sandbox: 'allow-scripts' }));
        break;
      }
      case 'link': {
        V.mode = 'web';
        st.append(h('iframe', { class: 'v-web', src: item.url, referrerpolicy: 'no-referrer' }));
        break;
      }
      default:
        unsupported(t('viewer.noPreview'));
    }
  }

  async function openPdf(url) {
    V.mode = 'pdf';
    const st = $('#viewerStage');
    const canvas = h('canvas', { class: 'v-pdf' });
    const spin = h('div', { class: 'spinner' });
    st.append(spin, canvas);
    const prevZone = h('div', { class: 'v-zone left', onclick: (e) => { e.stopPropagation(); pdfGo(V.page - 1); } });
    const nextZone = h('div', { class: 'v-zone right', onclick: (e) => { e.stopPropagation(); pdfGo(V.page + 1); } });
    st.append(prevZone, nextZone);
    try {
      const lib = await loadPdfjs();
      const task = lib.getDocument({ url });
      V.pdfTask = task;
      const pdf = await task.promise;
      if (V.pdfTask !== task) { pdf.destroy(); return; }
      V.pdf = pdf;
      V.pages = pdf.numPages;
      spin.remove();
      await pdfGo(1);
    } catch {
      spin.remove();
      canvas.remove();
      st.append(h('div', { class: 'v-message' }, h('p', {}, t('viewer.unsupported'))));
    }
  }

  async function pdfGo(n) {
    if (!V.pdf) return;
    n = Math.max(1, Math.min(V.pages, n));
    V.page = n;
    const canvas = $('#viewerStage canvas.v-pdf');
    if (!canvas) return;
    const page = await V.pdf.getPage(n);
    if (V.page !== n || !V.pdf) return;
    const st = $('#viewerStage');
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(st.clientWidth / base.width, st.clientHeight / base.height);
    const dpr = window.devicePixelRatio || 1;
    const vp = page.getViewport({ scale: scale * dpr });
    if (V.renderTask) { try { V.renderTask.cancel(); } catch { /* ignore */ } }
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    canvas.style.width = Math.floor(vp.width / dpr) + 'px';
    canvas.style.height = Math.floor(vp.height / dpr) + 'px';
    const task = page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
    V.renderTask = task;
    try { await task.promise; } catch { /* cancelled */ }
    const pager = $('#viewerPager');
    pager.textContent = `${n} / ${V.pages}`;
    pager.classList.toggle('hidden', V.pages < 2);
    reportUi();
  }

  function closeViewer() {
    if (!V.open) return;
    V.slideshow = null;
    clearStage();
    V.open = false;
    V.item = null;
    reportUi();
    const v = $('#viewer');
    v.classList.remove('show');
    setTimeout(() => { if (!V.open) v.classList.add('hidden'); }, 220);
    if (V.resumeMusic) { V.resumeMusic = false; musicCmd('play'); }
  }

  function viewerKey(k) {
    if (!V.open) return;
    const key = k.key;
    const nav = { ArrowRight: 1, ArrowDown: 1, PageDown: 1, ArrowLeft: -1, ArrowUp: -1, PageUp: -1 };
    if (V.mode === 'pdf' && S.docArrowNav && nav[key]) { pdfGo(V.page + nav[key]); return; }
    if (V.slideshow && (key === 'ArrowRight' || key === 'ArrowLeft')) { slideStep(key === 'ArrowRight' ? 1 : -1); return; }
    if (['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'Fn'].includes(key) && V.mode === 'pdf') return;
    closeViewer();
  }

  function pokeUi() {
    const v = $('#viewer');
    v.classList.add('ui');
    clearTimeout(V.uiTimer);
    V.uiTimer = setTimeout(() => v.classList.remove('ui'), 2600);
  }

  /* Slideshow */
  function startSlideshow() {
    const list = filteredItems().filter((i) => (i.type === 'image' || i.type === 'video') && i.status === 'ready');
    if (!list.length) return toast(t('gallery.noSlides'));
    V.slideshow = { list, i: 0 };
    openViewer(list[0]);
  }
  function slideStep(d) {
    if (!V.slideshow) return;
    const s = V.slideshow;
    s.i = (s.i + d + s.list.length) % s.list.length;
    const st = $('#viewerStage');
    st.classList.add('fade');
    setTimeout(() => {
      if (!V.slideshow) return;
      renderViewerItem(s.list[s.i]);
      st.classList.remove('fade');
      reportUi();
    }, 260);
  }

  /* ---------------------------------------------------------------- */
  /* Library                                                           */
  /* ---------------------------------------------------------------- */
  const L = { query: '', kind: 'image', source: 'auto', page: 1, results: [], loading: false, hasMore: false };
  const SUGGEST = [
    ['christmas', 'Kerstmis', 'Christmas'], ['snow', 'Sneeuw', 'Snow'], ['winter landscape', 'Winterlandschap', 'Winter landscape'],
    ['christmas lights', 'Kerstverlichting', 'Christmas lights'], ['fireworks', 'Vuurwerk', 'Fireworks'],
    ['house exterior', 'Woning', 'House'], ['living room interior', 'Interieur', 'Interior'], ['garden', 'Tuin', 'Garden'],
    ['belgium', 'België', 'Belgium'], ['nature', 'Natuur', 'Nature'],
  ];

  const PROVIDERS = [
    { id: 'pexels', key: 'pexelsKey', name: 'Pexels', url: 'https://www.pexels.com/api/', kinds: 'photos+videos' },
    { id: 'pixabay', key: 'pixabayKey', name: 'Pixabay', url: 'https://pixabay.com/api/docs/', kinds: 'photos+videos' },
    { id: 'unsplash', key: 'unsplashKey', name: 'Unsplash', url: 'https://unsplash.com/oauth/applications', kinds: 'photos' },
  ];
  const hasLibKey = () => PROVIDERS.some((p) => String(S[p.key] || '').trim());

  function providerCard(p) {
    const inp = h('input', { type: 'password', value: S[p.key] || '', placeholder: t('lib.pasteKey'), autocomplete: 'off', spellcheck: 'false' });
    const save = h('button', { class: 'btn accent', type: 'button' }, t('lib.saveKey'));
    save.addEventListener('click', async () => { await api.setSetting(p.key, inp.value.trim()); toast(t('set.saved'), 'ok'); });
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') save.click(); });
    return h('div', { class: 'lib-provider' + (S[p.key] ? ' ok' : '') },
      h('b', {}, p.name), h('span', {}, t('lib.kind.' + p.kinds)),
      inp,
      h('div', { class: 'row' }, save,
        h('button', { class: 'btn', type: 'button', onclick: () => api.openUrl(p.url) }, icon('external'), t('lib.getKey'))));
  }

  function renderLibSetup() {
    const box = $('#libSetup');
    const need = !hasLibKey();
    box.classList.toggle('hidden', !need);
    $('#libForm').classList.toggle('hidden', need);
    $('#libSuggest').classList.toggle('hidden', need);
    const used = PROVIDERS.filter((p) => S[p.key]);
    $('#libCredit').replaceChildren(...(used.length ? [t('lib.credit') + ' ', ...used.flatMap((p, i) => [
      h('a', { onclick: () => api.openUrl(p.url.replace(/\/api.*|\/oauth.*/, '/')) }, p.name), i < used.length - 1 ? ', ' : ''])] : []));
    if (need) {
      $('#libNote').textContent = '';
      $('#libGrid').replaceChildren();
      $('#libMore').classList.add('hidden');
      box.replaceChildren(h('h2', {}, t('lib.setupTitle')), h('p', {}, t('lib.setupText')),
        h('div', { class: 'lib-providers' }, ...PROVIDERS.map(providerCard)));
    }
  }

  function renderLibSuggest() {
    $('#libSuggest').replaceChildren(...SUGGEST.map(([q, nl, en]) => h('button', {
      class: 'chip', type: 'button',
      onclick: () => { $('#libQuery').value = S.language === 'en' ? en : nl; L.query = q; libSearch(true); },
    }, S.language === 'en' ? en : nl)));
    if (!L.results.length && !L.loading) $('#libNote').textContent = hasLibKey() ? t('lib.start') : '';
    renderLibSetup();
  }

  function libError(msg) {
    const m = String(msg).match(/(NO_KEYS|NO_KEY|INVALID_KEY|RATE_LIMIT|NO_VIDEO_SOURCE|NO_VIDEO)(?::(\w+))?/);
    const nm = { pexels: 'Pexels', pixabay: 'Pixabay', unsplash: 'Unsplash' }[m && m[2]] || (m && m[2]) || '';
    if (!m) return t('lib.error');
    if (m[1] === 'NO_KEYS') { renderLibSetup(); return ''; }
    return t('lib.err.' + m[1], { name: nm });
  }

  async function libSearch(reset) {
    if (!L.query || L.loading) return;
    if (reset) { L.page = 1; L.results = []; $('#libGrid').replaceChildren(); }
    L.loading = true;
    $('#libSpin').classList.remove('hidden');
    $('#libMore').classList.add('hidden');
    $('#libNote').textContent = '';
    try {
      const r = await api.librarySearch({ query: L.query, kind: L.kind, source: L.source, page: L.page });
      L.hasMore = r.hasMore;
      L.results.push(...r.results);
      $('#libGrid').append(...r.results.map(libCard));
      const names = { pexels: 'Pexels', pixabay: 'Pixabay', unsplash: 'Unsplash' };
      $('#libNote').textContent = L.results.length
        ? t('lib.via', { source: String(r.source || '').split('+').map((x) => names[x] || x).join(' + ') })
        : t('lib.empty');
      if (r.errors && r.errors.length) toast(libError(r.errors[0]), 'bad');
    } catch (e) {
      $('#libNote').textContent = libError(String((e && e.message) || ''));
    } finally {
      L.loading = false;
      $('#libSpin').classList.add('hidden');
      $('#libMore').classList.toggle('hidden', !L.hasMore);
    }
  }

  function libCard(r) {
    const thumb = h('div', { class: 'thumb g-' + r.kind });
    thumb.append(icon(r.kind === 'video' ? 'video' : 'image', 'ic thumb-icon'));
    if (r.thumb) {
      const img = h('img', { loading: 'lazy', alt: '', src: r.thumb, referrerpolicy: 'no-referrer' });
      img.onload = () => img.classList.add('loaded');
      img.onerror = () => img.remove();
      thumb.append(img);
    }
    if (r.kind === 'video') {
      thumb.append(h('span', { class: 'play-badge' }, icon('play')));
      let pv = null;
      thumb.addEventListener('mouseenter', () => {
        if (!CAN_H264) return;
        pv = h('video', { src: r.preview || r.url, autoplay: true, loop: true, playsinline: true, class: 'loaded hover-preview' });
        pv.muted = true;
        thumb.append(pv);
      });
      thumb.addEventListener('mouseleave', () => { if (pv) { pv.pause(); pv.remove(); pv = null; } });
    }
    const add = h('button', { class: 'icon-btn card-add', title: t('lib.add'), 'aria-label': t('lib.add') }, icon('plus'));
    add.addEventListener('click', (e) => { e.stopPropagation(); importLib(r, add); });
    const meta = [r.creator, r.license].filter(Boolean).join(', ');
    const card = h('article', { class: 'card', tabindex: '0' }, thumb,
      h('div', { class: 'card-body' }, h('div', { class: 'card-name' }, r.title || ''), h('div', { class: 'card-meta' }, h('span', {}, meta || r.sourceName))),
      add);
    card.addEventListener('click', async () => {
      let url = r.url;
      if (!url && r.resolve) {
        toast(t('viewer.loading'));
        try { url = r.url = await api.libResolve(r); } catch { toast(t('lib.error'), 'bad'); return; }
      }
      openViewer({ type: r.kind, name: r.title, remoteUrl: url, fallback: r.fallback, pageUrl: r.pageUrl, libResult: r });
    });
    return card;
  }

  const importing = new Set();
  async function importLib(r, btn) {
    if (importing.has(r.id)) return;
    importing.add(r.id);
    if (btn) { btn.disabled = true; btn.replaceChildren(h('div', { class: 'spinner sm' })); }
    toast(t('lib.adding'));
    try {
      await api.libraryImport(r);
      toast(t('lib.added'), 'ok');
      if (btn) { btn.replaceChildren(icon('check')); btn.classList.add('done'); }
    } catch {
      toast(t('lib.addFailed'), 'bad');
      if (btn) { btn.disabled = false; btn.replaceChildren(icon('plus')); }
      importing.delete(r.id);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Music: YouTube via yt-dlp, played here with an <audio> element    */
  /* ---------------------------------------------------------------- */
  const PRESETS = [
    { q: 'christmas hits playlist', nl: 'Kersthits', en: 'Christmas hits', d: 'Mariah, Wham!, Michael Bublé…' },
    { q: 'classic christmas songs', nl: 'Klassieke kerst', en: 'Classic Christmas', d: 'Bing Crosby, Nat King Cole…' },
    { q: 'nederlandstalige kerstliedjes', nl: 'Nederlandstalige kerst', en: 'Dutch Christmas', d: 'Kerst in het Nederlands' },
    { q: 'christmas jazz', nl: 'Kerstjazz', en: 'Christmas jazz', d: 'Rustig en sfeervol' },
    { q: 'christmas lofi', nl: 'Kerst lo-fi', en: 'Christmas lo-fi', d: 'Achtergrondmuziek' },
    { q: 'acoustic christmas songs', nl: 'Akoestische kerst', en: 'Acoustic Christmas', d: 'Gitaar en piano' },
    { q: 'instrumental christmas piano', nl: 'Kerstpiano', en: 'Christmas piano', d: 'Instrumentaal' },
    { q: 'christmas carols choir', nl: 'Kerstkoren', en: 'Christmas carols', d: 'Koren en carols' },
    { q: 'christmas songs for kids', nl: 'Kerst voor kinderen', en: 'Christmas for kids', d: 'Rudolph, Frosty…' },
  ];
  const audio = new Audio();
  audio.preload = 'auto';
  const M = { queue: [], idx: -1, results: [], resultsTitle: '', errors: 0, loading: false };
  let ytStatus = { available: true };
  let np = null;

  const cur = () => (M.idx >= 0 ? M.queue[M.idx] : null);

  function computeNp() {
    const tr = cur();
    np = tr ? {
      title: tr.title, artist: tr.artist, art: tr.thumb, paused: audio.paused,
      time: audio.currentTime || 0, dur: (isFinite(audio.duration) && audio.duration) || tr.duration || 0,
      vol: audio.volume, shuffle: !!S.musicShuffle,
    } : null;
    return np;
  }

  let npReportTimer = null;
  function reportNp(now) {
    clearTimeout(npReportTimer);
    npReportTimer = setTimeout(() => api.uiState({ np: computeNp() }), now ? 0 : 400);
  }

  function renderYtStatus() {
    const box = $('#ytStatus');
    let msg = '';
    if (ytStatus.installing) msg = t('music.installing');
    else if (!ytStatus.available) msg = ytStatus.error ? t('music.installFailed', { err: ytStatus.error }) : t('music.missing');
    else if (!ytStatus.jsRuntime) msg = t('music.noJs');
    box.classList.toggle('hidden', !msg);
    $('#ytStatusText').textContent = msg;
    const canInstall = ytStatus.canInstall !== false && !ytStatus.installing && (!ytStatus.available || ytStatus.error || !ytStatus.jsRuntime);
    $('#ytInstall').classList.toggle('hidden', !canInstall);
  }

  async function ytSearch(q, title) {
    if (!q) return [];
    if (!ytStatus.available) { renderYtStatus(); toast(t('music.missing'), 'bad'); return []; }
    M.loading = true;
    $('#ytSpin').classList.remove('hidden');
    try {
      const r = await fetch('/api/yt/search', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ q, n: 30 }),
      }).then((x) => x.json());
      if (r.error) throw new Error(r.error);
      M.results = r.results || [];
      M.resultsTitle = title || q;
      renderTracks();
      if (!M.results.length) toast(t('music.noResults'));
      return M.results;
    } catch (e) {
      toast(t('music.searchFailed'), 'bad');
      return [];
    } finally {
      M.loading = false;
      $('#ytSpin').classList.add('hidden');
    }
  }

  function setQueue(list, start, shuffle) {
    M.queue = list.slice();
    if (shuffle) {
      for (let i = M.queue.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [M.queue[i], M.queue[j]] = [M.queue[j], M.queue[i]];
      }
      start = 0;
    }
    playIndex(start || 0);
  }

  function playIndex(i) {
    if (!M.queue.length) return;
    M.idx = (i + M.queue.length) % M.queue.length;
    const tr = cur();
    audio.src = '/api/yt/stream/' + tr.id;
    audio.play().catch(() => {});
    const nxt = M.queue[(M.idx + 1) % M.queue.length];
    if (nxt) fetch('/api/yt/prefetch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: nxt.id }) }).catch(() => {});
    renderNowPlaying();
    renderTracks();
    reportNp(true);
  }

  function musicNext() {
    if (!M.queue.length) return;
    if (S.musicShuffle && M.queue.length > 2) {
      let j = M.idx;
      while (j === M.idx) j = Math.floor(Math.random() * M.queue.length);
      playIndex(j);
    } else playIndex(M.idx + 1);
  }
  function musicPrev() {
    if (!M.queue.length) return;
    if (audio.currentTime > 5) { audio.currentTime = 0; return; }
    playIndex(M.idx - 1);
  }
  function musicToggle() {
    if (!cur()) { playPreset(0); return; }
    if (audio.paused) audio.play().catch(() => {}); else audio.pause();
  }
  function musicStop() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    M.idx = -1;
    renderNowPlaying();
    renderTracks();
    reportNp(true);
  }

  function musicCmd(cmd, arg) {
    if (cmd === 'toggle') musicToggle();
    else if (cmd === 'play') { if (cur() && audio.paused) audio.play().catch(() => {}); }
    else if (cmd === 'pause') audio.pause();
    else if (cmd === 'next') musicNext();
    else if (cmd === 'prev') musicPrev();
    else if (cmd === 'stop') musicStop();
    else if (cmd === 'volume') setVolume(Number(arg));
    else if (cmd === 'seek') { if (isFinite(audio.duration)) audio.currentTime = Math.max(0, Number(arg) || 0); }
    else if (cmd === 'preset') playPreset(Number(arg) || 0);
    else if (cmd === 'shuffle') api.setSetting('musicShuffle', arg == null ? !S.musicShuffle : !!arg);
    else if (cmd === 'search') searchAndPlay(String(arg || ''));
    return Promise.resolve(true);
  }

  async function musicPauseIfPlaying() {
    if (!cur() || audio.paused) return false;
    audio.pause();
    return true;
  }

  function setVolume(v) {
    v = Math.max(0, Math.min(1, isFinite(v) ? v : 0.8));
    audio.volume = v;
    $('#npVol').value = Math.round(v * 100);
    clearTimeout(setVolume.t);
    setVolume.t = setTimeout(() => api.setSetting('musicVolume', Math.round(v * 100)), 600);
    reportNp();
  }

  async function playPreset(i) {
    const p = PRESETS[Math.max(0, Math.min(PRESETS.length - 1, i))];
    const name = S.language === 'en' ? p.en : p.nl;
    toast(t('music.loading', { name }));
    const list = await ytSearch(p.q, name);
    if (list.length) setQueue(list, 0, !!S.musicShuffle);
  }

  async function searchAndPlay(q) {
    q = q.trim();
    if (!q) return;
    $('#ytQuery').value = q;
    toast(t('music.loading', { name: q }));
    const list = await ytSearch(q, q);
    if (list.length) setQueue(list, 0, false);
  }

  function renderTracks() {
    if (!S.language) return;
    $('#resultsTitle').textContent = M.results.length ? M.resultsTitle : '';
    $('#resultsActions').classList.toggle('hidden', !M.results.length);
    const playingId = cur() ? cur().id : null;
    $('#tracks').replaceChildren(...M.results.map((tr, i) => {
      const li = h('li', { class: 'track' + (tr.id === playingId ? ' playing' : '') },
        h('img', { src: tr.thumb, alt: '', loading: 'lazy' }),
        h('div', {}, h('b', {}, tr.title), h('span', {}, tr.artist)),
        h('span', { class: 'dur' }, tr.duration ? fmtTime(tr.duration) : ''));
      li.addEventListener('click', () => setQueue(M.results, i, false));
      return li;
    }));
  }

  function renderNowPlaying() {
    if (!S.language) return;
    computeNp();
    const has = !!np;
    $('#mini').classList.toggle('hidden', !has);
    setIcon($('#miniPlay'), has && !np.paused ? 'pause' : 'play');
    setIcon($('#npPlay'), has && !np.paused ? 'pause' : 'play');
    $('#npTitle').textContent = has ? np.title : t('music.nothing');
    $('#npArtist').textContent = has ? np.artist : t('music.pick');
    $('#miniTitle').textContent = has ? np.title : '';
    $('#miniArtist').textContent = has ? np.artist : '';
    for (const id of ['#npArt', '#miniArt']) {
      const img = $(id);
      if (has && np.art) { if (img.getAttribute('src') !== np.art) img.src = np.art; img.classList.remove('hidden'); }
      else img.classList.add('hidden');
    }
    $('#npCur').textContent = fmtTime(has ? np.time : 0);
    $('#npDur').textContent = fmtTime(has ? np.dur : 0);
    $('#npProgress i').style.width = (has && np.dur ? Math.min(100, (np.time / np.dur) * 100) : 0) + '%';
    $('#npShuffle').classList.toggle('on', !!S.musicShuffle);
  }

  function renderPresets() {
    $('#presets').replaceChildren(...PRESETS.map((p, i) => h('button', { class: 'preset', onclick: () => playPreset(i) },
      h('b', {}, S.language === 'en' ? p.en : p.nl), h('span', {}, p.d))));
    renderYtStatus();
    renderTracks();
  }

  function bindMusic() {
    audio.volume = Math.max(0, Math.min(1, (S.musicVolume ?? 80) / 100));
    $('#npVol').value = Math.round(audio.volume * 100);
    let lastTick = 0;
    audio.addEventListener('timeupdate', () => {
      const now = Date.now();
      if (now - lastTick > 900) { lastTick = now; renderNowPlaying(); reportNp(); }
    });
    for (const ev of ['play', 'pause', 'loadedmetadata']) audio.addEventListener(ev, () => { renderNowPlaying(); reportNp(true); });
    audio.addEventListener('playing', () => { M.errors = 0; });
    audio.addEventListener('ended', musicNext);
    audio.addEventListener('error', () => {
      if (!audio.getAttribute('src')) return;
      M.errors += 1;
      toast(t('music.trackFailed'), 'bad');
      if (M.errors < 5 && M.queue.length > 1) setTimeout(musicNext, 800);
      else { M.errors = 0; musicStop(); }
    });

    $$('[data-music]').forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); musicCmd(b.dataset.music); }));
    $('#mini').addEventListener('click', () => switchTab('music'));
    $('#npVol').addEventListener('input', (e) => setVolume(Number(e.target.value) / 100));
    $('#npProgress').addEventListener('click', (e) => {
      if (!np || !np.dur) return;
      const r = e.currentTarget.getBoundingClientRect();
      musicCmd('seek', ((e.clientX - r.left) / r.width) * np.dur);
    });
    $('#npShuffle').addEventListener('click', () => musicCmd('shuffle'));
    $('#npStop').addEventListener('click', musicStop);
    $('#ytSearch').addEventListener('submit', (e) => {
      e.preventDefault();
      const q = $('#ytQuery').value.trim();
      if (q) ytSearch(q, q);
    });
    $('#btnPlayAll').addEventListener('click', () => setQueue(M.results, 0, false));
    $('#btnShuffleAll').addEventListener('click', () => setQueue(M.results, 0, true));
    $('#ytInstall').addEventListener('click', async () => {
      await fetch('/api/yt/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upgrade: ytStatus.available }) });
      ytStatus = { ...ytStatus, installing: true };
      renderYtStatus();
    });
  }

  /* ---------------------------------------------------------------- */
  /* Remote control                                                    */
  /* ---------------------------------------------------------------- */
  let uiTimer = null;
  function reportUi() {
    clearTimeout(uiTimer);
    uiTimer = setTimeout(() => {
      let viewer = null;
      if (V.open && V.item) {
        viewer = {
          name: V.item.name || '', type: V.item.type, id: V.item.id || null, slideshow: !!V.slideshow,
          page: V.mode === 'pdf' ? V.page : null, pages: V.mode === 'pdf' ? V.pages : null,
        };
      } else if (webOpenId) {
        viewer = { name: (items.find((x) => x.id === webOpenId) || {}).name || '', type: 'link', id: webOpenId, web: true };
      }
      api.uiState({ tab: currentTab, viewer });
    }, 120);
  }

  function stepItem(d) {
    const list = filteredItems().filter((i) => i.status === 'ready');
    if (!list.length) return;
    const curId = (V.item && V.item.id) || webOpenId;
    const cur = curId ? list.findIndex((i) => i.id === curId) : -1;
    const next = list[(cur + d + list.length) % list.length];
    if (V.open && next.type === 'link' && appInfo.native) closeViewer();
    openItem(next.id);
  }

  function onRemote(m) {
    if (!m) return;
    lastInput = Date.now();
    const { cmd, arg } = m;
    if (cmd.startsWith('music.')) { musicCmd(cmd.slice(6), arg); return; }
    if (cmd === 'tab') { if (V.open) closeViewer(); switchTab(String(arg)); }
    else if (cmd === 'open') { if (V.open) closeViewer(); openItem(String(arg)); }
    else if (cmd === 'viewer.close') closeViewer();
    else if (cmd === 'viewer.next' || cmd === 'viewer.prev') {
      const d = cmd === 'viewer.next' ? 1 : -1;
      if (V.open && V.mode === 'pdf' && ((d > 0 && V.page < V.pages) || (d < 0 && V.page > 1))) pdfGo(V.page + d);
      else if (V.slideshow) slideStep(d);
      else stepItem(d);
    } else if (cmd === 'slideshow') { if (V.open) closeViewer(); switchTab('gallery'); startSlideshow(); }
    else if (cmd === 'gallery.filter') {
      gFilter = String(arg || 'all');
      $$('#galleryFilter button').forEach((b) => b.classList.toggle('on', b.dataset.f === gFilter));
      switchTab('gallery');
      renderGallery();
    }
  }

  /* ---------------------------------------------------------------- */
  /* Settings                                                          */
  /* ---------------------------------------------------------------- */
  const SCHEMA = [
    { group: 'general', items: [
      { key: 'language', type: 'select', options: [['nl', 'Nederlands'], ['en', 'English']] },
      { key: 'welcomeName', type: 'text' },
      { key: 'theme', type: 'select', options: [['light', 'set.light'], ['dark', 'set.dark']] },
      { key: 'accent', type: 'color' },
      { key: 'autoStart', type: 'toggle' },
      { key: 'fullscreen', type: 'toggle' },
      { key: 'keepAwake', type: 'toggle' },
      { key: 'snow', type: 'toggle' },
    ] },
    { group: 'upload', items: [
      { key: 'linkRefreshMin', type: 'select', options: [[5], [10], [15], [30], [60], [0, 'set.never']], unit: 'min' },
      { key: 'maxUploadMB', type: 'select', options: [[100], [500], [1024], [4096], [10240]], unit: 'size' },
      { key: 'switchToGallery', type: 'toggle' },
      { key: 'autoShowNew', type: 'toggle' },
    ] },
    { group: 'display', items: [
      { key: 'slideshowSec', type: 'select', options: [[5], [8], [12], [20], [30], [60]], unit: 'sec' },
      { key: 'viewerFit', type: 'select', options: [['blur', 'set.fit.blur'], ['contain', 'set.fit.contain'], ['cover', 'set.fit.cover']] },
      { key: 'showLogo', type: 'toggle' },
      { key: 'docArrowNav', type: 'toggle' },
      { key: 'idleReturnMin', type: 'select', options: [[0, 'set.off'], [2], [5], [10], [30]], unit: 'min' },
    ] },
    { group: 'music', items: [
      { key: 'pauseMusicOnVideo', type: 'toggle' },
      { key: 'musicAutoplay', type: 'toggle' },
      { key: 'musicShuffle', type: 'toggle' },
    ] },
    { group: 'library', items: [
      { key: 'pexelsKey', type: 'key', url: 'https://www.pexels.com/api/' },
      { key: 'pixabayKey', type: 'key', url: 'https://pixabay.com/api/docs/' },
      { key: 'unsplashKey', type: 'key', url: 'https://unsplash.com/oauth/applications' },
    ] },
    { group: 'remote', items: [
      { key: 'remoteRefreshMin', type: 'select', options: [[0, 'set.never'], [15], [60], [240]], unit: 'min' },
    ] },
  ];
  const SWATCHES = ['#12424a', '#1d4ed8', '#7a1f2b', '#3f5f2a', '#6b4c9a', '#8a5a14', '#1f2937'];

  function optLabel(o, unit) {
    if (o[1]) return o[1].startsWith('set.') ? t(o[1]) : o[1];
    const v = o[0];
    if (unit === 'min') return t('set.minutes', { n: v });
    if (unit === 'sec') return t('set.seconds', { n: v });
    if (unit === 'size') return v >= 1024 ? (v / 1024) + ' GB' : v + ' MB';
    return String(v);
  }

  function settingControl(def) {
    const v = S[def.key];
    const set = (val) => api.setSetting(def.key, val);
    if (def.type === 'toggle') {
      const b = h('button', { class: 'switch' + (v ? ' on' : ''), role: 'switch', 'aria-checked': String(!!v), 'aria-label': t('set.' + def.key) });
      b.addEventListener('click', () => set(!S[def.key]));
      return b;
    }
    if (def.type === 'select') {
      const sel = h('select', { 'aria-label': t('set.' + def.key) }, ...def.options.map((o) =>
        h('option', { value: String(o[0]), selected: String(o[0]) === String(v) }, optLabel(o, def.unit))));
      sel.addEventListener('change', () => set(typeof v === 'number' ? Number(sel.value) : sel.value));
      return sel;
    }
    if (def.type === 'color') {
      const wrap = h('div', { class: 'swatches' });
      for (const c of SWATCHES) {
        const b = h('button', { class: 'swatch' + (c === v ? ' on' : ''), style: `--c:${c}`, 'aria-label': c });
        b.addEventListener('click', () => set(c));
        wrap.append(b);
      }
      const custom = h('input', { type: 'color', value: v, 'aria-label': t('set.custom') });
      custom.addEventListener('change', () => set(custom.value));
      wrap.append(custom);
      return wrap;
    }
    if (def.type === 'key') {
      const k = h('input', { type: 'password', value: v || '', placeholder: t('lib.pasteKey'), autocomplete: 'off', class: 'text-input', spellcheck: 'false' });
      k.addEventListener('change', () => { set(k.value.trim()); toast(t('set.saved'), 'ok'); });
      return h('div', { class: 'key-row' }, k, h('button', { class: 'btn', onclick: () => api.openUrl(def.url) }, icon('external'), t('lib.getKey')));
    }
    const inp = h('input', { type: def.secret ? 'password' : 'text', value: v || '', placeholder: t('set.' + def.key + '.ph'), autocomplete: 'off', class: 'text-input' });
    inp.addEventListener('change', () => { set(inp.value.trim()); toast(t('set.saved'), 'ok'); });
    return inp;
  }

  function renderSettings() {
    if (!S.language) return;
    const root = $('#settingsRoot');
    const groups = SCHEMA.map((g) => h('section', { class: 'set-card' },
      h('h2', {}, t('set.group.' + g.group)),
      ...g.items.map((def) => h('div', { class: 'set-row' },
        h('div', { class: 'set-label' }, h('b', {}, t('set.' + def.key)), h('span', {}, t('set.' + def.key + '.d'))),
        settingControl(def)))));

    const sysBtn = (ic, label, fn, cls = '') => h('button', { class: 'btn ' + cls, onclick: fn }, icon(ic), label);
    const system = h('section', { class: 'set-card' },
      h('h2', {}, t('set.group.system')),
      h('div', { class: 'storage', id: 'storageInfo' }, t('viewer.loading')),
      h('div', { class: 'sys-btns' },
        sysBtn('refresh', t('upload.newLink'), async () => { await api.newLink(); toast(t('set.linkMade'), 'ok'); }),
        sysBtn('remote', t('remote.newLink'), async () => { await api.newRemote(); toast(t('remote.made'), 'ok'); }),
        sysBtn('refresh', t('set.restartTunnel'), async () => { toast(t('tunnel.starting')); await api.restartTunnel(); }),
        sysBtn('folder', t('set.openFolder'), () => api.openFolder()),
        sysBtn('download', t('music.update'), async () => {
          await fetch('/api/yt/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ upgrade: true }) });
          toast(t('music.installing'));
        }),
        sysBtn('maximize', t('set.toggleFs'), () => api.toggleFullscreen()),
        sysBtn('trash', t('set.deleteAll'), async () => {
          if (await confirmBox(t('set.deleteAllQ'), t('set.deleteAllD'))) { await api.deleteAll(); toast(t('gallery.deleted')); refreshStorage(); }
        }, 'danger-ghost'),
        sysBtn('power', t('set.quit'), async () => { if (await confirmBox(t('set.quitQ'), '', t('set.quit'))) api.quit(); }, 'danger-ghost')),
      h('h3', { class: 'deps-title' }, t('deps.title')),
      h('div', { class: 'deps', id: 'depsList' }),
      h('p', { class: 'shortcuts' }, t('set.shortcuts')));

    const about = h('section', { class: 'set-card about' },
      h('div', { class: 'about-logo' }, h('img', { src: '/logo', alt: 'Fembimmo' })),
      h('p', { class: 'made-by' }, 'Made By Jack De Wandel'),
      h('p', { class: 'version' }, t('set.version', { v: appInfo.version || '1.0.0' })));

    root.replaceChildren(about, ...groups, system);
    refreshStorage();
    renderDeps();
  }

  let depsState = {};
  const DEP_INFO = [
    ['yt-dlp', 'deps.ytdlp'], ['deno', 'deps.deno'], ['ffmpeg', 'deps.ffmpeg'],
  ];
  function renderDeps() {
    const box = $('#depsList');
    if (!box) return;
    box.replaceChildren(...DEP_INFO.map(([name, desc]) => {
      const st = depsState[name] || {};
      let label = st.installed ? t('deps.installed') : t('deps.missing');
      if (st.status === 'downloading') label = t('deps.downloading', { pct: Math.round((st.progress || 0) * 100) });
      else if (st.status === 'error') label = t('deps.error');
      const btn = h('button', { class: 'btn', disabled: st.status === 'downloading' }, icon('download'),
        st.installed ? t('deps.update') : t('deps.install'));
      btn.addEventListener('click', () => fetch('/api/deps/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
      }));
      return h('div', { class: 'dep-row' + (st.status === 'error' ? ' bad' : st.installed ? ' ok' : '') },
        h('div', {}, h('b', {}, name), h('span', {}, t(desc))),
        h('span', { class: 'dep-state', title: st.error || '' }, label), btn);
    }));
  }

  async function refreshStorage() {
    const el = $('#storageInfo');
    if (!el) return;
    try {
      const s = await api.storageInfo();
      el.replaceChildren(
        h('div', {}, h('b', {}, fmtSize(s.used)), h('span', {}, t('set.storageUsed', { n: s.count }))),
        s.free != null ? h('div', {}, h('b', {}, fmtSize(s.free)), h('span', {}, t('set.storageFree'))) : '',
        h('div', {}, h('b', {}, s.office ? t('set.yes') : t('set.no')), h('span', {}, t('set.office'))));
    } catch { el.textContent = ''; }
  }

  /* ---------------------------------------------------------------- */
  /* Snow & idle                                                       */
  /* ---------------------------------------------------------------- */
  let snowRaf = null;
  function setupSnow() {
    const c = $('#snow');
    cancelAnimationFrame(snowRaf);
    if (!S.snow || matchMedia('(prefers-reduced-motion: reduce)').matches) { c.classList.add('hidden'); return; }
    c.classList.remove('hidden');
    const ctx = c.getContext('2d');
    let flakes = [];
    const resize = () => {
      c.width = innerWidth; c.height = innerHeight;
      const n = Math.round((innerWidth * innerHeight) / 14000);
      flakes = Array.from({ length: n }, () => ({
        x: Math.random() * c.width, y: Math.random() * c.height, r: 1 + Math.random() * 2.6,
        s: 0.3 + Math.random() * 0.9, d: Math.random() * Math.PI * 2,
      }));
    };
    resize();
    window.onresize = resize;
    const frame = () => {
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.fillStyle = 'rgba(255,255,255,.85)';
      for (const f of flakes) {
        f.y += f.s; f.d += 0.01; f.x += Math.sin(f.d) * 0.4;
        if (f.y > c.height) { f.y = -5; f.x = Math.random() * c.width; }
        ctx.beginPath(); ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2); ctx.fill();
      }
      snowRaf = requestAnimationFrame(frame);
    };
    frame();
  }

  let lastInput = Date.now();
  function setupIdle() {
    ['pointermove', 'pointerdown', 'keydown', 'wheel'].forEach((e) => window.addEventListener(e, () => { lastInput = Date.now(); }, { passive: true }));
    setInterval(() => {
      if (!S.idleReturnMin || V.open || currentTab === 'upload') return;
      if (Date.now() - lastInput > S.idleReturnMin * 60000) switchTab('upload');
    }, 10000);
  }

  /* ---------------------------------------------------------------- */
  /* Bindings                                                          */
  /* ---------------------------------------------------------------- */
  function bind() {
    $$('[data-icon]').forEach((el) => el.prepend(icon(el.dataset.icon)));
    $$('.tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
    $$('[data-goto]').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.goto)));

    const logo = $('#logo');
    logo.addEventListener('error', () => {
      if (!logo.dataset.fallback) { logo.dataset.fallback = '1'; logo.src = appInfo.logoUrl; }
      else logo.replaceWith(h('span', { class: 'brand-text' }, 'fembimmo'));
    });

    bindCopy($('#shortLink'), () => (session.qr ? session.shortUrl || session.url : null));
    bindCopy($('#rShortLink'), () => (rsession.qr ? rsession.shortUrl || rsession.url : null));

    $('#btnNewRemote').addEventListener('click', async () => {
      const b = $('#btnNewRemote');
      b.disabled = true;
      try { await api.newRemote(); toast(t('remote.made'), 'ok'); } finally { b.disabled = false; }
    });

    $('#btnNewLink').addEventListener('click', async () => {
      const b = $('#btnNewLink');
      b.disabled = true;
      try { await api.newLink(); toast(t('set.linkMade'), 'ok'); } finally { b.disabled = false; }
    });

    $('#gallerySearch').addEventListener('input', (e) => { gQuery = e.target.value.trim().toLowerCase(); renderGallery(); });
    $$('#galleryFilter button').forEach((b) => b.addEventListener('click', () => {
      gFilter = b.dataset.f;
      $$('#galleryFilter button').forEach((x) => x.classList.toggle('on', x === b));
      renderGallery();
    }));
    $('#btnSlideshow').addEventListener('click', startSlideshow);

    $('#libForm').addEventListener('submit', (e) => {
      e.preventDefault();
      L.query = $('#libQuery').value.trim();
      libSearch(true);
    });
    $$('#libKind button').forEach((b) => b.addEventListener('click', () => {
      L.kind = b.dataset.k;
      $$('#libKind button').forEach((x) => x.classList.toggle('on', x === b));
      if (L.query) libSearch(true);
    }));
    $('#libSource').addEventListener('change', (e) => { L.source = e.target.value; if (L.query) libSearch(true); });
    $('#libMore').addEventListener('click', () => { L.page += 1; libSearch(false); });

    const viewer = $('#viewer');
    viewer.addEventListener('pointermove', pokeUi);
    viewer.addEventListener('pointerdown', pokeUi);

    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'q') { e.preventDefault(); api.quit(); return; }
      if (e.key === 'F11') { e.preventDefault(); api.toggleFullscreen(); return; }
      if (V.open) { e.preventDefault(); viewerKey({ key: e.key }); }
    });
    window.addEventListener('resize', () => {
      if (V.open && V.mode === 'pdf') pdfGo(V.page);
      if (V.open && V.placeBadge) V.placeBadge();
    });

    bindMusic();
  }

  /* ---------------------------------------------------------------- */
  /* Init                                                              */
  /* ---------------------------------------------------------------- */
  async function init() {
    api.connect();  // open the live event stream before anything else can use up connections
    const st = await api.getState();
    S = st.settings;
    items = st.items;
    session = st.session;
    tunnel = st.tunnel;
    online = st.online;
    rsession = st.remote;
    rtunnel = st.rtunnel;
    ytStatus = st.yt || ytStatus;
    depsState = st.deps || {};
    appInfo = st;

    api.on('session', (s) => { session = s; renderSession(); });
    api.on('tunnel', (s) => { tunnel = s; renderSession(); });
    api.on('remote:session', (s) => { rsession = s; renderRemote(); });
    api.on('rtunnel', (s) => { rtunnel = s; renderRemote(); });
    api.on('yt:status', (s) => { const was = ytStatus.installing; ytStatus = s; renderYtStatus(); if (was && !s.installing) toast(s.available && !s.error ? t('music.installed') : t('music.installFailedShort'), s.error ? 'bad' : 'ok'); });
    api.on('remote', onRemote);
    api.on('web:closed', () => { webOpenId = null; reportUi(); });
    api.on('deps', async (d) => {
      depsState = d || {};
      renderDeps();
      try { ytStatus = await fetch('/api/yt/status').then((r) => r.json()); renderYtStatus(); } catch { /* ignore */ }
    });
    api.on('video:ready', (it) => { if (it) toast(t('gallery.videoReady', { name: it.name }), 'ok'); });
    api.on('reconnect', async () => {
      const st2 = await api.getState();
      S = st2.settings; items = st2.items; session = st2.session; tunnel = st2.tunnel;
      rsession = st2.remote; rtunnel = st2.rtunnel; online = st2.online;
      applyTheme(); applyLanguage(); renderOnline();
    });
    api.on('items', (list) => { items = list; renderGallery(); });
    api.on('item:added', onItemAdded);
    api.on('upload:progress', renderProgress);
    api.on('net', (o) => { online = o; renderOnline(); toast(o ? t('net.back') : t('net.offline'), o ? 'ok' : 'bad'); });
    api.on('settings', (s) => {
      const prev = S;
      S = s;
      applyTheme();
      if (prev.showLogo !== s.showLogo) renderGallery(true);
      if (prev.language !== s.language) applyLanguage();
      else { renderSettings(); renderWelcome(); renderNowPlaying(); renderLibSetup(); }
      if (prev.snow !== s.snow) setupSnow();
    });

    bind();
    applyTheme();
    applyLanguage();
    renderOnline();
    startClock();
    setupSnow();
    setupIdle();
    setInterval(() => {
      renderExpiry();
      if (rsession.expiresAt || rtunnel.status !== 'online') renderRemote();
      if (tunnel.status !== 'online' || !session.qr) renderSession();
    }, 1000);
    setInterval(() => { if (currentTab === 'gallery') renderGallery(); }, 60000);

    reportUi();
    api.uiState({ h264: CAN_H264 });
    if (S.musicAutoplay) setTimeout(() => playPreset(0), 1500);
  }

  init();
})();

</script>
</body>
</html>
'''

UPLOAD_HTML = r'''<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#12424a">
<meta name="robots" content="noindex,nofollow">
<title>Fembimmo · Upload</title>
<style>
  :root {
    --petrol: #12424a; --petrol-2: #0d3238; --ink: #15201e; --ink-2: #4d5b57; --line: #dadfdb;
    --bg: #f2f3f0; --surface: #fff; --ok: #1d7a4c; --bad: #b42318; --brass: #b0894b;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root { --ink: #eef2f0; --ink-2: #a9b6b2; --line: #2a3634; --bg: #0e1513; --surface: #16201e; color-scheme: dark; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Roboto, sans-serif;
    background: var(--bg); color: var(--ink); -webkit-text-size-adjust: 100%;
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }
  header {
    background: var(--petrol); color: #fff;
    padding: calc(env(safe-area-inset-top, 0px) + 18px) 20px 64px;
  }
  .logo { display: inline-flex; background: #fff; border-radius: 12px; padding: 8px 12px; }
  .logo img { height: 34px; width: auto; display: block; }
  h1 { font-size: clamp(26px, 7vw, 34px); line-height: 1.1; letter-spacing: -0.02em; margin: 22px 0 8px; font-weight: 650; }
  header p { margin: 0; color: #cfe0dd; font-size: 16px; line-height: 1.45; max-width: 34ch; }
  main { max-width: 560px; margin: -44px auto 0; padding: 0 16px 32px; }
  .panel { background: var(--surface); border-radius: 22px; padding: 18px; box-shadow: 0 10px 30px rgba(10, 30, 30, .12); }
  .drop {
    border: 2px dashed var(--line); border-radius: 16px; padding: 22px 14px; text-align: center;
    transition: border-color .15s, background .15s;
  }
  .drop.over { border-color: var(--petrol); background: rgba(18, 66, 74, .06); }
  .btn {
    display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%;
    border: 0; border-radius: 14px; padding: 16px; font: inherit; font-size: 17px; font-weight: 600;
    cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  .btn svg { width: 22px; height: 22px; }
  .btn.primary { background: var(--petrol); color: #fff; }
  .btn.primary:active { background: var(--petrol-2); }
  .btn.ghost { background: transparent; color: var(--ink); border: 1.5px solid var(--line); margin-top: 10px; }
  .hint { color: var(--ink-2); font-size: 14px; margin: 12px 0 0; }
  input[type=file] { display: none; }
  .link { margin-top: 18px; }
  .link label { display: block; font-weight: 600; margin-bottom: 8px; font-size: 15px; }
  .link .row { display: flex; gap: 8px; }
  .link input {
    flex: 1; min-width: 0; font: inherit; font-size: 16px; padding: 14px; border-radius: 12px;
    border: 1.5px solid var(--line); background: var(--bg); color: var(--ink);
  }
  .link input:focus { outline: 2px solid var(--petrol); outline-offset: 1px; }
  .link .btn { width: auto; padding: 14px 18px; font-size: 16px; }
  ul { list-style: none; margin: 18px 0 0; padding: 0; display: grid; gap: 10px; }
  li { background: var(--surface); border-radius: 14px; padding: 12px 14px; box-shadow: 0 2px 8px rgba(10, 30, 30, .06); }
  li .top { display: flex; justify-content: space-between; gap: 10px; font-size: 15px; }
  li .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  li .state { color: var(--ink-2); flex: none; font-variant-numeric: tabular-nums; }
  li.done .state { color: var(--ok); }
  li.error .state { color: var(--bad); }
  .bar { height: 6px; border-radius: 6px; background: var(--line); margin-top: 10px; overflow: hidden; }
  .bar i { display: block; height: 100%; width: 0; background: var(--petrol); transition: width .2s; }
  li.done .bar i { background: var(--ok); }
  li.error .bar i { background: var(--bad); }
  .banner {
    display: none; margin-top: 16px; padding: 14px 16px; border-radius: 14px; background: rgba(29, 122, 76, .1);
    color: var(--ok); font-weight: 600; text-align: center;
  }
  .banner.show { display: block; }
  footer { text-align: center; color: var(--ink-2); font-size: 13px; padding: 8px 0 24px; }
</style>
</head>
<body>
<header>
  <div class="logo"><img src="/logo" alt="Fembimmo"></div>
  <h1 data-t="title"></h1>
  <p data-t="sub"></p>
</header>
<main>
  <section class="panel">
    <div class="drop" id="drop">
      <button class="btn primary" id="pick">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg>
        <span data-t="choose"></span>
      </button>
      <button class="btn ghost" id="cam">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>
        <span data-t="camera"></span>
      </button>
      <p class="hint" data-t="types"></p>
      <input type="file" id="files" multiple>
      <input type="file" id="camera" accept="image/*,video/*" capture="environment">
    </div>
    <form class="link" id="linkForm">
      <label for="url" data-t="linkLabel"></label>
      <div class="row">
        <input id="url" type="url" inputmode="url" autocomplete="off" placeholder="https://">
        <button class="btn primary" type="submit" data-t="send"></button>
      </div>
    </form>
  </section>
  <div class="banner" id="banner" role="status"></div>
  <ul id="list" aria-live="polite"></ul>
</main>
<footer>Fembimmo</footer>
<script>window.CFG = __CFG__;</script>
<script>
(() => {
  const CFG = window.CFG;
  const T = {
    nl: {
      title: 'Upload naar het scherm', sub: 'Kies bestanden of deel een link. Alles verschijnt meteen op het scherm.',
      choose: 'Bestanden kiezen', camera: 'Foto of video maken', drop: 'Sleep bestanden hierheen',
      types: 'PDF, foto’s, video’s, audio, PowerPoint, documenten en meer. Max {max} per bestand.',
      linkLabel: 'Website of link delen', send: 'Versturen', waiting: 'Wachten…', done: 'Op het scherm',
      failed: 'Mislukt', tooBig: 'Te groot', expired: 'Link verlopen', allDone: 'Klaar! Je bestanden staan op het scherm.',
      linkSent: 'Link staat op het scherm.', badUrl: 'Dit is geen geldige link.', expiredMsg: 'Deze link is verlopen. Scan de nieuwe QR-code op het scherm.'
    },
    en: {
      title: 'Upload to the screen', sub: 'Choose files or share a link. Everything appears on the screen right away.',
      choose: 'Choose files', camera: 'Take a photo or video', drop: 'Drop files here',
      types: 'PDF, photos, videos, audio, PowerPoint, documents and more. Max {max} per file.',
      linkLabel: 'Share a website or link', send: 'Send', waiting: 'Waiting…', done: 'On screen',
      failed: 'Failed', tooBig: 'Too large', expired: 'Link expired', allDone: 'Done! Your files are on the screen.',
      linkSent: 'The link is on the screen.', badUrl: 'That is not a valid link.', expiredMsg: 'This link has expired. Scan the new QR code on the screen.'
    }
  };
  const L = T[CFG.lang] || T.nl;
  document.documentElement.lang = CFG.lang;
  const maxBytes = CFG.maxMB * 1024 * 1024;
  const fmtMax = CFG.maxMB >= 1024 ? (CFG.maxMB / 1024) + ' GB' : CFG.maxMB + ' MB';
  document.querySelectorAll('[data-t]').forEach((el) => { el.textContent = L[el.dataset.t].replace('{max}', fmtMax); });

  const $ = (id) => document.getElementById(id);
  const CHUNK = 8 * 1024 * 1024;
  const queue = [];
  let running = 0;
  let pending = 0;
  let wakeLock = null;

  const banner = (msg, bad) => {
    const b = $('banner');
    b.textContent = msg;
    b.style.color = bad ? 'var(--bad)' : '';
    b.style.background = bad ? 'rgba(180,35,24,.1)' : '';
    b.classList.add('show');
  };

  function fmt(n) {
    if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + ' KB';
    if (n < 1024 ** 3) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 ** 3).toFixed(2) + ' GB';
  }

  function row(name) {
    const li = document.createElement('li');
    li.innerHTML = '<div class="top"><span class="name"></span><span class="state"></span></div><div class="bar"><i></i></div>';
    li.querySelector('.name').textContent = name;
    $('list').prepend(li);
    return {
      li,
      set(pct, text) {
        li.querySelector('i').style.width = pct + '%';
        if (text != null) li.querySelector('.state').textContent = text;
      },
    };
  }

  function uid() {
    if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    return Math.random().toString(36).slice(2) + Date.now().toString(36) + Math.random().toString(36).slice(2);
  }

  function sendChunk(id, index, total, file, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('POST', '/api/chunk/' + CFG.token);
      x.setRequestHeader('Content-Type', 'application/octet-stream');
      x.setRequestHeader('X-Upload-Id', id);
      x.setRequestHeader('X-Chunk-Index', String(index));
      x.setRequestHeader('X-Chunk-Total', String(total));
      x.setRequestHeader('X-File-Size', String(file.size));
      x.setRequestHeader('X-File-Name', encodeURIComponent(file.name || 'file'));
      x.setRequestHeader('X-File-Type', encodeURIComponent(file.type || ''));
      x.timeout = 120000;
      x.upload.onprogress = (e) => onProgress(e.loaded);
      x.onload = () => {
        let j = {};
        try { j = JSON.parse(x.responseText); } catch (e) { /* ignore */ }
        if (x.status === 200 || (x.status === 409 && j.expected !== undefined)) return resolve(j);
        const err = new Error(j.error || 'HTTP ' + x.status);
        err.status = x.status;
        err.fatal = [400, 410, 413].includes(x.status);
        reject(err);
      };
      x.onerror = () => reject(new Error('network'));
      x.ontimeout = () => reject(new Error('timeout'));
      x.send(blob);
    });
  }

  async function upload(file, r) {
    if (file.size > maxBytes) { r.li.classList.add('error'); r.set(100, L.tooBig); return false; }
    const id = uid();
    const total = Math.max(1, Math.ceil(file.size / CHUNK));
    let index = 0;
    while (index < total) {
      const start = index * CHUNK;
      const blob = file.slice(start, Math.min(file.size, start + CHUNK));
      let res;
      for (let tries = 0; ; tries++) {
        try {
          res = await sendChunk(id, index, total, file, blob, (loaded) => {
            const pct = file.size ? Math.min(99, ((start + loaded) / file.size) * 100) : 50;
            r.set(pct, Math.floor(pct) + '%');
          });
          break;
        } catch (e) {
          if (e.fatal || tries >= 5) throw e;
          await new Promise((ok) => setTimeout(ok, 1000 * (tries + 1)));
        }
      }
      if (res.expected !== undefined) { index = res.expected; continue; }
      if (res.done) break;
      index = res.next !== undefined ? res.next : index + 1;
    }
    r.li.classList.add('done');
    r.set(100, L.done);
    return true;
  }

  async function pump() {
    while (running < 2 && queue.length) {
      const job = queue.shift();
      running++;
      upload(job.file, job.r)
        .catch((e) => {
          job.r.li.classList.add('error');
          job.r.set(100, e.status === 410 ? L.expired : e.status === 413 ? L.tooBig : L.failed);
          if (e.status === 410) banner(L.expiredMsg, true);
        })
        .finally(() => {
          running--;
          pending--;
          if (pending === 0) {
            if (!document.querySelector('li.error')) banner(L.allDone);
            if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
          }
          pump();
        });
    }
  }

  async function addFiles(list) {
    const files = Array.from(list || []);
    if (!files.length) return;
    $('banner').classList.remove('show');
    try { if (navigator.wakeLock && !wakeLock) wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* ignore */ }
    for (const file of files) {
      const r = row(file.name || 'file');
      r.set(0, L.waiting + ' ' + fmt(file.size));
      queue.push({ file, r });
      pending++;
    }
    pump();
  }

  $('pick').addEventListener('click', () => $('files').click());
  $('cam').addEventListener('click', () => $('camera').click());
  $('files').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
  $('camera').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });

  const drop = $('drop');
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));

  $('linkForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('url');
    const url = input.value.trim();
    if (!url) return;
    const r = row(url);
    r.set(40, L.waiting);
    try {
      const res = await fetch('/api/link/' + CFG.token, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
      });
      if (res.status === 410) { r.li.classList.add('error'); r.set(100, L.expired); banner(L.expiredMsg, true); return; }
      if (!res.ok) { r.li.classList.add('error'); r.set(100, L.failed); banner(L.badUrl, true); return; }
      r.li.classList.add('done');
      r.set(100, L.done);
      input.value = '';
      banner(L.linkSent);
    } catch (err) {
      r.li.classList.add('error');
      r.set(100, L.failed);
    }
  });
})();
</script>
</body>
</html>
'''

EXPIRED_HTML = r'''<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Fembimmo</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #12424a; color: #fff;
    font-family: "Segoe UI", system-ui, -apple-system, Roboto, sans-serif; padding: 24px; box-sizing: border-box; text-align: center; }
  .logo { display: inline-flex; background: #fff; border-radius: 12px; padding: 8px 12px; margin-bottom: 28px; }
  .logo img { height: 34px; }
  h1 { font-size: 26px; margin: 0 0 10px; letter-spacing: -0.01em; }
  p { color: #cfe0dd; max-width: 32ch; margin: 0 auto; line-height: 1.5; font-size: 17px; }
</style>
</head>
<body>
<div>
  <div class="logo"><img src="/logo" alt="Fembimmo"></div>
  <h1 id="h"></h1>
  <p id="p"></p>
</div>
<script>window.CFG = __CFG__;</script>
<script>
  var en = window.CFG.lang === 'en';
  var remote = window.CFG.kind === 'remote';
  document.documentElement.lang = en ? 'en' : 'nl';
  document.getElementById('h').textContent = en ? 'This link has expired' : 'Deze link is verlopen';
  document.getElementById('p').textContent = remote
    ? (en ? 'Scan the QR code on the Remote tab of the screen again.' : 'Scan opnieuw de QR-code op het tabblad Afstandsbediening van het scherm.')
    : (en ? 'Scan the new QR code on the screen to upload.' : 'Scan de nieuwe QR-code op het scherm om te uploaden.');
</script>
</body>
</html>
'''

REMOTE_HTML = r'''<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#12424a">
<meta name="robots" content="noindex,nofollow">
<title>Fembimmo · Remote</title>
<style>
  :root {
    --petrol: #12424a; --petrol-2: #0d3238; --ink: #15201e; --ink-2: #4d5b57; --ink-3: #87938f; --line: #dadfdb;
    --bg: #f2f3f0; --surface: #fff; --brass: #b0894b; --bad: #b42318; --ok: #1d7a4c;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root { --ink: #eef2f0; --ink-2: #a9b6b2; --ink-3: #6c7a76; --line: #2a3634; --bg: #0e1513; --surface: #16201e; color-scheme: dark; }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; }
  body {
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
    background: var(--bg); color: var(--ink); padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 24px);
  }
  button { font: inherit; color: inherit; cursor: pointer; }
  svg { width: 24px; height: 24px; display: block; }
  header {
    background: var(--petrol); color: #fff; padding: calc(env(safe-area-inset-top, 0px) + 14px) 16px 18px;
    position: sticky; top: 0; z-index: 5; box-shadow: 0 4px 18px rgba(0, 0, 0, .18);
  }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .logo { display: inline-flex; background: #fff; border-radius: 10px; padding: 6px 10px; }
  .logo img { height: 26px; display: block; }
  .dot { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: #cfe0dd; }
  .dot i { width: 9px; height: 9px; border-radius: 50%; background: #5fd38d; }
  .dot.off i { background: #ff7a6e; }
  .onscreen { margin-top: 14px; font-size: 14px; color: #cfe0dd; }
  .onscreen b { display: block; color: #fff; font-size: 18px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ctrl { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 14px; }
  .ctrl button {
    border: 0; border-radius: 16px; padding: 16px 0; background: rgba(255, 255, 255, .14); color: #fff;
    display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 13px; font-weight: 600;
  }
  .ctrl button:active { background: rgba(255, 255, 255, .28); }
  .ctrl button.close { background: #fff; color: var(--petrol); }
  main { padding: 16px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 22px; max-width: 640px; margin: 0 auto; }
  main > section { min-width: 0; }
  h2 { font-size: 18px; margin: 0 0 10px; letter-spacing: -.01em; }
  .tabs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .tabs button {
    border: 1.5px solid var(--line); background: var(--surface); border-radius: 14px; padding: 12px 6px;
    display: flex; flex-direction: column; align-items: center; gap: 6px; font-weight: 600; font-size: 14px;
  }
  .tabs button.on { border-color: var(--petrol); background: var(--petrol); color: #fff; }
  .chips { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 4px; scrollbar-width: none; }
  .chips button { flex: none; border: 1.5px solid var(--line); background: var(--surface); border-radius: 999px; padding: 8px 14px; font-weight: 600; font-size: 14px; }
  .chips button.on { background: var(--ink); color: var(--bg); border-color: var(--ink); }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px; }
  .item {
    border: 0; padding: 0; background: var(--surface); border-radius: 12px; overflow: hidden; text-align: left;
    box-shadow: 0 2px 8px rgba(0, 0, 0, .06); position: relative;
  }
  .item.on { outline: 3px solid var(--brass); }
  .item .th { aspect-ratio: 1; background: #dfe4e1; display: grid; place-items: center; color: var(--ink-3); position: relative; }
  @media (prefers-color-scheme: dark) { .item .th { background: #223230; } }
  .item .th img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .item span { display: block; padding: 6px 8px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty { color: var(--ink-2); font-size: 14px; }
  .card { background: var(--surface); border-radius: 18px; padding: 16px; box-shadow: 0 2px 10px rgba(0, 0, 0, .06); }
  .np { display: flex; gap: 12px; align-items: center; }
  .np img { width: 64px; height: 64px; border-radius: 10px; object-fit: cover; background: var(--line); flex: none; }
  .np div { min-width: 0; }
  .np b, .np small { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .np small { color: var(--ink-2); margin-top: 2px; }
  .mctrl { display: flex; justify-content: center; align-items: center; gap: 18px; margin: 14px 0 8px; }
  .mctrl button { border: 0; background: transparent; width: 52px; height: 52px; border-radius: 50%; display: grid; place-items: center; }
  .mctrl button.play { background: var(--petrol); color: #fff; width: 64px; height: 64px; }
  input[type=range] { width: 100%; accent-color: var(--petrol); }
  .presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
  .presets button { border: 1.5px solid var(--line); background: transparent; border-radius: 999px; padding: 8px 12px; font-size: 14px; font-weight: 600; }
  form.search { display: flex; gap: 8px; margin-top: 12px; }
  form.search input {
    flex: 1; min-width: 0; font: inherit; font-size: 16px; padding: 12px; border-radius: 12px; border: 1.5px solid var(--line);
    background: var(--bg); color: var(--ink);
  }
  .btn { border: 0; border-radius: 12px; padding: 12px 16px; font-weight: 600; background: var(--petrol); color: #fff; }
  .btn.ghost { background: transparent; border: 1.5px solid var(--line); color: var(--ink); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; }
  .row .btn { flex: 1 1 45%; }
  .seg { display: flex; background: var(--line); border-radius: 12px; padding: 3px; }
  .seg button { flex: 1; border: 0; background: transparent; border-radius: 9px; padding: 10px; font-weight: 600; }
  .seg button.on { background: var(--surface); box-shadow: 0 1px 3px rgba(0, 0, 0, .1); }
  .setrow { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px solid var(--line); }
  .setrow:first-child { border-top: 0; padding-top: 0; }
  .toast {
    position: fixed; left: 50%; bottom: calc(env(safe-area-inset-bottom, 0px) + 20px); transform: translate(-50%, 20px);
    background: var(--ink); color: var(--bg); padding: 10px 16px; border-radius: 12px; font-weight: 600; opacity: 0;
    transition: opacity .2s, transform .2s; pointer-events: none; z-index: 10; max-width: 90vw; text-align: center;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .lost { display: none; background: var(--bad); color: #fff; padding: 10px 16px; text-align: center; font-weight: 600; }
  .lost.show { display: block; }
  footer { text-align: center; color: var(--ink-3); font-size: 12px; }
</style>
</head>
<body>
<div class="lost" id="lost"></div>
<header>
  <div class="top">
    <div class="logo"><img src="/logo" alt="Fembimmo"></div>
    <span class="dot" id="dot"><i></i><span id="dotText"></span></span>
  </div>
  <div class="onscreen"><span id="onLabel"></span><b id="onName">…</b></div>
  <div class="ctrl">
    <button data-cmd="viewer.prev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg><span data-t="prev"></span></button>
    <button data-cmd="viewer.close" class="close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg><span data-t="close"></span></button>
    <button data-cmd="viewer.next"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg><span data-t="next"></span></button>
  </div>
</header>

<main>
  <section>
    <h2 data-t="screen"></h2>
    <div class="tabs" id="tabs"></div>
  </section>

  <section>
    <h2 data-t="gallery"></h2>
    <div class="chips" id="filters"></div>
    <div class="grid" id="grid"></div>
    <p class="empty" id="empty" data-t="empty" hidden></p>
    <div class="row" style="margin-top:12px">
      <button class="btn" data-cmd="slideshow" data-t="slideshow"></button>
    </div>
  </section>

  <section class="card">
    <h2 data-t="music"></h2>
    <div class="np"><img id="art" alt=""><div><b id="npTitle"></b><small id="npArtist"></small></div></div>
    <div class="mctrl">
      <button data-cmd="music.prev" aria-label="prev"><svg viewBox="0 0 24 24"><path d="M19 4.5v15L8 12z" fill="currentColor"/><path d="M5 5v14" stroke="currentColor" stroke-width="2"/></svg></button>
      <button data-cmd="music.toggle" class="play" id="playBtn" aria-label="play"></button>
      <button data-cmd="music.next" aria-label="next"><svg viewBox="0 0 24 24"><path d="M5 4.5v15l11-7.5z" fill="currentColor"/><path d="M19 5v14" stroke="currentColor" stroke-width="2"/></svg></button>
    </div>
    <input type="range" id="vol" min="0" max="100" value="80" aria-label="volume">
    <div class="presets" id="presets"></div>
    <form class="search" id="musicSearch">
      <input id="musicQuery" type="search" autocomplete="off">
      <button class="btn" type="submit" data-t="play"></button>
    </form>
    <div class="row" style="margin-top:10px">
      <button class="btn ghost" data-cmd="music.shuffle" id="shuffleBtn" data-t="shuffle"></button>
    </div>
  </section>

  <section class="card">
    <h2 data-t="settings"></h2>
    <div class="setrow"><span data-t="language"></span>
      <div class="seg" id="lang"><button data-v="nl">NL</button><button data-v="en">EN</button></div></div>
    <div class="setrow"><span data-t="theme"></span>
      <div class="seg" id="theme"><button data-v="light" data-t="light"></button><button data-v="dark" data-t="dark"></button></div></div>
    <div class="setrow"><span data-t="snow"></span>
      <div class="seg" id="snow"><button data-v="1" data-t="on"></button><button data-v="0" data-t="off"></button></div></div>
    <div class="row" style="margin-top:12px">
      <button class="btn ghost" data-cmd="link.new" data-t="newQr"></button>
      <button class="btn ghost" data-cmd="app.fullscreen" data-t="fullscreen"></button>
    </div>
  </section>
  <footer>Fembimmo Display</footer>
</main>
<div class="toast" id="toast"></div>

<script>window.CFG = __CFG__;</script>
<script>
(() => {
  const CFG = window.CFG;
  const T = {
    nl: {
      online: 'Verbonden', offline: 'Geen verbinding', on: 'Aan', off: 'Uit', onLabel: 'Nu op het scherm',
      prev: 'Vorige', close: 'Sluiten', next: 'Volgende', screen: 'Tabblad op het scherm', gallery: 'Galerij',
      empty: 'De galerij is nog leeg.', slideshow: 'Diavoorstelling starten', music: 'Muziek', play: 'Afspelen',
      shuffle: 'Shuffle', settings: 'Scherm', language: 'Taal',
      theme: 'Thema', light: 'Licht', dark: 'Donker', snow: 'Sneeuw', newQr: 'Nieuwe upload-QR', fullscreen: 'Volledig scherm',
      nothing: 'Er speelt niets', pick: 'Kies een kerstlijst', sent: 'Verstuurd', lost: 'Geen verbinding met het scherm. Scan de QR-code opnieuw als dit blijft duren.',
      search: 'Zoek een nummer op YouTube…',
      tabs: { upload: 'Upload', gallery: 'Galerij', library: 'Bibliotheek', music: 'Muziek', remote: 'Remote', settings: 'Instellingen' },
      filters: { all: 'Alles', image: 'Foto’s', video: 'Video’s', doc: 'Documenten', link: 'Links', audio: 'Audio' },
      presets: ['Kersthits', 'Klassieke kerst', 'Nederlandstalig', 'Kerstjazz', 'Lo-fi', 'Akoestisch', 'Piano', 'Koren', 'Kinderen'],
      viewerPage: 'pagina', webOpen: 'Website open',
    },
    en: {
      online: 'Connected', offline: 'No connection', on: 'On', off: 'Off', onLabel: 'On screen now',
      prev: 'Previous', close: 'Close', next: 'Next', screen: 'Tab on screen', gallery: 'Gallery',
      empty: 'The gallery is still empty.', slideshow: 'Start slideshow', music: 'Music', play: 'Play',
      shuffle: 'Shuffle', settings: 'Screen', language: 'Language',
      theme: 'Theme', light: 'Light', dark: 'Dark', snow: 'Snow', newQr: 'New upload QR', fullscreen: 'Full screen',
      nothing: 'Nothing playing', pick: 'Pick a Christmas list', sent: 'Sent', lost: 'Lost connection to the screen. Scan the QR code again if this persists.',
      search: 'Search a song on YouTube…',
      tabs: { upload: 'Upload', gallery: 'Gallery', library: 'Library', music: 'Music', remote: 'Remote', settings: 'Settings' },
      filters: { all: 'All', image: 'Photos', video: 'Videos', doc: 'Documents', link: 'Links', audio: 'Audio' },
      presets: ['Christmas hits', 'Classic', 'Dutch', 'Jazz', 'Lo-fi', 'Acoustic', 'Piano', 'Carols', 'Kids'],
      viewerPage: 'page', webOpen: 'Website open',
    },
  };
  let L = T[CFG.lang] || T.nl;
  const $ = (id) => document.getElementById(id);
  const API = '/rapi/' + CFG.token;
  const GROUP = { image: 'image', video: 'video', audio: 'audio', pdf: 'doc', presentation: 'doc', document: 'doc', text: 'doc', other: 'doc', html: 'link', link: 'link' };
  const ICON = {
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    video: '<path d="m16 13 5.2 3.5a.5.5 0 0 0 .8-.4V7.9a.5.5 0 0 0-.8-.4L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    audio: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    doc: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    link: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    gallery: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    library: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    remote: '<rect x="6" y="2" width="12" height="20" rx="3"/><circle cx="12" cy="8" r="2.5"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  };
  const svg = (p) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
  const PLAY = '<svg viewBox="0 0 24 24"><path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z" fill="currentColor"/></svg>';
  const PAUSE = '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"/><rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"/></svg>';

  let state = null;
  let filter = 'all';
  let fails = 0;
  let lastGridKey = '';

  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('show'), 1600);
  }

  async function cmd(c, arg) {
    if (navigator.vibrate) navigator.vibrate(12);
    try {
      const r = await fetch(API + '/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: c, arg }) });
      if (r.status === 410) { location.reload(); return; }
      toast(L.sent);
      setTimeout(poll, 350);
    } catch (e) { toast(L.offline); }
  }

  function applyText() {
    document.documentElement.lang = CFG.lang;
    document.querySelectorAll('[data-t]').forEach((el) => { el.textContent = L[el.dataset.t]; });
    $('onLabel').textContent = L.onLabel;
    $('musicQuery').placeholder = L.search;
    $('tabs').innerHTML = '';
    for (const k of Object.keys(L.tabs)) {
      const b = document.createElement('button');
      b.dataset.tab = k;
      b.innerHTML = svg(ICON[k]) + '<span></span>';
      b.querySelector('span').textContent = L.tabs[k];
      b.onclick = () => cmd('tab', k);
      $('tabs').append(b);
    }
    $('filters').innerHTML = '';
    for (const k of Object.keys(L.filters)) {
      const b = document.createElement('button');
      b.dataset.f = k;
      b.textContent = L.filters[k];
      b.className = k === filter ? 'on' : '';
      b.onclick = () => { filter = k; lastGridKey = ''; render(); document.querySelectorAll('#filters button').forEach((x) => x.classList.toggle('on', x === b)); };
      $('filters').append(b);
    }
    $('presets').innerHTML = '';
    L.presets.forEach((name, i) => {
      const b = document.createElement('button');
      b.textContent = name;
      b.onclick = () => cmd('music.preset', i);
      $('presets').append(b);
    });
  }

  function render() {
    if (!state) return;
    const ui = state.ui || {};
    const v = ui.viewer;
    $('onName').textContent = v
      ? (v.name || '') + (v.pages > 1 ? '  ·  ' + L.viewerPage + ' ' + v.page + '/' + v.pages : '')
      : (L.tabs[ui.tab] || ui.tab || '…');
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', !v && b.dataset.tab === ui.tab));
    $('dot').classList.toggle('off', !state.online);
    $('dotText').textContent = state.online ? L.online : L.offline;

    const list = state.items.filter((i) => filter === 'all' || GROUP[i.type] === filter);
    const key = filter + '|' + list.map((i) => i.id + i.status + i.name).join(',');
    if (key !== lastGridKey) {
      lastGridKey = key;
      const grid = $('grid');
      grid.innerHTML = '';
      for (const it of list) {
        const b = document.createElement('button');
        b.className = 'item';
        b.dataset.id = it.id;
        b.innerHTML = '<div class="th">' + svg(ICON[GROUP[it.type]] || ICON.doc) + '</div><span></span>';
        b.querySelector('span').textContent = it.name || '';
        if (it.thumb) {
          const img = document.createElement('img');
          img.loading = 'lazy';
          img.src = API + '/thumb/' + encodeURIComponent(it.id);
          img.onerror = () => img.remove();
          b.querySelector('.th').append(img);
        }
        b.onclick = () => cmd('open', it.id);
        grid.append(b);
      }
      $('empty').hidden = list.length > 0;
    }
    document.querySelectorAll('#grid .item').forEach((b) => b.classList.toggle('on', !!(v && v.id === b.dataset.id)));

    const np = state.np;
    $('npTitle').textContent = np ? np.title : L.nothing;
    $('npArtist').textContent = np ? np.artist : L.pick;
    if (np && np.art) { $('art').src = np.art; $('art').style.display = ''; } else $('art').style.display = 'none';
    $('playBtn').innerHTML = np && !np.paused ? PAUSE : PLAY;
    $('shuffleBtn').style.background = np && np.shuffle ? 'var(--petrol)' : '';
    $('shuffleBtn').style.color = np && np.shuffle ? '#fff' : '';
    if (np && document.activeElement !== $('vol')) $('vol').value = Math.round(np.vol * 100);

    const s = state.settings;
    if (s.language !== CFG.lang) { CFG.lang = s.language; L = T[CFG.lang] || T.nl; lastGridKey = ''; applyText(); render(); return; }
    document.querySelectorAll('#lang button').forEach((b) => b.classList.toggle('on', b.dataset.v === s.language));
    document.querySelectorAll('#theme button').forEach((b) => b.classList.toggle('on', b.dataset.v === s.theme));
    document.querySelectorAll('#snow button').forEach((b) => b.classList.toggle('on', (b.dataset.v === '1') === !!s.snow));
  }

  async function poll() {
    try {
      const r = await fetch(API + '/state', { cache: 'no-store' });
      if (r.status === 410 || r.status === 404) { location.reload(); return; }
      state = await r.json();
      fails = 0;
      $('lost').classList.remove('show');
      render();
    } catch (e) {
      if (++fails >= 3) { $('lost').textContent = L.lost; $('lost').classList.add('show'); }
    }
  }

  document.querySelectorAll('[data-cmd]').forEach((b) => b.addEventListener('click', () => cmd(b.dataset.cmd)));
  document.querySelectorAll('#lang button').forEach((b) => b.addEventListener('click', () => cmd('set', { key: 'language', value: b.dataset.v })));
  document.querySelectorAll('#theme button').forEach((b) => b.addEventListener('click', () => cmd('set', { key: 'theme', value: b.dataset.v })));
  document.querySelectorAll('#snow button').forEach((b) => b.addEventListener('click', () => cmd('set', { key: 'snow', value: b.dataset.v === '1' })));
  let volTimer;
  $('vol').addEventListener('input', (e) => { clearTimeout(volTimer); volTimer = setTimeout(() => cmd('music.volume', e.target.value / 100), 150); });
  $('musicSearch').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('musicQuery').value.trim();
    if (q) { cmd('music.search', q); $('musicQuery').blur(); }
  });

  applyText();
  poll();
  setInterval(() => { if (!document.hidden) poll(); }, 2000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
</script>
</body>
</html>
'''


if __name__ == '__main__':
    main()

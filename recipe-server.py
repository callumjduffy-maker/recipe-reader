#!/usr/bin/env python3
"""
Recipe Reader — local server.

Usage:
    python3 recipe-server.py

Then visit http://localhost:8765 in your browser.
Press Ctrl+C to stop.
"""

import http.server
import urllib.parse
import json
import re
import sys
import webbrowser
import threading
import requests

# curl-cffi impersonates real browser TLS fingerprints — bypasses Akamai/Cloudflare
try:
    from curl_cffi import requests as cffi_requests
    CFFI_AVAILABLE = True
except ImportError:
    CFFI_AVAILABLE = False

# Playwright is optional — used as a last resort for JS-rendered sites
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

import os
PORT = int(os.environ.get("PORT", 8765))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Signs that a response was blocked by bot protection
BOT_BLOCK_SIGNALS = [
    "access denied", "403 forbidden", "blocked", "challenge",
    "captcha", "robot", "cloudflare", "akamai", "just a moment",
    "enable javascript", "checking your browser",
]

def looks_blocked(html):
    """Return True if the HTML looks like a bot-block page rather than real content."""
    if len(html) < 5000:
        return True
    lower = html.lower()
    return any(sig in lower for sig in BOT_BLOCK_SIGNALS)


def fetch_with_cffi(url):
    """Fetch impersonating Chrome's TLS fingerprint — bypasses Akamai/Cloudflare."""
    if not CFFI_AVAILABLE:
        raise RuntimeError("curl-cffi not installed")
    resp = cffi_requests.get(url, impersonate="chrome124", timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_with_requests(url):
    """Standard fetch via requests library."""
    session = requests.Session()
    resp = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def fetch_with_playwright(url):
    """Fetch via a real headless Chromium — bypasses JS rendering and many bot checks."""
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright not installed. Run: pip3 install playwright && python3 -m playwright install chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-GB",
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        page = ctx.new_page()
        # Hide automation markers
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        try:
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            # Brief wait for any JS to populate structured data
            page.wait_for_timeout(1500)
            html = page.content()
        finally:
            browser.close()
    return html


def fetch_html(url):
    """
    Fetch strategy (in order):
    1. curl-cffi with Chrome TLS fingerprint — bypasses Akamai/Cloudflare/most bot protection
    2. Standard requests — fast, works for unprotected sites
    3. Playwright headless browser — last resort for JS-rendered sites
    Returns (html, method_used).
    """
    # 1. curl-cffi (best bot evasion) — trust any large 200 response
    if CFFI_AVAILABLE:
        try:
            html = fetch_with_cffi(url)
            if len(html) > 10000:
                return html, "curl-cffi"
            if not looks_blocked(html):
                return html, "curl-cffi"
            print(f"  curl-cffi: short/blocked response, trying Playwright…")
        except Exception as e:
            print(f"  curl-cffi failed ({e}), trying next method…")

    # 2. Standard requests — also trust large responses
    try:
        html = fetch_with_requests(url)
        if len(html) > 10000:
            return html, "requests"
        if not looks_blocked(html):
            return html, "requests"
        print(f"  requests: short/blocked response, trying Playwright…")
    except Exception as e:
        print(f"  requests failed ({e}), trying Playwright…")

    # 3. Playwright
    pw_html = fetch_with_playwright(url)
    return pw_html, "playwright"


# ── Recipe extraction (mirrors the JS logic, runs server-side) ─────────────────

def extract_recipe(html, source_url=""):
    """Return a dict with recipe data, or raise ValueError."""

    # 1. Try JSON-LD <script> tags
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        recipe = _find_recipe_in_json_text(m.group(1))
        if recipe:
            return _build_recipe(recipe, source_url)

    # 2. Try __NEXT_DATA__ (Next.js sites like BBC Good Food, Waitrose)
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if m:
        try:
            data = json.loads(m.group(1))
            recipe = _find_recipe_in_obj(data, 0)
            if recipe:
                return _build_recipe(recipe, source_url)
        except Exception:
            pass

    # 3. Try any inline JSON blob containing recipeIngredient
    for m in re.finditer(r'\{[^{}]*"recipeIngredient"[^{}]*\}', html):
        try:
            obj = json.loads(m.group(0))
            if obj.get("recipeIngredient"):
                return _build_recipe(obj, source_url)
        except Exception:
            pass

    raise ValueError(
        "No recipe data found on this page. "
        "The site may use a format we can't read, or it may require JavaScript to render."
    )


def _find_recipe_in_json_text(text):
    try:
        data = json.loads(text.strip())
    except Exception:
        return None
    candidates = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        if "@graph" in data:
            candidates = data["@graph"]
        else:
            candidates = [data]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        types = item.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "Recipe" in types:
            return item
    return None


def _find_recipe_in_obj(obj, depth):
    if depth > 10 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, list):
        for item in obj:
            r = _find_recipe_in_obj(item, depth + 1)
            if r:
                return r
        return None
    types = obj.get("@type", [])
    if isinstance(types, str):
        types = [types]
    if "Recipe" in types and (obj.get("recipeIngredient") or obj.get("recipeInstructions")):
        return obj
    for v in obj.values():
        r = _find_recipe_in_obj(v, depth + 1)
        if r:
            return r
    return None


def _build_recipe(data, source_url):
    name = data.get("name") or "Untitled Recipe"
    description = _strip_html(data.get("description") or "")
    image = _extract_image(data.get("image"))

    # Servings
    servings = 1
    yield_raw = data.get("recipeYield") or data.get("yield")
    if yield_raw:
        for y in ([yield_raw] if not isinstance(yield_raw, list) else yield_raw):
            try:
                n = float(re.search(r"[\d.]+", str(y)).group())
                if n > 0:
                    servings = n
                    break
            except Exception:
                pass

    ingredients = [str(i) for i in (data.get("recipeIngredient") or [])]
    instructions = _extract_instructions(data.get("recipeInstructions") or [])

    meta = []
    for key, label in [("prepTime", "Prep"), ("cookTime", "Cook"), ("totalTime", "Total")]:
        if data.get(key):
            meta.append({"label": label, "value": _format_duration(data[key])})
    if servings > 1:
        meta.append({"label": "Serves", "value": servings})
    for key, label in [("recipeCuisine", "Cuisine"), ("recipeCategory", "Category")]:
        val = data.get(key)
        if val:
            meta.append({"label": label, "value": ", ".join([val] if isinstance(val, str) else val)})

    return {
        "name": name,
        "description": description,
        "image": image,
        "servings": servings,
        "ingredients": ingredients,
        "instructions": instructions,
        "meta": meta,
        "sourceUrl": source_url,
    }


def _extract_instructions(raw):
    steps = []
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, str):
            s = _strip_html(item).strip()
            if s:
                steps.append(s)
        elif isinstance(item, dict):
            t = item.get("@type", "")
            if t == "HowToSection":
                for sub in (item.get("itemListElement") or []):
                    s = _strip_html((sub if isinstance(sub, str) else sub.get("text") or sub.get("name") or "")).strip()
                    if s:
                        steps.append(s)
            else:
                s = _strip_html(item.get("text") or item.get("name") or "").strip()
                if s:
                    steps.append(s)
    return steps


def _extract_image(img):
    if not img:
        return None
    if isinstance(img, str):
        return img
    if isinstance(img, list):
        for i in img:
            url = i if isinstance(i, str) else (i.get("url") or i.get("contentUrl"))
            if url:
                return url
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl")
    return None


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s))).strip()


def _format_duration(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(iso))
    if not m:
        return str(iso)
    h, mins = int(m.group(1) or 0), int(m.group(2) or 0)
    if h and mins:
        return f"{h}h {mins}m"
    if h:
        return f"{h}h"
    if mins:
        return f"{mins} min"
    return str(iso)


# ── HTTP handler ───────────────────────────────────────────────────────────────

class RecipeHandler(http.server.BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Serve the HTML app
        if path in ("/", "/index.html", ""):
            self._serve_html()
            return

        # PWA support files
        if path == "/manifest.json":
            self._serve_manifest()
            return
        if path == "/sw.js":
            self._serve_sw()
            return
        if path == "/icon.png":
            self._serve_icon()
            return

        # /recipe?url=... → fetch page, extract recipe, return JSON
        if path == "/recipe":
            url = (params.get("url") or [None])[0]
            if not url:
                self._json_error(400, "Missing ?url= parameter")
                return
            self._handle_recipe(url)
            return

        self.send_error(404)

    def _handle_recipe(self, url):
        print(f"  Fetching: {url}")
        try:
            html, method = fetch_html(url)
            print(f"  Fetched via {method} ({len(html):,} bytes)")
        except requests.exceptions.Timeout:
            self._json_error(504, "The recipe site took too long to respond (timed out).")
            return
        except requests.exceptions.HTTPError as e:
            self._json_error(502, f"The recipe site returned an error: {e}")
            return
        except requests.exceptions.ConnectionError as e:
            self._json_error(502, f"Could not connect to the recipe site. Check your internet connection.")
            return
        except RuntimeError as e:
            self._json_error(503, str(e))
            return
        except Exception as e:
            self._json_error(500, f"Fetch error: {e}")
            return

        try:
            recipe = extract_recipe(html, url)
        except ValueError as e:
            self._json_error(422, str(e))
            return
        except Exception as e:
            self._json_error(500, f"Error parsing recipe: {e}")
            return

        body = json.dumps(recipe, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipe-reader.html")
        try:
            with open(html_path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, "recipe-reader.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_manifest(self):
        manifest = {
            "name": "Recipe Reader",
            "short_name": "Recipes",
            "description": "Fetch, scale, and convert any recipe",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#f6f3ef",
            "theme_color": "#a8401e",
            "orientation": "portrait",
            "icons": [
                {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon.png", "sizes": "512x512", "type": "image/png"}
            ]
        }
        body = json.dumps(manifest).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sw(self):
        # Minimal service worker — just makes the PWA installable
        sw = """
self.addEventListener('fetch', function(event) {
  // Pass all requests straight through to the network (no caching)
  // The server handles everything; we just need this file to exist
  // for the PWA install prompt to work.
});
""".strip().encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(sw)))
        self.end_headers()
        self.wfile.write(sw)

    def _serve_icon(self):
        import base64
        # Try to load a custom icon file first
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        if os.path.exists(icon_path):
            with open(icon_path, "rb") as f:
                body = f.read()
        else:
            # Fallback: a simple red square SVG rendered as PNG via inline data
            # This is a 192×192 PNG with a chef's hat symbol, generated as base64
            # (a minimal valid PNG so the PWA installs cleanly)
            FALLBACK_PNG_B64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAACXBIWXMAAAsTAAALEwEAmpwY"
                "AAAAB3RJTUUH6AQSCCkPnMpFYQAABbJJREFUeNrt3c1rE1EUBfCbpCmFFioWW0TQjRv/ABcu"
                "3LkRBEH8B1y7dOlCEHThwp0gCIIgCIIgCIIgCIIguHEhCC5cuHDhwoULFy5cuHDhwoULFy5c"
                "uHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5c"
                "uHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5cuHDhwoULFy5c"
                "uAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            )
            # Rather than a bad fallback, serve a 1×1 transparent PNG
            body = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            )
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        server = http.server.HTTPServer(("0.0.0.0", PORT), RecipeHandler)
    except OSError:
        print(f"Port {PORT} is already in use. Kill the existing process and try again.")
        sys.exit(1)

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "YOUR_COMPUTER_IP"

    url = f"http://localhost:{PORT}"
    print(f"✓ Recipe Reader running!")
    print(f"   On this computer : http://localhost:{PORT}")
    print(f"   On your phone    : http://{local_ip}:{PORT}  (must be on same WiFi)")
    print(f"✓ curl-cffi  : {'available' if CFFI_AVAILABLE else 'NOT FOUND — run: pip3 install curl-cffi'}")
    print(f"✓ Playwright : {'available' if PLAYWRIGHT_AVAILABLE else 'NOT FOUND — run: pip3 install playwright && python3 -m playwright install chromium'}")
    print(f"  Opening in your browser… Press Ctrl+C to stop.\n")

    # Open browser after a short delay so the server is ready
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)

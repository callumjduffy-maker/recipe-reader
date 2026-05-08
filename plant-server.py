#!/usr/bin/env python3
"""
Plant Care — server with Claude AI plant analysis.

Storage:
  - Supabase PostgreSQL when DATABASE_URL is set  (Railway / production)
  - Local plants.json otherwise                   (local development)

Usage:
    python3 plant-server.py
    DATABASE_URL=postgresql://... ANTHROPIC_API_KEY=sk-ant-... python3 plant-server.py

Then visit http://localhost:8766
"""

import http.server
import urllib.parse
import json
import os
import sys
import uuid
import base64
import threading
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    import pillow_heif
    from PIL import Image
    import io as _io
    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

PORT = int(os.environ.get("PORT", 8766))
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "plants.json"
USE_DB = bool(os.environ.get("DATABASE_URL"))


# ── Database ───────────────────────────────────────────────────────────────────

def _db_conn():
    url = os.environ.get("DATABASE_URL", "")
    if "supabase" in url and "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg2.connect(url)


def init_db():
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plants (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            image_data       TEXT DEFAULT '',
            image_type       TEXT DEFAULT 'image/jpeg',
            added            TEXT DEFAULT '',
            analysis         JSONB DEFAULT '{}',
            last_watered     TEXT DEFAULT '',
            next_watering    TEXT DEFAULT '',
            watering_history JSONB DEFAULT '[]',
            created_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()
    conn.close()
    print("  Database ready.")


def _row_to_plant(row):
    b64  = row.get("image_data") or ""
    mime = row.get("image_type") or "image/jpeg"
    return {
        "id":             row["id"],
        "name":           row["name"],
        "imageDataUrl":   f"data:{mime};base64,{b64}" if b64 else "",
        "added":          row.get("added") or "",
        "analysis":       row.get("analysis") or {},
        "lastWatered":    row.get("last_watered") or "",
        "nextWatering":   row.get("next_watering") or "",
        "wateringHistory":row.get("watering_history") or [],
    }


# ── Storage API ────────────────────────────────────────────────────────────────

def plants_load():
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM plants ORDER BY created_at ASC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return [_row_to_plant(r) for r in rows]
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def plant_get(plant_id):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM plants WHERE id = %s", (plant_id,))
        row = cur.fetchone()
        conn.close()
        return _row_to_plant(dict(row)) if row else None
    return next((p for p in plants_load() if p["id"] == plant_id), None)


def plant_get_image(plant_id):
    """Return (b64, media_type) for the stored image."""
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("SELECT image_data, image_type FROM plants WHERE id = %s", (plant_id,))
        row = cur.fetchone()
        conn.close()
        return (row[0], row[1]) if row else (None, None)
    plant = plant_get(plant_id)
    if not plant or not plant.get("imageDataUrl"):
        return None, None
    try:
        header, b64 = plant["imageDataUrl"].split(",", 1)
        mime = header.split(";")[0].split(":")[1]
        return b64, mime
    except Exception:
        return None, None


def plant_insert(plant_id, name, b64, media_type, today, analysis, next_watering):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO plants
              (id, name, image_data, image_type, added, analysis,
               last_watered, next_watering, watering_history)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (plant_id, name, b64, media_type, today,
              json.dumps(analysis), today, next_watering, json.dumps([today])))
        conn.commit()
        conn.close()
        return plant_get(plant_id)
    plant = {
        "id": plant_id, "name": name,
        "imageDataUrl": f"data:{media_type};base64,{b64}",
        "added": today,
        "analysis": {**analysis, "analyzedAt": datetime.now().isoformat()},
        "lastWatered": today, "nextWatering": next_watering,
        "wateringHistory": [today],
    }
    plants = plants_load()
    plants.append(plant)
    DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    return plant


def plant_update_analysis(plant_id, analysis, next_watering):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE plants SET analysis=%s, next_watering=%s WHERE id=%s",
                    (json.dumps(analysis), next_watering, plant_id))
        conn.commit()
        conn.close()
    else:
        plants = plants_load()
        for p in plants:
            if p["id"] == plant_id:
                p["analysis"] = analysis
                p["nextWatering"] = next_watering
                break
        DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    return plant_get(plant_id)


def plant_update_photo(plant_id, b64, media_type, analysis, next_watering):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("""UPDATE plants
            SET image_data=%s, image_type=%s, analysis=%s, next_watering=%s
            WHERE id=%s""",
            (b64, media_type, json.dumps(analysis), next_watering, plant_id))
        conn.commit()
        conn.close()
    else:
        plants = plants_load()
        for p in plants:
            if p["id"] == plant_id:
                p["imageDataUrl"] = f"data:{media_type};base64,{b64}"
                p["analysis"] = analysis
                p["nextWatering"] = next_watering
                break
        DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    return plant_get(plant_id)


def plant_water(plant_id, today, next_watering, history):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("""UPDATE plants
            SET last_watered=%s, next_watering=%s, watering_history=%s
            WHERE id=%s""",
            (today, next_watering, json.dumps(history), plant_id))
        conn.commit()
        conn.close()
    else:
        plants = plants_load()
        for p in plants:
            if p["id"] == plant_id:
                p["lastWatered"] = today
                p["nextWatering"] = next_watering
                p["wateringHistory"] = history
                break
        DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    return plant_get(plant_id)


def plant_update_name(plant_id, name):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE plants SET name=%s WHERE id=%s", (name, plant_id))
        conn.commit()
        conn.close()
    else:
        plants = plants_load()
        for p in plants:
            if p["id"] == plant_id:
                p["name"] = name
                break
        DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")
    return plant_get(plant_id)


def plant_delete(plant_id):
    if USE_DB:
        conn = _db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM plants WHERE id=%s", (plant_id,))
        conn.commit()
        conn.close()
    else:
        plants = [p for p in plants_load() if p["id"] != plant_id]
        DATA_FILE.write_text(json.dumps(plants, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Image helpers ──────────────────────────────────────────────────────────────

SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def normalise_image(b64, media_type):
    if media_type in SUPPORTED_MEDIA_TYPES:
        return b64, media_type
    if not HEIF_AVAILABLE:
        raise RuntimeError(
            f"Image format '{media_type}' is not supported. "
            "Install support with: pip3 install pillow-heif Pillow"
        )
    img = Image.open(_io.BytesIO(base64.b64decode(b64)))
    buf = _io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


# ── AI analysis ────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = (
    "You are an expert botanist and plant care specialist. "
    "Analyze this houseplant photo carefully and respond with ONLY valid JSON "
    "(no markdown fences, no extra text — just the raw JSON object):\n"
    "{\n"
    '  "plantType": "common name of the plant, e.g. Monstera Deliciosa, or Unknown Plant",\n'
    '  "healthScore": <integer 1-10, where 1=dying, 10=thriving>,\n'
    '  "healthSummary": "2-3 sentence description of the plant\'s current condition",\n'
    '  "needs": ["specific things this plant currently needs"],\n'
    '  "waterFrequencyDays": <integer: recommended days between waterings>,\n'
    '  "tips": ["specific care tip 1", "care tip 2", "care tip 3"],\n'
    '  "warnings": ["any urgent issues to address right now, or empty array if none"]\n'
    "}"
)


def _parse_json_response(text):
    text = text.strip()
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                text = chunk
                break
    return json.loads(text)


def analyze_with_gemini(image_b64, media_type="image/jpeg"):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    if not GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. Run: pip3 install google-genai")
    client = google_genai.Client(api_key=key)
    response = client.models.generate_content(
        model="gemini-2.0-flash-lite",
        contents=[
            google_genai_types.Part.from_bytes(
                data=base64.b64decode(image_b64), mime_type=media_type),
            ANALYSIS_PROMPT,
        ],
    )
    return _parse_json_response(response.text)


def analyze_with_anthropic(image_b64, media_type="image/jpeg"):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    if not ANTHROPIC_AVAILABLE:
        raise RuntimeError("anthropic package not installed. Run: pip3 install anthropic")
    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": ANALYSIS_PROMPT},
        ]}]
    )
    return _parse_json_response(response.content[0].text)


def analyze_plant_image(image_b64, media_type="image/jpeg"):
    if os.environ.get("GEMINI_API_KEY"):
        return analyze_with_gemini(image_b64, media_type)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        return analyze_with_anthropic(image_b64, media_type)
    else:
        raise RuntimeError(
            "No API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY."
        )


def compute_next_watering(last_watered_str, frequency_days):
    if not last_watered_str:
        return date.today().isoformat()
    last = date.fromisoformat(last_watered_str)
    return (last + timedelta(days=int(frequency_days))).isoformat()


# ── PWA icon ───────────────────────────────────────────────────────────────────

_ICON_CACHE = None

def _get_icon():
    global _ICON_CACHE
    if _ICON_CACHE:
        return _ICON_CACHE
    try:
        from PIL import Image, ImageDraw
        import io
        s = 512
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # Dark green circle background
        d.ellipse([0, 0, s, s], fill=(45, 106, 79, 255))
        # Terracotta pot
        d.rectangle([s//2 - 65, s*11//16, s//2 + 65, s - 55], fill=(180, 100, 55, 255))
        d.ellipse([s//2 - 75, s*11//16 - 18, s//2 + 75, s*11//16 + 18], fill=(155, 82, 42, 255))
        # Stem
        d.rectangle([s//2 - 10, s*5//16, s//2 + 10, s*11//16], fill=(88, 166, 57, 255))
        # Left leaf
        d.ellipse([s//5, s*3//8, s//2 + 25, s*5//8], fill=(95, 190, 65, 255))
        # Right leaf
        d.ellipse([s//2 - 25, s//4, s*4//5, s//2 + 30], fill=(120, 210, 80, 255))
        # Top leaf
        d.ellipse([s//2 - 50, s//8, s//2 + 50, s*7//16], fill=(160, 225, 95, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _ICON_CACHE = buf.getvalue()
    except Exception:
        _ICON_CACHE = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
    return _ICON_CACHE


# ── HTTP handler ───────────────────────────────────────────────────────────────

class PlantHandler(http.server.BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._serve_file("plant-care.html", "text/html; charset=utf-8")
        elif path == "/api/plants":
            self._json_response(200, plants_load())
        elif path == "/manifest.json":
            self._serve_manifest()
        elif path == "/icon.png":
            self._serve_icon()
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if path == "/api/plants":
            self._handle_add_plant()
        elif len(parts) == 5 and parts[1:3] == ["api", "plants"] and parts[4] == "analyze":
            self._handle_analyze_plant(parts[3])
        elif len(parts) == 5 and parts[1:3] == ["api", "plants"] and parts[4] == "photo":
            self._handle_update_photo(parts[3])
        else:
            self.send_error(404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "plants"] and parts[4] == "water":
            self._handle_water_plant(parts[3])
        elif len(parts) == 4 and parts[1:3] == ["api", "plants"]:
            self._handle_update_plant(parts[3])
        else:
            self.send_error(404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "plants"]:
            self._handle_delete_plant(parts[3])
        else:
            self.send_error(404)

    # ── Handlers ───────────────────────────────────────────────────────────────

    def _handle_add_plant(self):
        body = self._read_json_body()
        if body is None:
            return
        name = (body.get("name") or "My Plant").strip()
        image_data = body.get("imageData", "")
        if not image_data:
            self._json_error(400, "imageData is required")
            return
        try:
            header, b64 = image_data.split(",", 1)
            media_type = header.split(";")[0].split(":")[1]
        except Exception:
            self._json_error(400, "Invalid imageData format")
            return
        try:
            b64, media_type = normalise_image(b64, media_type)
        except RuntimeError as e:
            self._json_error(400, str(e))
            return
        try:
            analysis = analyze_plant_image(b64, media_type)
        except RuntimeError as e:
            self._json_error(503, str(e))
            return
        except Exception as e:
            self._json_error(500, f"Analysis failed: {e}")
            return
        today = date.today().isoformat()
        freq = int(analysis.get("waterFrequencyDays", 7))
        analysis["analyzedAt"] = datetime.now().isoformat()
        plant = plant_insert(str(uuid.uuid4()), name, b64, media_type,
                             today, analysis, compute_next_watering(today, freq))
        self._json_response(201, plant)

    def _handle_analyze_plant(self, plant_id):
        b64, media_type = plant_get_image(plant_id)
        if b64 is None:
            self._json_error(404, "Plant or image not found")
            return
        try:
            analysis = analyze_plant_image(b64, media_type)
        except RuntimeError as e:
            self._json_error(503, str(e))
            return
        except Exception as e:
            self._json_error(500, f"Analysis failed: {e}")
            return
        analysis["analyzedAt"] = datetime.now().isoformat()
        plant = plant_get(plant_id)
        freq = int(analysis.get("waterFrequencyDays", 7))
        updated = plant_update_analysis(
            plant_id, analysis,
            compute_next_watering(plant.get("lastWatered") if plant else None, freq))
        self._json_response(200, updated)

    def _handle_update_photo(self, plant_id):
        body = self._read_json_body()
        if body is None:
            return
        image_data = body.get("imageData", "")
        if not image_data:
            self._json_error(400, "imageData is required")
            return
        try:
            header, b64 = image_data.split(",", 1)
            media_type = header.split(";")[0].split(":")[1]
        except Exception:
            self._json_error(400, "Invalid imageData format")
            return
        try:
            b64, media_type = normalise_image(b64, media_type)
        except RuntimeError as e:
            self._json_error(400, str(e))
            return
        try:
            analysis = analyze_plant_image(b64, media_type)
        except RuntimeError as e:
            self._json_error(503, str(e))
            return
        except Exception as e:
            self._json_error(500, f"Analysis failed: {e}")
            return
        analysis["analyzedAt"] = datetime.now().isoformat()
        plant = plant_get(plant_id)
        freq = int(analysis.get("waterFrequencyDays", 7))
        updated = plant_update_photo(
            plant_id, b64, media_type, analysis,
            compute_next_watering(plant.get("lastWatered") if plant else None, freq))
        self._json_response(200, updated)

    def _handle_water_plant(self, plant_id):
        plant = plant_get(plant_id)
        if not plant:
            self._json_error(404, "Plant not found")
            return
        today = date.today().isoformat()
        freq = int((plant.get("analysis") or {}).get("waterFrequencyDays", 7))
        history = plant.get("wateringHistory", [])
        if today not in history:
            history.append(today)
        updated = plant_water(plant_id, today,
                              compute_next_watering(today, freq), history[-30:])
        self._json_response(200, updated)

    def _handle_update_plant(self, plant_id):
        body = self._read_json_body()
        if body is None:
            return
        if "name" in body:
            name = str(body["name"]).strip()
            if name:
                updated = plant_update_name(plant_id, name)
                self._json_response(200, updated)
                return
        self._json_error(400, "Nothing to update")

    def _handle_delete_plant(self, plant_id):
        if not plant_get(plant_id):
            self._json_error(404, "Plant not found")
            return
        plant_delete(plant_id)
        self._json_response(200, {"deleted": plant_id})

    # ── Static ─────────────────────────────────────────────────────────────────

    def _serve_file(self, filename, content_type):
        path = BASE_DIR / filename
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, f"{filename} not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_manifest(self):
        manifest = {
            "name": "Plant Care", "short_name": "Plants",
            "description": "AI-powered houseplant care tracker",
            "start_url": "/", "display": "standalone",
            "background_color": "#f0f7ee", "theme_color": "#2d6a4f",
            "icons": [
                {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
        body = json.dumps(manifest).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_icon(self):
        body = _get_icon()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_error(400, "Empty request body")
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body")
            return None

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, msg):
        self._json_response(code, {"error": msg})

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")


# ── Main ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if USE_DB:
        if not PSYCOPG2_AVAILABLE:
            print("  Error: psycopg2 not installed. Run: pip3 install psycopg2-binary")
            sys.exit(1)
        try:
            init_db()
        except Exception as e:
            print(f"  Error connecting to database: {e}")
            sys.exit(1)
    else:
        print("  No DATABASE_URL set — using local plants.json")

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  Warning: No AI API key set. Analysis will fail.")
        print("           Set ANTHROPIC_API_KEY or GEMINI_API_KEY.\n")

    try:
        server = http.server.HTTPServer(("0.0.0.0", PORT), PlantHandler)
    except OSError:
        print(f"Port {PORT} is in use.")
        sys.exit(1)

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "YOUR_COMPUTER_IP"

    provider = "Anthropic" if os.environ.get("ANTHROPIC_API_KEY") else \
               "Gemini" if os.environ.get("GEMINI_API_KEY") else "NOT SET"

    print(f"Plant Care running!")
    print(f"   On this computer : http://localhost:{PORT}")
    print(f"   On your phone    : http://{local_ip}:{PORT}  (same WiFi)")
    print(f"   Storage          : {'Supabase' if USE_DB else 'Local file'}")
    print(f"   AI provider      : {provider}")
    print(f"\n  Opening browser… Press Ctrl+C to stop.\n")

    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)

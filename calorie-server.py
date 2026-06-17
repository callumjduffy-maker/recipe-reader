#!/usr/bin/env python3
"""
Calorie Tracker — local server with AI food recognition.

Usage:
    python3 calorie-server.py

Then visit http://localhost:8766 in your browser.
Set ANTHROPIC_API_KEY in your environment first.
"""

import http.server
import urllib.parse
import json
import base64
import sys
import os
import uuid
import webbrowser
import threading
from datetime import date, datetime

import anthropic
from PIL import Image
import io
from pillow_heif import register_heif_opener
register_heif_opener()

PORT = int(os.environ.get("PORT", 8766))
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "calorie-data.json")

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"log": {}, "goal": 2000, "water": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def analyze_food_image(image_data: str, media_type: str) -> dict:
    """Send image to Claude for calorie/macro estimation."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "Analyze this food image and estimate the nutritional content. "
                        "Respond with ONLY a JSON object (no markdown, no extra text) in this exact format:\n"
                        '{"name": "Food name", "calories": 350, "protein": 15, "carbs": 45, "fat": 10, '
                        '"fiber": 3, "serving": "1 cup (240g)", "confidence": "high", '
                        '"ingredients": [{"name": "Ingredient", "amount": "100g", "calories": 200}]}\n\n'
                        "All numeric values should be integers. "
                        "Confidence should be 'high', 'medium', or 'low'. "
                        "List the main ingredients with estimated amounts and calories. "
                        "If you cannot identify food, return: "
                        '{"name": "Unknown food", "calories": 0, "protein": 0, "carbs": 0, "fat": 0, '
                        '"fiber": 0, "serving": "unknown", "confidence": "low", "ingredients": []}'
                    )
                }
            ]
        }]
    )
    text = next(b.text for b in response.content if b.type == "text")
    # Strip any accidental markdown fences
    text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(text)

def analyze_food_text(description: str) -> dict:
    """Use Claude to estimate calories from a text description."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Estimate the nutritional content for: {description}\n\n"
                "Respond with ONLY a JSON object (no markdown, no extra text) in this exact format:\n"
                '{"name": "Food name", "calories": 350, "protein": 15, "carbs": 45, "fat": 10, '
                '"fiber": 3, "serving": "1 serving", "confidence": "medium", '
                '"ingredients": [{"name": "Ingredient", "amount": "100g", "calories": 200}]}\n\n'
                "All numeric values should be integers. "
                "Confidence should be 'high', 'medium', or 'low'. "
                "List the main ingredients with estimated amounts and calories."
            )
        }]
    )
    text = next(b.text for b in response.content if b.type == "text")
    text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(text)


def _generate_bear_icon(size: int = 192) -> bytes:
    """Draw a cute bear face as a PNG icon."""
    from PIL import ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), "#FFF8F0")
    d = ImageDraw.Draw(img)

    def p(x, y=None):
        if y is None:
            return tuple(int(v * s) for v in x)
        return int(x * s), int(y * s)

    # Orange circle background
    d.ellipse([p(0.04, 0.04), p(0.96, 0.96)], fill="#E8704A")
    # Outer ears
    d.ellipse([p(0.06, 0.05), p(0.36, 0.35)], fill="#7A4F2D")
    d.ellipse([p(0.64, 0.05), p(0.94, 0.35)], fill="#7A4F2D")
    # Inner ears
    d.ellipse([p(0.11, 0.10), p(0.31, 0.30)], fill="#C4916A")
    d.ellipse([p(0.69, 0.10), p(0.89, 0.30)], fill="#C4916A")
    # Head
    d.ellipse([p(0.10, 0.18), p(0.90, 0.92)], fill="#8B5733")
    # Muzzle
    d.ellipse([p(0.32, 0.60), p(0.68, 0.86)], fill="#C4916A")
    # Eyes
    d.ellipse([p(0.22, 0.38), p(0.42, 0.56)], fill="#2C1A0E")
    d.ellipse([p(0.58, 0.38), p(0.78, 0.56)], fill="#2C1A0E")
    # Eye shines
    d.ellipse([p(0.26, 0.40), p(0.33, 0.47)], fill="white")
    d.ellipse([p(0.62, 0.40), p(0.69, 0.47)], fill="white")
    # Nose
    d.ellipse([p(0.41, 0.61), p(0.59, 0.70)], fill="#2C1A0E")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class CalorieHandler(http.server.BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html", ""):
            self._serve_html()
        elif path == "/manifest.json":
            self._serve_manifest()
        elif path == "/sw.js":
            self._serve_sw()
        elif path == "/icon.png":
            self._serve_icon()
        elif path == "/api/log":
            day = (params.get("date") or [str(date.today())])[0]
            data = load_data()
            entries = data["log"].get(day, [])
            self._json_ok({"entries": entries, "date": day})
        elif path == "/api/goal":
            data = load_data()
            self._json_ok({"goal": data.get("goal", 2000)})
        elif path == "/api/water":
            day = (params.get("date") or [str(date.today())])[0]
            data = load_data()
            ml = data.get("water", {}).get(day, 0)
            self._json_ok({"ml": ml, "date": day})
        elif path == "/api/history":
            data = load_data()
            summary = []
            for d, entries in sorted(data["log"].items(), reverse=True)[:30]:
                total = sum(e.get("calories", 0) for e in entries)
                summary.append({"date": d, "calories": total, "entries": len(entries)})
            self._json_ok({"history": summary, "goal": data.get("goal", 2000)})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/api/analyze":
            try:
                payload = json.loads(body)
            except Exception:
                self._json_error(400, "Invalid JSON")
                return

            try:
                if "image" in payload:
                    # base64 image: "data:image/jpeg;base64,..."
                    img = payload["image"]
                    if img.startswith("data:"):
                        header, data = img.split(",", 1)
                        media_type = header.split(";")[0].split(":")[1]
                    else:
                        data = img
                        media_type = "image/jpeg"
                    # Normalize to Claude-supported types
                    type_map = {
                        "image/jpg": "image/jpeg",
                        "image/jfif": "image/jpeg",
                        "image/pjpeg": "image/jpeg",
                        "image/tiff": "image/png",
                        "image/bmp": "image/png",
                        "image/heic": "image/jpeg",
                        "image/heif": "image/jpeg",
                    }
                    media_type = type_map.get(media_type, media_type)
                    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                        media_type = "image/jpeg"
                    # Normalize to JPEG, resize to safe dimensions
                    try:
                        raw = base64.b64decode(data)
                        pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
                        pil_img.thumbnail((1568, 1568), Image.LANCZOS)
                        buf = io.BytesIO()
                        pil_img.save(buf, format="JPEG", quality=85)
                        data = base64.b64encode(buf.getvalue()).decode()
                        media_type = "image/jpeg"
                    except Exception as pil_err:
                        print(f"  PIL warning (sending raw): {pil_err}")
                    result = analyze_food_image(data, media_type)
                elif "description" in payload:
                    result = analyze_food_text(payload["description"])
                else:
                    self._json_error(400, "Provide 'image' or 'description'")
                    return
                self._json_ok(result)
            except anthropic.AuthenticationError:
                self._json_error(401, "Invalid ANTHROPIC_API_KEY. Set it in your environment.")
            except json.JSONDecodeError as e:
                self._json_error(500, f"Claude returned unexpected format: {e}")
            except Exception as e:
                self._json_error(500, str(e))

        elif path == "/api/log":
            try:
                payload = json.loads(body)
            except Exception:
                self._json_error(400, "Invalid JSON")
                return
            data = load_data()
            day = payload.get("date", str(date.today()))
            if day not in data["log"]:
                data["log"][day] = []
            entry = {
                "id": str(uuid.uuid4())[:8],
                "name": payload.get("name", "Unknown"),
                "calories": int(payload.get("calories", 0)),
                "protein": int(payload.get("protein", 0)),
                "carbs": int(payload.get("carbs", 0)),
                "fat": int(payload.get("fat", 0)),
                "fiber": int(payload.get("fiber", 0)),
                "serving": payload.get("serving", "1 serving"),
                "ingredients": payload.get("ingredients", []),
                "time": datetime.now().strftime("%H:%M"),
                "note": payload.get("note", ""),
            }
            data["log"][day].append(entry)
            save_data(data)
            self._json_ok({"success": True, "entry": entry})

        elif path == "/api/log/delete":
            try:
                payload = json.loads(body)
            except Exception:
                self._json_error(400, "Invalid JSON")
                return
            data = load_data()
            day = payload.get("date", str(date.today()))
            entry_id = payload.get("id")
            if day in data["log"]:
                data["log"][day] = [e for e in data["log"][day] if e["id"] != entry_id]
            save_data(data)
            self._json_ok({"success": True})

        elif path == "/api/goal":
            try:
                payload = json.loads(body)
                goal = int(payload.get("goal", 2000))
            except Exception:
                self._json_error(400, "Invalid JSON or goal value")
                return
            data = load_data()
            data["goal"] = goal
            save_data(data)
            self._json_ok({"success": True, "goal": goal})

        elif path == "/api/water":
            try:
                payload = json.loads(body)
            except Exception:
                self._json_error(400, "Invalid JSON")
                return
            data = load_data()
            day = payload.get("date", str(date.today()))
            if "water" not in data:
                data["water"] = {}
            ml = int(payload.get("ml", 0))
            delta = payload.get("delta", False)
            if delta:
                data["water"][day] = data["water"].get(day, 0) + ml
            else:
                data["water"][day] = ml
            save_data(data)
            self._json_ok({"success": True, "ml": data["water"][day]})

        else:
            self.send_error(404)

    def _serve_html(self):
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calorie-tracker.html")
        try:
            with open(html_path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, "calorie-tracker.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_manifest(self):
        manifest = {
            "name": "Calorie Tracker",
            "short_name": "Calories",
            "description": "AI-powered calorie tracking with photo recognition",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#FFF8F0",
            "theme_color": "#E8704A",
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
        sw = b"self.addEventListener('fetch', function(e){});"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(sw)))
        self.end_headers()
        self.wfile.write(sw)

    def _serve_icon(self):
        body = _generate_bear_icon(192)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json_ok(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY not set — AI analysis will fail.")
        print("   Run: export ANTHROPIC_API_KEY=your-key-here\n")

    try:
        server = http.server.HTTPServer(("0.0.0.0", PORT), CalorieHandler)
    except OSError:
        print(f"Port {PORT} is in use. Kill the existing process and try again.")
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
    print("✓ Calorie Tracker running!")
    print(f"  On this computer : http://localhost:{PORT}")
    print(f"  On your phone    : http://{local_ip}:{PORT}  (same WiFi)")
    print(f"  API key          : {'set ✓' if api_key else 'NOT SET ⚠️'}")
    print("  Opening in browser… Press Ctrl+C to stop.\n")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)

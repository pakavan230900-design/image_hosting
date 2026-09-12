import http.server
import io
import os
import uuid
import json
import logging
import mimetypes

from pathlib import Path
from PIL import Image, UnidentifiedImageError

PORT = 8000
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
MAX_FILE_SIZE = 5 * 1024 * 1024 
ALLOWED_PIL_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF"}
IMAGES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


logging.basicConfig(
    filename=LOGS_DIR / "app.log",
    level=logging.INFO,
    format="[%(asctime)s] Дія: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger().addHandler(logging.StreamHandler())


class ImageHostingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._handle_index()
        elif self.path in ("/images", "/images/"):
            self._handle_image_list()
        elif self.path.startswith("/images/"):
            self._handle_image_serve()
        elif self.path.startswith("/static/"):
            self._handle_static()
        else:
            self._send_error(404, "Маршрут не знайдено")

    def do_POST(self):
        if self.path == "/upload":
            self._handle_upload()
        else:
            self._send_error(404, "Маршрут не знайдено")

    def _handle_index(self):
        self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

    def _handle_static(self):
        filename = self.path[len("/static/"):]
        requested_path = (STATIC_DIR / filename).resolve()
        if STATIC_DIR.resolve() not in requested_path.parents:
            self._send_error(403, "Доступ заборонено")
            return
        content_type = mimetypes.guess_type(str(requested_path))[0] or "application/octet-stream"
        self._serve_file(requested_path, content_type)

    def _handle_image_list(self):
        files = sorted(p.name for p in IMAGES_DIR.iterdir() if p.is_file())
        if files:
            items = "\n".join(
                f'<li><a href="/images/{name}" target="_blank">{name}</a></li>'
                for name in files)
        else:
            items = "<li>Поки що немає завантажених зображень.</li>"
        template = (STATIC_DIR / "catalog.html").read_text(encoding="utf-8")
        html = template.replace("<!--ITEMS-->", items)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json_error(400, "Очікується Content-Type: multipart/form-data")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json_error(400, "Некоректний Content-Length")
            return
        if content_length <= 0:
            self._send_json_error(400, "Порожнє тіло запиту")
            return
        if content_length > MAX_FILE_SIZE:
            logging.info(
                f"Помилка: файл перевищує ліміт розміру ({content_length} байт).")
            self._send_json_error(400, "Файл перевищує максимальний розмір 5 МБ")
            return
        
        body = self.rfile.read(content_length)
        filename, file_bytes = self._parse_multipart(body, content_type)

        if filename is None:
            logging.info("Помилка: не вдалося розпізнати файл у запиті.")
            self._send_json_error(400, "Не вдалося знайти файл у запиті")
            return

        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            logging.info(f"Помилка: непідтримуваний формат файлу ({filename}).")
            self._send_json_error(400, f"Непідтримуваний формат файлу: {ext}")
            return

        if len(file_bytes) > MAX_FILE_SIZE:
            logging.info(f"Помилка: файл {filename} перевищує ліміт розміру.")
            self._send_json_error(400, "Файл перевищує максимальний розмір 5 МБ")
            return

        try:
            with Image.open(io.BytesIO(file_bytes)) as img:
                img.verify()
                actual_format = img.format
        except (UnidentifiedImageError, OSError):
            logging.info(f"Помилка: файл {filename} не є коректним зображенням.")
            self._send_json_error(400, "Файл не є коректним зображенням")
            return

        if actual_format != ALLOWED_PIL_FORMATS.get(ext):
            logging.info(
                f"Помилка: вміст файлу {filename} не відповідає розширенню "
                f"(розширення {ext}, реальний формат {actual_format}).")
            self._send_json_error(400, "Вміст файлу не відповідає його розширенню")
            return

        unique_name = f"{uuid.uuid4().hex}{ext}"
        save_path = IMAGES_DIR / unique_name
        with open(save_path, "wb") as f:
            f.write(file_bytes)

        logging.info(f"Успіх: зображення {unique_name} завантажено.")

        self._send_json(200, {
            "message": "Файл успішно завантажено",
            "filename": unique_name,
            "url": f"/images/{unique_name}"})

    def _handle_image_serve(self):
        filename = self.path[len("/images/"):]
        requested_path = (IMAGES_DIR / filename).resolve()

        if IMAGES_DIR.resolve() not in requested_path.parents:
            self._send_error(403, "Доступ заборонено")
            return

        content_type = mimetypes.guess_type(str(requested_path))[0] or "application/octet-stream"
        self._serve_file(requested_path, content_type)

    def _send_error(self, status: int, message: str):

        body = (f"<html><body><h1>{status}</h1><p>{message}</p></body></html>").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str):
        if not path.is_file():
            self._send_error(404, "Файл не знайдено")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as f:
            self.wfile.write(f.read())

    @staticmethod
    def _parse_multipart(body: bytes, content_type: str):
        boundary = None
        for piece in content_type.split(";"):
            piece = piece.strip()
            if piece.startswith("boundary="):
                boundary = piece[len("boundary="):].strip('"')
        if boundary is None:
            return None, None

        boundary_bytes = ("--" + boundary).encode()
        parts = body.split(boundary_bytes)

        for part in parts:
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if b"filename=" not in part:
                continue

            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue

            headers_text = part[:header_end].decode(errors="ignore")
            file_data = part[header_end + 4:]
            if file_data.endswith(b"\r\n"):
                file_data = file_data[:-2]

            filename = None
            for line in headers_text.split("\r\n"):
                if "filename=" in line:
                    filename = line.split("filename=")[1].strip().strip('"')
                    break

            if filename:
                return filename, file_data

        return None, None

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def log_message(self, format, *args):
        pass

def run():
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ImageHostingHandler) as httpd:
        print(f"Сервер запущено на порту {PORT} (http://localhost:{PORT})")
        httpd.serve_forever()


if __name__ == "__main__":
    run()
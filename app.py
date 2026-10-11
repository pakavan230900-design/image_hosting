import http.server
import io
import os
import uuid
import json
import html
import logging
import mimetypes
import time

from pathlib import Path
from urllib.parse import urlparse, parse_qs
from PIL import Image, UnidentifiedImageError
import psycopg2

PORT = 8000
BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
LOGS_DIR = BASE_DIR / "logs"
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
MAX_FILE_SIZE = 5 * 1024 * 1024
PER_PAGE = 10
ALLOWED_PIL_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF"}
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "images_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "host": os.getenv("DB_HOST", "db"),
    "port": os.getenv("DB_PORT", "5432"),
}
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_type TEXT NOT NULL
);
"""
IMAGES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "app.log",
    level=logging.INFO,
    format="[%(asctime)s] Дія: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger().addHandler(logging.StreamHandler())


def get_db_connection():
    """Нове з'єднання з PostgreSQL для кожного запиту (безпечно для потоків)."""
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


def init_db(retries: int = 10, delay: int = 2):
    """Перевіряє з'єднання з БД і створює таблицю images, якщо її ще немає."""
    for attempt in range(1, retries + 1):
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(CREATE_TABLE_SQL)
                conn.commit()
            finally:
                conn.close()
            logging.info("Успіх: з'єднання з базою даних встановлено, таблиця images готова.")
            return
        except psycopg2.Error as e:
            logging.error(
                f"Помилка: не вдалося підключитися до бази даних "
                f"(спроба {attempt}/{retries}): {e}")
            time.sleep(delay)
    logging.error("Помилка: база даних недоступна, сервер запускається без неї.")


def save_metadata(cursor, filename, original_name, size, file_type):
    """Додає запис про зображення і повертає його id."""
    query = """
    INSERT INTO images (filename, original_name, size, file_type)
    VALUES (%s, %s, %s, %s)
    RETURNING id
    """
    cursor.execute(query, (filename, original_name, size, file_type))
    return cursor.fetchone()[0]


class ImageHostingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._handle_index()
        elif path == "/images-list":
            self._handle_images_list()
        elif path in ("/images", "/images/"):
            self._redirect("/images-list")
        elif self.path.startswith("/images/"):
            self._handle_image_serve()
        elif self.path.startswith("/static/"):
            self._handle_static()
        elif path == "/favicon.ico":
            # Браузер запитує іконку автоматично; відповідаємо "немає вмісту",
            # щоб не засмічувати лог помилками 404.
            self.send_response(204)
            self.end_headers()
        else:
            logging.info(f"Помилка: маршрут GET {self.path} не знайдено.")
            self._send_error(404, "Маршрут не знайдено")

    def do_POST(self):
        if self.path == "/upload":
            self._handle_upload()
        else:
            logging.info(f"Помилка: маршрут POST {self.path} не знайдено.")
            self._send_error(404, "Маршрут не знайдено")

    def _handle_index(self):
        self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

    def _handle_static(self):
        filename = self.path[len("/static/"):]
        requested_path = (STATIC_DIR / filename).resolve()
        if STATIC_DIR.resolve() not in requested_path.parents:
            logging.info(f"Помилка: спроба доступу за межі дозволеної папки ({self.path}).")
            self._send_error(403, "Доступ заборонено")
            return
        content_type = mimetypes.guess_type(str(requested_path))[0] or "application/octet-stream"
        self._serve_file(requested_path, content_type)

    def _handle_images_list(self):
        page = self._get_page_number()
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM images")
                    total = cur.fetchone()[0]

                    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
                    page = min(page, total_pages)
                    offset = (page - 1) * PER_PAGE

                    cur.execute(
                        "SELECT id, filename, original_name, size, upload_time, file_type "
                        "FROM images ORDER BY upload_time DESC, id DESC "
                        "LIMIT %s OFFSET %s",
                        (PER_PAGE, offset))
                    rows = cur.fetchall()
            finally:
                conn.close()
        except psycopg2.Error as e:
            logging.error(f"Помилка: не вдалося отримати список зображень з бази даних. Причина: {e}")
            self._send_error(500, "Помилка бази даних, спробуйте пізніше")
            return

        if rows:
            table_rows = "\n".join(self._render_image_row(row) for row in rows)
        else:
            table_rows = (
                '<tr><td colspan="5" class="empty">Немає завантажених зображень</td></tr>')

        template = (STATIC_DIR / "images_list.html").read_text(encoding="utf-8")
        page_html = (template
                     .replace("<!--ROWS-->", table_rows)
                     .replace("<!--PAGINATION-->",
                              self._render_pagination(page, total_pages, total)))
        self._send_html(200, page_html)

    def _get_page_number(self) -> int:
        """Номер сторінки з ?page=N. Некоректне або менше 1 значення -> 1."""
        query = parse_qs(urlparse(self.path).query)
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            return 1
        return max(page, 1)

    @staticmethod
    def _render_pagination(page: int, total_pages: int, total: int) -> str:
        if total == 0:
            return ""

        if page > 1:
            prev_btn = (f'<a class="page-btn" href="/images-list?page={page - 1}">'
                        "&larr; Попередня сторінка</a>")
        else:
            prev_btn = '<span class="page-btn disabled">&larr; Попередня сторінка</span>'

        if page < total_pages:
            next_btn = (f'<a class="page-btn" href="/images-list?page={page + 1}">'
                        "Наступна сторінка &rarr;</a>")
        else:
            next_btn = '<span class="page-btn disabled">Наступна сторінка &rarr;</span>'

        return (
            '<nav class="pagination">'
            f"{prev_btn}"
            f'<span class="page-info">Сторінка {page} з {total_pages} '
            f"(всього зображень: {total})</span>"
            f"{next_btn}"
            "</nav>")

    @staticmethod
    def _render_image_row(row) -> str:
        image_id, filename, original_name, size, upload_time, file_type = row
        name = html.escape(filename)
        original = html.escape(original_name)
        size_kb = f"{size / 1024:.1f}"
        uploaded = (upload_time.strftime("%Y-%m-%d %H:%M:%S")
                    if hasattr(upload_time, "strftime") else str(upload_time))
        return (
            "<tr>"
            f'<td><a href="/images/{name}" target="_blank">{name}</a></td>'
            f"<td>{original}</td>"
            f'<td class="num">{size_kb}</td>'
            f"<td>{uploaded}</td>"
            f"<td>{html.escape(file_type)}</td>"
            "</tr>")

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            logging.info(
                f"Помилка: некоректний Content-Type запиту на завантаження ({content_type}).")
            self._send_json_error(400, "Очікується Content-Type: multipart/form-data")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            logging.info("Помилка: некоректний Content-Length у запиті на завантаження.")
            self._send_json_error(400, "Некоректний Content-Length")
            return
        if content_length <= 0:
            logging.info("Помилка: порожнє тіло запиту на завантаження.")
            self._send_json_error(400, "Порожнє тіло запиту")
            return
        if content_length > MAX_FILE_SIZE:
            # Дочитуємо тіло запиту, інакше з'єднання обірветься
            # і Nginx замість нашої відповіді поверне 502.
            self._discard_body(content_length)
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
        original_name = os.path.basename(filename)
        file_type = ext.lstrip(".")

        # Порядок: запис у БД -> файл на диск -> commit.
        # Якщо БД недоступна або запис не вдався, файл на диску не з'являється.
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                image_id = save_metadata(
                    cur, unique_name, original_name, len(file_bytes), file_type)
            with open(save_path, "wb") as f:
                f.write(file_bytes)
            conn.commit()
        except psycopg2.Error as e:
            self._rollback_upload(conn, save_path)
            logging.error(
                f"Помилка: не вдалося зберегти метадані файлу {original_name} "
                f"в базі даних, файл не збережено. Причина: {e}")
            self._send_json_error(500, "Помилка бази даних, файл не збережено")
            return
        except OSError as e:
            self._rollback_upload(conn, save_path)
            logging.error(
                f"Помилка: не вдалося записати файл {unique_name} на диск. Причина: {e}")
            self._send_json_error(500, "Не вдалося зберегти файл на сервері")
            return
        finally:
            if conn is not None:
                conn.close()

        logging.info(
            f"Успіх: зображення {unique_name} (оригінал: {original_name}, "
            f"{len(file_bytes)} байт, id={image_id}) завантажено.")

        self._send_json(200, {
            "message": "Файл успішно завантажено",
            "id": image_id,
            "filename": unique_name,
            "url": f"/images/{unique_name}"})

    def _discard_body(self, length: int):
        """Читає і відкидає тіло запиту шматками, не тримаючи його в пам'яті."""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)

    @staticmethod
    def _rollback_upload(conn, save_path: Path):
        """Відкочує транзакцію і прибирає файл, якщо він встиг записатися."""
        if conn is not None:
            try:
                conn.rollback()
            except psycopg2.Error:
                pass
        try:
            save_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _handle_image_serve(self):
        filename = self.path[len("/images/"):]
        requested_path = (IMAGES_DIR / filename).resolve()

        if IMAGES_DIR.resolve() not in requested_path.parents:
            logging.info(f"Помилка: спроба доступу за межі дозволеної папки ({self.path}).")
            self._send_error(403, "Доступ заборонено")
            return

        content_type = mimetypes.guess_type(str(requested_path))[0] or "application/octet-stream"
        self._serve_file(requested_path, content_type)

    def _send_html(self, status: int, page: str):
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_error(self, status: int, message: str):

        body = (f"<html><body><h1>{status}</h1><p>{message}</p></body></html>").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str):
        if not path.is_file():
            logging.info(f"Помилка: файл {self.path} не знайдено.")
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
    init_db()
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ImageHostingHandler) as httpd:
        print(f"Сервер запущено на порту {PORT} (http://localhost:{PORT})")
        httpd.serve_forever()


if __name__ == "__main__":
    run()
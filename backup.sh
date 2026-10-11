#!/usr/bin/env bash
#
# Резервне копіювання бази даних PostgreSQL (сервіс "Сервер зображень").
#
# Запуск з кореня проекту:
#     ./backup.sh
#
# Результат: backups/backup_YYYY-MM-DD_HHMMSS.sql
# Результат операції записується в logs/app.log.
#
# Відновлення з бекапу:
#     docker exec -i postgres_container psql -U postgres images_db < backups/<файл>.sql

set -uo pipefail

CONTAINER="postgres_container"
DB_USER="postgres"
DB_NAME="images_db"

# Працюємо відносно папки, де лежить скрипт, щоб його можна було
# запускати з будь-якої директорії.
cd "$(dirname "$0")" || exit 1

BACKUP_DIR="backups"
LOG_FILE="logs/app.log"
mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"

# Час у UTC, як і в app.log та в базі даних (контейнери працюють в UTC).
TIMESTAMP="$(date -u +%Y-%m-%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/backup_${TIMESTAMP}.sql"
TMP_FILE="${BACKUP_FILE}.tmp"

log() {
    # Той самий формат, що й у app.py: [YYYY-MM-DD HH:MM:SS] Дія: ...
    local line
    line="[$(date -u '+%Y-%m-%d %H:%M:%S')] Дія: $1"
    echo "$line" >> "$LOG_FILE"
    echo "$line"
}

fail() {
    rm -f "$TMP_FILE"
    log "Помилка: резервне копіювання не виконано. Причина: $1"
    exit 1
}

# 1. Контейнер з базою має бути запущений.
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    fail "контейнер $CONTAINER не запущено (виконайте docker compose up -d)"
fi

# 2. Робимо дамп у тимчасовий файл.
#    Без прапорця -t: з ним docker додає \r у кінці рядків і дамп псується.
#    --clean --if-exists: при відновленні таблиця спочатку видаляється
#    і створюється заново, тому дані не дублюються.
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" \
        > "$TMP_FILE" 2> "${TMP_FILE}.err"; then
    ERR="$(tr '\n' ' ' < "${TMP_FILE}.err")"
    rm -f "${TMP_FILE}.err"
    fail "pg_dump завершився з помилкою: ${ERR}"
fi
rm -f "${TMP_FILE}.err"

# 3. Порожній дамп вважаємо помилкою.
if [ ! -s "$TMP_FILE" ]; then
    fail "файл резервної копії порожній"
fi

# 4. Тільки успішний дамп отримує остаточну назву.
mv "$TMP_FILE" "$BACKUP_FILE"
SIZE="$(wc -c < "$BACKUP_FILE" | tr -d ' ')"
log "Успіх: резервну копію бази даних створено: $BACKUP_FILE ($SIZE байт)."
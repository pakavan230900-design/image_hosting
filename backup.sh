#!/usr/bin/env bash

set -uo pipefail

CONTAINER="postgres_container"
DB_USER="postgres"
DB_NAME="images_db"

cd "$(dirname "$0")" || exit 1

BACKUP_DIR="backups"
LOG_FILE="logs/app.log"
mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"

TIMESTAMP="$(date -u +%Y-%m-%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/backup_${TIMESTAMP}.sql"
TMP_FILE="${BACKUP_FILE}.tmp"

log() {
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

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    fail "контейнер $CONTAINER не запущено (виконайте docker compose up -d)"
fi

if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" \
        > "$TMP_FILE" 2> "${TMP_FILE}.err"; then
    ERR="$(tr '\n' ' ' < "${TMP_FILE}.err")"
    rm -f "${TMP_FILE}.err"
    fail "pg_dump завершився з помилкою: ${ERR}"
fi
rm -f "${TMP_FILE}.err"

if [ ! -s "$TMP_FILE" ]; then
    fail "файл резервної копії порожній"
fi

mv "$TMP_FILE" "$BACKUP_FILE"
SIZE="$(wc -c < "$BACKUP_FILE" | tr -d ' ')"
log "Успіх: резервну копію бази даних створено: $BACKUP_FILE ($SIZE байт)."
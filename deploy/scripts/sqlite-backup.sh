#!/usr/bin/env bash
set -euo pipefail

db_path="${TRAINING_TOOLS_DB_PATH:-/var/lib/training-tools/training_tools.db}"
backup_dir="${TRAINING_TOOLS_BACKUP_DIR:-/var/lib/training-tools/backups}"
retention_days="${TRAINING_TOOLS_BACKUP_RETENTION_DAYS:-14}"

if [ ! -f "$db_path" ]; then
  echo "SQLite database not found at $db_path; skipping backup"
  exit 0
fi

mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="$backup_dir/training_tools-$stamp.db"

sqlite3 "$db_path" ".backup '$backup_path'"
gzip "$backup_path"
find "$backup_dir" -name 'training_tools-*.db.gz' -type f -mtime "+$retention_days" -delete

echo "Wrote $backup_path.gz"

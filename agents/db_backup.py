"""
db_backup.py

Creates a daily backup of affiliate_bot.db and removes backups older than 7 days.

Backup location: data/backups/YYYYMMDD_affiliate_bot.db
Discord notifications:
  ✅ on success
  🚨 on failure
"""

import logging
import os
import shutil
from datetime import datetime, timedelta

logger = logging.getLogger("affiliate_bot.db_backup")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH      = os.path.join(_PROJECT_ROOT, "data", "affiliate_bot.db")
_BACKUP_DIR   = os.path.join(_PROJECT_ROOT, "data", "backups")
_KEEP_DAYS    = 7


def run_db_backup() -> None:
    """Back up affiliate_bot.db and prune old backups.

    Sends a Discord notification on completion or failure.
    """
    # Import here to avoid circular imports at module load time
    from agents.discord_notifier import send_discord, send_discord_error

    os.makedirs(_BACKUP_DIR, exist_ok=True)

    today       = datetime.now().strftime("%Y%m%d")
    backup_name = f"{today}_affiliate_bot.db"
    backup_path = os.path.join(_BACKUP_DIR, backup_name)

    # --- Copy ---
    try:
        shutil.copy2(_DB_PATH, backup_path)
        size_kb = os.path.getsize(backup_path) // 1024
        logger.info("[DBBackup] Backup created: %s (%d KB)", backup_name, size_kb)
        send_discord(f"✅ DBバックアップ完了: {backup_name} ({size_kb} KB)")
    except Exception as e:
        logger.exception("[DBBackup] Backup failed")
        send_discord_error(f"🚨 DBバックアップ失敗: {e}")
        return

    # --- Prune old backups ---
    cutoff = datetime.now() - timedelta(days=_KEEP_DAYS)
    deleted = 0
    try:
        for fname in os.listdir(_BACKUP_DIR):
            if not fname.endswith("_affiliate_bot.db"):
                continue
            date_str = fname[:8]
            try:
                file_date = datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                continue
            if file_date < cutoff:
                os.remove(os.path.join(_BACKUP_DIR, fname))
                deleted += 1
                logger.debug("[DBBackup] Deleted old backup: %s", fname)
    except Exception:
        logger.exception("[DBBackup] Error during old backup pruning")

    if deleted:
        logger.info("[DBBackup] Pruned %d old backup(s) (older than %d days)", deleted, _KEEP_DAYS)


if __name__ == "__main__":
    # Allow running standalone for manual backup
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    run_db_backup()

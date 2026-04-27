#!/usr/bin/env python3
"""
affiliate_bot サマリー生成スクリプト
新チャット開始時に状況を把握するためのサマリーを出力する
"""

import sqlite3
import sys
from datetime import datetime, timedelta

DB_PATH = '/home/affiliate_bot/data/affiliate_bot.db'


def main():
    now = datetime.now()
    print(f"=== affiliate_bot DB SUMMARY [{now.strftime('%Y-%m-%d %H:%M:%S')}] ===")

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
    except Exception as e:
        print(f"\nDB connection failed: {e}")
        sys.exit(1)

    seven_days_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')

    # ─────────────────────────────────────
    # 1. POST STATS - Last 7 days
    # ─────────────────────────────────────
    print("\n[POST STATS - Last 7 days]")
    try:
        cur.execute(
            "SELECT COUNT(*) FROM post_logs WHERE posted_at >= ?",
            (seven_days_ago,)
        )
        total = cur.fetchone()[0]
        print(f"- Total: {total}")

        cur.execute(
            "SELECT ab_group, COUNT(*) as cnt FROM post_logs "
            "WHERE posted_at >= ? GROUP BY ab_group ORDER BY ab_group",
            (seven_days_ago,)
        )
        ab_rows = cur.fetchall()
        ab_dict = {row['ab_group']: row['cnt'] for row in ab_rows}
        count_a = ab_dict.get('A', 0)
        count_b = ab_dict.get('B', 0)
        print(f"- Pattern A: {count_a} / Pattern B: {count_b}")

    except Exception as e:
        print(f"ERROR: {e}")

    # ─────────────────────────────────────
    # 2. A/B TEST
    # ─────────────────────────────────────
    print("\n[A/B TEST]")
    try:
        cur.execute(
            "SELECT pattern, AVG(engagement_rate) as avg_er, COUNT(*) as cnt "
            "FROM ab_test_results GROUP BY pattern ORDER BY pattern"
        )
        ab_results = cur.fetchall()
        if ab_results:
            for row in ab_results:
                avg = row['avg_er'] if row['avg_er'] is not None else 0
                print(f"- Pattern {row['pattern']} avg engagement_rate: {avg:.2f}%（{row['cnt']} posts）")
        else:
            print("- (no data)")

        cur.execute("SELECT MIN(posted_at) FROM ab_test_results")
        first_post = cur.fetchone()[0]
        if first_post:
            try:
                first_dt = datetime.fromisoformat(first_post)
                elapsed = (now - first_dt).days
                print(f"- First post: {first_post}")
                print(f"- Days elapsed: {elapsed}日")
            except Exception:
                print(f"- First post: {first_post}")
        else:
            print("- First post: (no data)")

    except Exception as e:
        print(f"ERROR: {e}")

    # ─────────────────────────────────────
    # 3. FOLLOWERS
    # ─────────────────────────────────────
    print("\n[FOLLOWERS]")
    try:
        cur.execute(
            "SELECT timestamp, threads, instagram, bluesky "
            "FROM follower_history ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            print(f"- Threads: {row['threads']} / Instagram: {row['instagram']} / Bluesky: {row['bluesky']}")
            print(f"- Updated: {row['timestamp']}")
        else:
            print("- (no data)")

    except Exception as e:
        print(f"ERROR: {e}")

    # ─────────────────────────────────────
    # 4. QUEUE
    # ─────────────────────────────────────
    print("\n[QUEUE]")
    try:
        cur.execute(
            "SELECT status, COUNT(*) as cnt FROM posts_queue GROUP BY status ORDER BY status"
        )
        queue_rows = cur.fetchall()
        queue_dict = {row['status']: row['cnt'] for row in queue_rows}
        pending = queue_dict.get('pending', 0)
        sent = queue_dict.get('sent', 0)
        print(f"- pending: {pending} / sent: {sent}")

        other = {k: v for k, v in queue_dict.items() if k not in ('pending', 'sent')}
        for status, cnt in other.items():
            print(f"- {status}: {cnt}")

    except Exception as e:
        print(f"ERROR: {e}")

    # ─────────────────────────────────────
    # 5. LEARNING DATA - Top 5
    # ─────────────────────────────────────
    print("\n[LEARNING DATA - Top 5]")
    try:
        cur.execute("SELECT COUNT(*) FROM learning_data")
        total_count = cur.fetchone()[0]

        cur.execute(
            "SELECT title FROM learning_data ORDER BY engagement_score DESC LIMIT 5"
        )
        top_rows = cur.fetchall()
        top_count = len(top_rows)
        other_count = max(0, total_count - top_count)

        if top_rows:
            titles = ', '.join((row['title'] or '')[:20] for row in top_rows)
            print(f"- engagement上位: {titles} / その他: {other_count}件")
        else:
            print("- (no data)")

    except Exception as e:
        print(f"ERROR: {e}")

    conn.close()
    print("\n=== Copy above and paste to Claude ===")


if __name__ == '__main__':
    main()

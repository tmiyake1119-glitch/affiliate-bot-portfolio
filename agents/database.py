"""
database.py — SQLite データベース管理モジュール

【概要】
  全エージェントから共通利用される SQLite データベース層。
  旧来 JSON ファイルで管理していたデータをすべて affiliate_bot.db に集約している。
  インポート時に init_db() が自動実行されるため、テーブルは常に存在することが保証される。

【テーブル一覧と役割】
  post_logs              — 投稿ログ (旧 data/post_log.json)
                           threads_poster / instagram_poster / bluesky_poster が書き込む
                           product_selector（重複チェック）/ analytics_agent（分析）が参照する

  learning_data          — エンゲージメント学習データ (旧 data/learning_data.json)
                           analytics_agent が Threads API からいいね数等を取得して書き込む
                           product_selector（スコア重み付け）/ weekly_report が参照する

  posts_queue            — 投稿キュー (旧 data/posts_queue.json)
                           post_manager が書き込み、threads_poster が読み取る
                           status: 'pending' → 'sent' で投稿済みを管理する

  follower_history       — フォロワー履歴 (旧 data/follower_history.json)
                           analytics_agent.fetch_follower_count が1実行につき1件書き込む
                           follower_strategy / weekly_report が参照する

  rare_items             — レアアイテム監視 (旧 data/rare_items_seen.json)
                           rare_item_monitor が楽天APIで在庫チェックして書き込む

  rare_item_price_history — レアアイテム価格履歴 (副テーブル)
                            rare_items の price_history を正規化した子テーブル

  price_history          — 商品価格履歴 (旧 data/price_history.json)
                           product_selector が価格下落率（discount_rate）算出に参照する

  follow_log             — フォロー・いいね実績ログ
                           follow_reminder が日次目標管理に使用する

  ab_test_results        — A/Bテスト結果
                           analytics_agent が書き込み、ab_tester が比率調整に参照する

【使用方法】
  from database import get_db, insert_post_log, get_all_post_logs, ...

NOTE: engagement_score カラムは get_* 関数では "engagement" キーとして返す。
      既存エージェントとの互換性を保つため。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'affiliate_bot.db')
_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

_POST_LOG_JSON    = os.path.join(_DATA_DIR, 'post_log.json')
_LEARNING_JSON    = os.path.join(_DATA_DIR, 'learning_data.json')
_QUEUE_JSON       = os.path.join(_DATA_DIR, 'posts_queue.json')
_FOLLOWER_JSON    = os.path.join(_DATA_DIR, 'follower_history.json')
_RARE_ITEMS_JSON  = os.path.join(_DATA_DIR, 'rare_items_seen.json')
_PRICE_HIST_JSON  = os.path.join(_DATA_DIR, 'price_history.json')


# ---------------------------------------------------------------------------
# 接続管理
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """SQLite コネクションを返す。全 CRUD 関数の共通エントリーポイント。

    - row_factory=sqlite3.Row により dict(row) で列名付きdictを取得可能。
    - WAL モードで複数プロセスからの同時書き込み競合を軽減する（VPS上のcron並行実行対策）。
    - 呼び出し側は必ず finally: conn.close() でクローズすること。
    """
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 書き込み競合を軽減
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# テーブル初期化
# ---------------------------------------------------------------------------

_DDL_POST_LOGS = """
CREATE TABLE IF NOT EXISTS post_logs (
    -- 【post_logs】 投稿ログテーブル
    --   書き込み: threads_poster.py（Threads 投稿時）/ instagram_poster.py（Instagram 投稿時）
    --   参照: product_selector.py（重複投稿防止）/ analytics_agent.py（分析）
    --         weekly_report.py / generate_summary.py
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT,           -- 商品名（先頭60文字）
    genre         TEXT,           -- ジャンル名（楽天カテゴリ）
    price         TEXT,           -- 表示価格文字列（例: "¥1,980"）
    url           TEXT,           -- 楽天商品URL（重複チェックのキー）
    affiliate_url TEXT,           -- アフィリエイトURL（投稿本文に使用）
    ab_group      TEXT,           -- A/Bテストグループ（"A" or "B"）
    account       TEXT,           -- 投稿アカウント名（例: "mono_zukan_jp"）
    posted_at     TEXT,           -- 投稿日時（ISO 8601 UTC）
    status        TEXT,           -- 投稿結果（"success" / "error"）
    post_id       TEXT,           -- SNS側の投稿ID（Threads/Instagram media_id 等）
    error         TEXT,           -- エラーメッセージ（status="error" 時のみ）
    image_url     TEXT,           -- 投稿に使用した画像URL（Cloudinary or 楽天）
    platform      TEXT DEFAULT 'threads',  -- 投稿プラットフォーム（"threads" / "instagram"）
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- レコード作成日時
);
"""

_DDL_LEARNING = """
CREATE TABLE IF NOT EXISTS learning_data (
    -- 【learning_data】 エンゲージメント学習データテーブル
    --   書き込み: analytics_agent.py（Threads API からインサイトを取得して upsert）
    --   参照: product_selector.py（高エンゲージメント商品を優先選定）
    --         weekly_report.py / ab_tester.py / generate_summary.py
    --   NOTE: engagement_score は get_* 関数では "engagement" キーとして返す（後方互換）
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id          TEXT UNIQUE,       -- SNS側の投稿ID（重複 upsert のキー）
    title            TEXT,              -- 商品名
    genre            TEXT,              -- ジャンル名
    price            TEXT,              -- 表示価格文字列
    ab_group         TEXT,              -- A/Bテストグループ
    posted_at        TEXT,              -- 投稿日時（ISO 8601 UTC）
    likes            INTEGER DEFAULT 0, -- いいね数
    replies          INTEGER DEFAULT 0, -- リプライ数
    reposts          INTEGER DEFAULT 0, -- リポスト数
    views            INTEGER DEFAULT 0, -- インプレッション数
    engagement_score REAL    DEFAULT 0, -- エンゲージメントスコア（likes+replies*2+reposts 等の合成値）
    fetched_at       TEXT,              -- インサイト最終取得日時
    image_url        TEXT,              -- 投稿画像URL
    url              TEXT               -- 楽天商品URL
);
"""

_IDX_POST_LOGS = [
    "CREATE INDEX IF NOT EXISTS idx_post_logs_posted_at ON post_logs(posted_at);",
    "CREATE INDEX IF NOT EXISTS idx_post_logs_status    ON post_logs(status);",
    "CREATE INDEX IF NOT EXISTS idx_post_logs_genre     ON post_logs(genre);",
    "CREATE INDEX IF NOT EXISTS idx_post_logs_url       ON post_logs(url);",
]

_IDX_LEARNING = [
    "CREATE INDEX IF NOT EXISTS idx_learning_post_id   ON learning_data(post_id);",
    "CREATE INDEX IF NOT EXISTS idx_learning_genre      ON learning_data(genre);",
    "CREATE INDEX IF NOT EXISTS idx_learning_posted_at  ON learning_data(posted_at);",
]

_DDL_POSTS_QUEUE = """
CREATE TABLE IF NOT EXISTS posts_queue (
    -- 【posts_queue】 投稿キューテーブル
    --   書き込み: post_manager.py（save_posts / refill_queue で追加）
    --   読み出し: threads_poster.py（get_next_post で status='pending' を1件取得）
    --   更新   : threads_poster.py（投稿完了後に status='sent' に変更）
    --            main.py（genre 不一致時に status='pending' に戻す）
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT,                       -- 楽天商品URL
    affiliate_url       TEXT,                       -- アフィリエイトURL
    title               TEXT,                       -- 商品名
    price               TEXT,                       -- 表示価格文字列
    genre               TEXT,                       -- ジャンル名
    image_url           TEXT,                       -- 商品画像URL（Cloudinary or 楽天）
    post_text           TEXT,                       -- 生成済み投稿本文（OpenAIで生成）
    ab_group            TEXT,                       -- A/Bテストグループ（"A" or "B"）
    status              TEXT DEFAULT 'pending',     -- 'pending'（未投稿）/ 'sent'（投稿済み）
    created_at          TEXT,                       -- キュー追加日時（ISO 8601 UTC）
    sent_at             TEXT,                       -- 投稿完了日時（投稿後に設定）
    rating_str          TEXT,                       -- 星評価文字列（例: "★4.5"）
    free_shipping       INTEGER DEFAULT 0,          -- 送料無料フラグ（1=無料）
    review_quote        TEXT,                       -- レビュー引用文（投稿文に使用）
    product_description TEXT,                       -- 商品説明文
    review_average      REAL    DEFAULT 0,          -- レビュー平均点（楽天APIから取得）
    review_count        INTEGER DEFAULT 0,          -- レビュー件数
    rank                INTEGER DEFAULT 0,          -- 楽天ランキング順位
    point_rate          INTEGER DEFAULT 1,          -- ポイント倍率（通常=1）
    postage_flag        INTEGER DEFAULT 0,          -- 送料条件フラグ（楽天API値）
    price_raw           INTEGER DEFAULT 0,          -- 税込み価格（円・整数）
    price_avg_30d       REAL,                       -- 過去30日平均価格（価格下落判定用）
    price_old           INTEGER DEFAULT 0,          -- 変更前の価格（price_dropped=1 の場合）
    price_dropped       INTEGER DEFAULT 0,          -- 価格下落フラグ（1=下落中）
    price_drop_pct      REAL    DEFAULT 0,          -- 価格下落率（例: 0.15 = 15%引き）
    discount_rate       REAL    DEFAULT 0           -- 割引率（product_selector スコアリングに使用）
);
"""

_DDL_FOLLOWER_HISTORY = """
CREATE TABLE IF NOT EXISTS follower_history (
    -- 【follower_history】 フォロワー数履歴テーブル
    --   書き込み: analytics_agent.fetch_follower_count（毎実行1件追加）
    --   参照: follower_strategy.py（成長率計算）/ weekly_report.py / strategy_agent.py
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,     -- 記録日時（ISO 8601 UTC）
    threads   INTEGER,  -- Threads フォロワー数（取得失敗時は None）
    instagram INTEGER,  -- Instagram フォロワー数（取得失敗時は None）
    bluesky   INTEGER   -- Bluesky フォロワー数（取得失敗時は None）
);
"""

_IDX_POSTS_QUEUE = [
    "CREATE INDEX IF NOT EXISTS idx_queue_url    ON posts_queue(url);",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON posts_queue(status);",
]

_IDX_FOLLOWER = [
    "CREATE INDEX IF NOT EXISTS idx_follower_ts ON follower_history(timestamp);",
]

_DDL_RARE_ITEMS = """
CREATE TABLE IF NOT EXISTS rare_items (
    -- 【rare_items】 レアアイテム監視テーブル
    --   書き込み: rare_item_monitor.py（楽天APIで在庫チェックして upsert）
    --   参照: rare_item_monitor.py（入荷/売切れ検知・Discord通知判定）
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code        TEXT UNIQUE,       -- 楽天商品コード（監視キー）
    name             TEXT,              -- 商品名
    price            INTEGER,           -- 最新価格（円）
    keyword          TEXT,              -- 監視に使用したキーワード
    category         TEXT,              -- カテゴリ
    first_seen       TEXT,              -- 初回検出日時（ISO 8601 UTC）
    last_seen        TEXT,              -- 最終在庫確認日時
    last_checked     TEXT,              -- 最終チェック日時
    last_posted_at   TEXT,              -- 最終Discord通知日時
    posted           INTEGER DEFAULT 0, -- 入荷通知送信済みフラグ（1=送信済み）
    sold_out         INTEGER DEFAULT 0, -- 売切れフラグ（1=売切れ中）
    sold_out_posted  INTEGER DEFAULT 0, -- 売切れ通知送信済みフラグ
    restock_count    INTEGER DEFAULT 0, -- 再入荷回数カウント
    daily_post_count INTEGER DEFAULT 0, -- 当日の通知送信回数
    daily_post_date  TEXT               -- daily_post_count のリセット基準日
);
"""

_DDL_RARE_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS rare_item_price_history (
    -- 【rare_item_price_history】 レアアイテムの価格変動履歴テーブル
    --   rare_items の正規化子テーブル（price_history を分離管理）
    --   書き込み: rare_item_monitor.py
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code TEXT,      -- 親テーブル rare_items.item_code への外部キー
    price     INTEGER,   -- 価格（円）
    timestamp TEXT,      -- 記録日時（ISO 8601 UTC）
    FOREIGN KEY (item_code) REFERENCES rare_items(item_code)
);
"""

_DDL_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS price_history (
    -- 【price_history】 通常商品の価格履歴テーブル
    --   書き込み: product_selector.py（価格変動を記録して discount_rate を計算）
    --   参照: product_selector._get_price_stats()（30日平均価格・下落率算出）
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT,      -- 楽天商品URL（キー）
    price     INTEGER,   -- 価格（円）
    timestamp TEXT       -- 記録日時（ISO 8601 UTC）
);
"""

_DDL_FOLLOW_LOG = """
CREATE TABLE IF NOT EXISTS follow_log (
    -- 【follow_log】 フォロー・いいね実績ログテーブル
    --   書き込み: follow_reminder.py（フォロー実績を日次で記録）
    --   参照: follow_reminder.py（進捗通知・目標達成判定）
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT UNIQUE,                -- 日付（YYYY-MM-DD）
    follows              INTEGER DEFAULT 0,          -- 当日のフォロー実施数
    likes                INTEGER DEFAULT 0,          -- 当日のいいね実施数
    target_follows       INTEGER DEFAULT 20,         -- 当日のフォロー目標数
    target_likes         INTEGER DEFAULT 50,         -- 当日のいいね目標数
    follower_gain_ig     INTEGER DEFAULT 0,          -- Instagram フォロワー増減数
    follower_gain_threads INTEGER DEFAULT 0,         -- Threads フォロワー増減数
    follower_gain_bluesky INTEGER DEFAULT 0,         -- Bluesky フォロワー増減数
    updated_at           TEXT                        -- 最終更新日時（ISO 8601 UTC）
);
"""

_IDX_FOLLOW_LOG = [
    "CREATE INDEX IF NOT EXISTS idx_follow_log_date ON follow_log(date);",
]

_DDL_AB_TEST_RESULTS = """
CREATE TABLE IF NOT EXISTS ab_test_results (
    -- 【ab_test_results】 A/Bテスト結果テーブル
    --   書き込み: analytics_agent.save_ab_result（インサイト取得時に upsert）
    --   参照: ab_tester.auto_adjust_ab_ratio（A/B比率の自動調整）
    --         analytics_agent.evaluate_ab_test（A/B評価・Discord通知）
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern           TEXT,               -- A/Bグループ名（"A" or "B"）
    post_id           TEXT UNIQUE,        -- SNS側の投稿ID（重複 upsert のキー）
    engagement_rate   REAL    DEFAULT 0,  -- エンゲージメント率（engagement / followers_at_post）
    followers_at_post INTEGER DEFAULT 0,  -- 投稿時点のフォロワー数（正規化に使用）
    posted_at         TEXT,               -- 投稿日時（ISO 8601 UTC）
    genre             TEXT,               -- ジャンル名
    views             INTEGER DEFAULT 0   -- インプレッション数
);
"""

_IDX_AB_TEST = [
    "CREATE INDEX IF NOT EXISTS idx_ab_test_pattern   ON ab_test_results(pattern);",
    "CREATE INDEX IF NOT EXISTS idx_ab_test_posted_at ON ab_test_results(posted_at);",
]

_IDX_RARE = [
    "CREATE INDEX IF NOT EXISTS idx_rare_item_code  ON rare_items(item_code);",
    "CREATE INDEX IF NOT EXISTS idx_rare_keyword    ON rare_items(keyword);",
    "CREATE INDEX IF NOT EXISTS idx_rare_ph_code    ON rare_item_price_history(item_code);",
    "CREATE INDEX IF NOT EXISTS idx_rare_ph_ts      ON rare_item_price_history(timestamp);",
]

_IDX_PRICE_HIST = [
    "CREATE INDEX IF NOT EXISTS idx_price_hist_url  ON price_history(url);",
    "CREATE INDEX IF NOT EXISTS idx_price_hist_ts   ON price_history(timestamp);",
]


def init_db() -> None:
    """テーブルとインデックスを作成する（既存なら何もしない）。"""
    conn = get_db()
    try:
        conn.execute(_DDL_POST_LOGS)
        conn.execute(_DDL_LEARNING)
        conn.execute(_DDL_POSTS_QUEUE)
        conn.execute(_DDL_FOLLOWER_HISTORY)
        conn.execute(_DDL_RARE_ITEMS)
        conn.execute(_DDL_RARE_PRICE_HISTORY)
        conn.execute(_DDL_PRICE_HISTORY)
        conn.execute(_DDL_FOLLOW_LOG)
        conn.execute(_DDL_AB_TEST_RESULTS)
        for sql in (_IDX_POST_LOGS + _IDX_LEARNING + _IDX_POSTS_QUEUE
                    + _IDX_FOLLOWER + _IDX_RARE + _IDX_PRICE_HIST + _IDX_FOLLOW_LOG
                    + _IDX_AB_TEST):
            conn.execute(sql)
        # follow_log の follower_gain 列マイグレーション（既存 DB 対応）
        for col, default in [
            ("follower_gain_ig",      0),
            ("follower_gain_threads", 0),
            ("follower_gain_bluesky", 0),
        ]:
            try:
                conn.execute(f"ALTER TABLE follow_log ADD COLUMN {col} INTEGER DEFAULT {default}")
            except Exception:
                pass  # 列が既に存在する場合はスキップ
        # post_logs の platform 列マイグレーション（既存 DB 対応）
        # VPS側でも自動実行される。既存レコードは DEFAULT 'threads' が適用される。
        try:
            conn.execute("ALTER TABLE post_logs ADD COLUMN platform TEXT DEFAULT 'threads'")
        except Exception:
            pass  # 列が既に存在する場合はスキップ
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# マイグレーション
# ---------------------------------------------------------------------------

def migrate() -> dict[str, int]:
    """
    既存の JSON ファイルを SQLite に移行する。
    テーブルにデータが存在する場合はスキップ（二重移行防止）。

    Returns: {"post_logs": int, "learning_data": int, "posts_queue": int,
              "follower_history": int, "rare_items": int, "price_history": int}
    """
    init_db()
    conn  = get_db()
    stats = {"post_logs": 0, "learning_data": 0, "posts_queue": 0,
             "follower_history": 0, "rare_items": 0, "price_history": 0}

    # post_logs 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM post_logs").fetchone()[0]
    if existing_count == 0 and os.path.exists(_POST_LOG_JSON):
        try:
            with open(_POST_LOG_JSON, 'r', encoding='utf-8') as f:
                entries = json.load(f)
            for e in entries:
                conn.execute("""
                    INSERT INTO post_logs
                      (title, genre, price, url, affiliate_url, ab_group, account,
                       posted_at, status, post_id, error, image_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    e.get("title"),
                    e.get("genre"),
                    e.get("price"),
                    e.get("url"),
                    e.get("affiliate_url"),
                    e.get("ab_group"),
                    e.get("account"),
                    e.get("posted_at"),
                    e.get("status"),
                    e.get("post_id"),
                    e.get("error"),
                    e.get("image_url"),
                ))
            conn.commit()
            stats["post_logs"] = len(entries)
            print(f"[DB] post_logs: {len(entries)} 件移行完了")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] post_logs 移行エラー: {ex}")
    else:
        print(f"[DB] post_logs: スキップ（既存 {existing_count} 件）")

    # learning_data 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM learning_data").fetchone()[0]
    if existing_count == 0 and os.path.exists(_LEARNING_JSON):
        try:
            with open(_LEARNING_JSON, 'r', encoding='utf-8') as f:
                entries = json.load(f)
            for e in entries:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO learning_data
                          (post_id, title, genre, price, ab_group, posted_at,
                           likes, replies, reposts, views, engagement_score,
                           fetched_at, image_url, url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        e.get("post_id"),
                        e.get("title"),
                        e.get("genre"),
                        e.get("price"),
                        e.get("ab_group"),
                        e.get("posted_at"),
                        e.get("likes", 0),
                        e.get("replies", 0),
                        e.get("reposts", 0),
                        e.get("views", 0),
                        e.get("engagement", e.get("engagement_score", 0)),
                        e.get("fetched_at"),
                        e.get("image_url"),
                        e.get("url"),
                    ))
                except sqlite3.IntegrityError:
                    pass  # UNIQUE 違反は無視
            conn.commit()
            stats["learning_data"] = len(entries)
            print(f"[DB] learning_data: {len(entries)} 件移行完了")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] learning_data 移行エラー: {ex}")
    else:
        print(f"[DB] learning_data: スキップ（既存 {existing_count} 件）")

    # posts_queue 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM posts_queue").fetchone()[0]
    if existing_count == 0 and os.path.exists(_QUEUE_JSON):
        try:
            with open(_QUEUE_JSON, 'r', encoding='utf-8') as f:
                entries = json.load(f)
            for e in entries:
                conn.execute("""
                    INSERT INTO posts_queue
                      (url, affiliate_url, title, price, genre, image_url, post_text,
                       ab_group, status, created_at, sent_at, rating_str, free_shipping,
                       review_quote, product_description, review_average, review_count,
                       rank, point_rate, postage_flag, price_raw, price_avg_30d, discount_rate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    e.get("url"), e.get("affiliate_url"), e.get("title"), e.get("price"),
                    e.get("genre"), e.get("image_url"), e.get("post_text"),
                    e.get("ab_group"), e.get("status", "pending"),
                    e.get("created_at"), e.get("sent_at"),
                    e.get("rating_str"), 1 if e.get("free_shipping") else 0,
                    e.get("review_quote"), e.get("product_description"),
                    e.get("review_average", 0), e.get("review_count", 0),
                    e.get("rank", 0), e.get("point_rate", 1),
                    e.get("postage_flag", 0),
                    e.get("price_raw", 0),
                    e.get("price_avg_30d"),
                    e.get("discount_rate", 0),
                ))
            conn.commit()
            stats["posts_queue"] = len(entries)
            print(f"[DB] posts_queue: {len(entries)} 件移行完了")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] posts_queue 移行エラー: {ex}")
    else:
        print(f"[DB] posts_queue: スキップ（既存 {existing_count} 件）")

    # follower_history 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM follower_history").fetchone()[0]
    if existing_count == 0 and os.path.exists(_FOLLOWER_JSON):
        try:
            with open(_FOLLOWER_JSON, 'r', encoding='utf-8') as f:
                entries = json.load(f)
            for e in entries:
                conn.execute(
                    "INSERT INTO follower_history (timestamp, threads, instagram, bluesky) VALUES (?, ?, ?, ?)",
                    (e.get("timestamp"), e.get("threads"), e.get("instagram"), e.get("bluesky")),
                )
            conn.commit()
            stats["follower_history"] = len(entries)
            print(f"[DB] follower_history: {len(entries)} 件移行完了")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] follower_history 移行エラー: {ex}")
    else:
        print(f"[DB] follower_history: スキップ（既存 {existing_count} 件）")

    # rare_items + rare_item_price_history 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM rare_items").fetchone()[0]
    if existing_count == 0 and os.path.exists(_RARE_ITEMS_JSON):
        try:
            with open(_RARE_ITEMS_JSON, 'r', encoding='utf-8') as f:
                items = json.load(f)
            ph_rows = 0
            for item_code, rec in items.items():
                dpc = rec.get("daily_post_count", {})
                conn.execute("""
                    INSERT OR IGNORE INTO rare_items
                      (item_code, name, price, keyword, category, first_seen, last_seen,
                       last_checked, last_posted_at, posted, sold_out, sold_out_posted,
                       restock_count, daily_post_count, daily_post_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item_code,
                    rec.get("name"), rec.get("price"), rec.get("keyword"), rec.get("category"),
                    rec.get("first_seen"), rec.get("last_seen"), rec.get("last_checked"),
                    rec.get("last_posted_at"),
                    1 if rec.get("posted") else 0,
                    1 if rec.get("sold_out") else 0,
                    1 if rec.get("sold_out_posted") else 0,
                    rec.get("restock_count", 0),
                    dpc.get("count", 0) if isinstance(dpc, dict) else 0,
                    dpc.get("date", "") if isinstance(dpc, dict) else "",
                ))
                for ph in rec.get("price_history", []):
                    conn.execute(
                        "INSERT INTO rare_item_price_history (item_code, price, timestamp) VALUES (?, ?, ?)",
                        (item_code, ph.get("price"), ph.get("timestamp")),
                    )
                    ph_rows += 1
            conn.commit()
            stats["rare_items"] = len(items)
            print(f"[DB] rare_items: {len(items)} 件移行完了 (price_history: {ph_rows} 件)")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] rare_items 移行エラー: {ex}")
    else:
        print(f"[DB] rare_items: スキップ（既存 {existing_count} 件）")

    # price_history 移行
    existing_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    if existing_count == 0 and os.path.exists(_PRICE_HIST_JSON):
        try:
            with open(_PRICE_HIST_JSON, 'r', encoding='utf-8') as f:
                ph_data = json.load(f)
            count = 0
            for url, entry in ph_data.items():
                if isinstance(entry, dict):
                    conn.execute(
                        "INSERT INTO price_history (url, price, timestamp) VALUES (?, ?, ?)",
                        (url, entry.get("price"), entry.get("timestamp")),
                    )
                    count += 1
                elif isinstance(entry, list):
                    for point in entry:
                        conn.execute(
                            "INSERT INTO price_history (url, price, timestamp) VALUES (?, ?, ?)",
                            (url, point.get("price"), point.get("timestamp")),
                        )
                        count += 1
            conn.commit()
            stats["price_history"] = count
            print(f"[DB] price_history: {count} 件移行完了 ({len(ph_data)} URL)")
        except Exception as ex:
            conn.rollback()
            print(f"[DB] price_history 移行エラー: {ex}")
    else:
        print(f"[DB] price_history: スキップ（既存 {existing_count} 件）")

    conn.close()
    return stats


# ---------------------------------------------------------------------------
# post_logs CRUD
# ---------------------------------------------------------------------------

def insert_post_log(entry: dict) -> None:
    """投稿ログを1件挿入する（threads_poster.py / instagram_poster.py から呼ばれる）。

    entry に platform キーがない場合は "threads" をデフォルト値とする（後方互換）。
    """
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO post_logs
              (title, genre, price, url, affiliate_url, ab_group, account,
               posted_at, status, post_id, error, image_url, platform)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("title"),
            entry.get("genre"),
            entry.get("price"),
            entry.get("url"),
            entry.get("affiliate_url"),
            entry.get("ab_group"),
            entry.get("account"),
            entry.get("posted_at"),
            entry.get("status"),
            entry.get("post_id"),
            entry.get("error"),
            entry.get("image_url"),
            entry.get("platform", "threads"),
        ))
        conn.commit()
    finally:
        conn.close()


def get_all_post_logs() -> list[dict]:
    """
    全投稿ログを posted_at 昇順で返す。
    旧 json.load(LOG_PATH) の完全な代替。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM post_logs ORDER BY posted_at"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_recent_post_logs(limit: int = 100) -> list[dict]:
    """直近 limit 件のログを返す（新しい順）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM post_logs ORDER BY posted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_post_logs_since(cutoff_iso: str) -> list[dict]:
    """cutoff_iso 以降のログを返す。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM post_logs WHERE posted_at >= ? ORDER BY posted_at",
            (cutoff_iso,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_post_logs_for_date(date_str: str) -> list[dict]:
    """指定日付 (YYYY-MM-DD) のログを返す。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM post_logs WHERE posted_at LIKE ? ORDER BY posted_at",
            (f"{date_str}%",),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_successful_post_urls(days: int = 7) -> set[str]:
    """直近 days 日間に成功投稿された URL セットを返す（重複投稿防止用）。

    呼び出し元: product_selector.select_top_products（同じ商品の連投を避けるフィルタ）
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT url FROM post_logs
               WHERE status='success' AND url!='' AND url IS NOT NULL AND posted_at>=?""",
            (cutoff,),
        ).fetchall()
        return {row["url"] for row in rows}
    finally:
        conn.close()


def get_recent_posted_urls(days: int = 3) -> set[str]:
    """直近 days 日間に成功投稿された URL セットを返す（product_selector フィルタ用）。

    get_successful_post_urls のエイリアス。呼び出し側で用途を明示するために分離。
    """
    return get_successful_post_urls(days=days)


def get_recent_posted_titles(days: int = 7) -> set[str]:
    """直近 days 日間に成功投稿された商品名（先頭60文字・正規化済み）のセットを返す。

    post_logs.title は threads_poster が post["title"][:60] で保存する。
    正規化: 半角スペース・全角スペース除去、小文字化。
    呼び出し元: product_selector.select_top_products（タイトル類似商品の連投防止）
    """
    import re as _re
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT title FROM post_logs
               WHERE status='success' AND title!='' AND title IS NOT NULL AND posted_at>=?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    result: set[str] = set()
    for row in rows:
        t = row["title"] or ""
        # 空白除去・小文字化して正規化
        normalized = _re.sub(r"[\s\u3000]+", "", t).lower()
        if normalized:
            result.add(normalized)
    return result


def is_url_in_queue(url: str) -> bool:
    """指定 URL が posts_queue に pending 状態で存在するか確認する。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM posts_queue WHERE url=? AND status='pending' LIMIT 1",
            (url,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# learning_data CRUD
# ---------------------------------------------------------------------------

def get_all_learning_data() -> list[dict]:
    """全学習データを返す。engagement_score を "engagement" キーで返す。

    旧 json.load(LEARNING_PATH) の完全な代替。
    呼び出し元: product_selector.py（スコアリング重み計算）/ weekly_report.py
               generate_summary.py / learning_agent.py
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, post_id, title, genre, price, ab_group, posted_at,
                   likes, replies, reposts, views,
                   engagement_score AS engagement,
                   fetched_at, image_url, url
            FROM learning_data
            ORDER BY posted_at
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def upsert_learning_data(entry: dict) -> None:
    """1件の学習データを INSERT OR REPLACE する。post_id で一意。

    呼び出し元: analytics_agent.fetch_all_insights（インサイト取得後に1件ずつ upsert）
    """
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO learning_data
              (post_id, title, genre, price, ab_group, posted_at,
               likes, replies, reposts, views, engagement_score,
               fetched_at, image_url, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("post_id"),
            entry.get("title"),
            entry.get("genre"),
            entry.get("price"),
            entry.get("ab_group"),
            entry.get("posted_at"),
            entry.get("likes", 0),
            entry.get("replies", 0),
            entry.get("reposts", 0),
            entry.get("views", 0),
            entry.get("engagement", entry.get("engagement_score", 0)),
            entry.get("fetched_at"),
            entry.get("image_url"),
            entry.get("url"),
        ))
        conn.commit()
    finally:
        conn.close()


def save_learning_data_bulk(entries: list[dict]) -> None:
    """全件を一括 INSERT OR REPLACE する。

    旧 json.dump(data, LEARNING_PATH) の完全な代替。
    呼び出し元: analytics_agent.py（全件上書き保存パターン）
    1件ずつ upsert_learning_data を呼ぶより高速（1トランザクションで完結）。
    """
    conn = get_db()
    try:
        for entry in entries:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO learning_data
                      (post_id, title, genre, price, ab_group, posted_at,
                       likes, replies, reposts, views, engagement_score,
                       fetched_at, image_url, url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry.get("post_id"),
                    entry.get("title"),
                    entry.get("genre"),
                    entry.get("price"),
                    entry.get("ab_group"),
                    entry.get("posted_at"),
                    entry.get("likes", 0),
                    entry.get("replies", 0),
                    entry.get("reposts", 0),
                    entry.get("views", 0),
                    entry.get("engagement", entry.get("engagement_score", 0)),
                    entry.get("fetched_at"),
                    entry.get("image_url"),
                    entry.get("url"),
                ))
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()


def get_learning_data_by_genre(genre: str) -> list[dict]:
    """ジャンルで絞った学習データを返す。"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, post_id, title, genre, price, ab_group, posted_at,
                   likes, replies, reposts, views,
                   engagement_score AS engagement,
                   fetched_at, image_url, url
            FROM learning_data WHERE genre=? ORDER BY posted_at
        """, (genre,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_ab_performance() -> dict[str, float]:
    """A/B グループ別の平均エンゲージメントスコアを返す。

    呼び出し元: ab_tester.auto_adjust_ab_ratio（グループ比率の自動調整判定）
    Returns: {"A": 3.5, "B": 4.2} のように {グループ名: 平均スコア} を返す
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ab_group, AVG(engagement_score) AS avg_score, COUNT(*) AS cnt
            FROM learning_data
            WHERE ab_group IS NOT NULL
            GROUP BY ab_group
        """).fetchall()
        return {row["ab_group"]: round(row["avg_score"], 2) for row in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# posts_queue CRUD
# ---------------------------------------------------------------------------

def get_all_queue_entries() -> list[dict]:
    """全キューエントリを id 昇順で返す（旧 _load_queue() の代替）。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM posts_queue ORDER BY id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_pending_queue() -> list[dict]:
    """status='pending' のエントリを id 昇順で返す。

    呼び出し元: post_manager.print_queue_status / post_manager.refill_queue
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM posts_queue WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_pending_queue() -> int:
    """pending エントリ数を返す。"""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM posts_queue WHERE status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()


def get_queue_urls() -> set[str]:
    """キュー内の全 URL セットを返す（重複挿入防止用）。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT url FROM posts_queue WHERE url IS NOT NULL").fetchall()
        return {row["url"] for row in rows}
    finally:
        conn.close()


def insert_queue_entry(entry: dict) -> None:
    """キューに1件挿入する。"""
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO posts_queue
              (url, affiliate_url, title, price, genre, image_url, post_text,
               ab_group, status, created_at, sent_at, rating_str, free_shipping,
               review_quote, product_description, review_average, review_count,
               rank, point_rate, postage_flag, price_raw, price_avg_30d, discount_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("url"), entry.get("affiliate_url"), entry.get("title"),
            entry.get("price"), entry.get("genre"), entry.get("image_url"),
            entry.get("post_text"), entry.get("ab_group"),
            entry.get("status", "pending"), entry.get("created_at"), entry.get("sent_at"),
            entry.get("rating_str"), 1 if entry.get("free_shipping") else 0,
            entry.get("review_quote"), entry.get("product_description"),
            entry.get("review_average", 0), entry.get("review_count", 0),
            entry.get("rank", 0), entry.get("point_rate", 1),
            entry.get("postage_flag", 0),
            entry.get("price_raw", 0),
            entry.get("price_avg_30d"),
            entry.get("discount_rate", 0),
        ))
        conn.commit()
    finally:
        conn.close()


def update_queue_entry_status(entry_id: int, status: str, sent_at: str | None) -> None:
    """指定 id のエントリのステータスを更新する。

    呼び出し元:
      - threads_poster.py: 投稿完了後に status='sent', sent_at=<now> を設定
      - main.py: genre 不一致で取り戻した投稿を status='pending', sent_at=None に戻す
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE posts_queue SET status=?, sent_at=? WHERE id=?",
            (status, sent_at, entry_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# follower_history CRUD
# ---------------------------------------------------------------------------

def insert_follower_history(entry: dict) -> None:
    """フォロワー履歴を1件挿入する（analytics_agent.fetch_follower_count から呼ばれる）。"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO follower_history (timestamp, threads, instagram, bluesky) VALUES (?, ?, ?, ?)",
            (entry.get("timestamp"), entry.get("threads"),
             entry.get("instagram"), entry.get("bluesky")),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_follower_history() -> list[dict]:
    """全フォロワー履歴を timestamp 昇順で返す（旧 json.load(FOLLOWER_PATH) の代替）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM follower_history ORDER BY timestamp"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rare_items CRUD
# ---------------------------------------------------------------------------

def _row_to_rare_item(d: dict) -> dict:
    """sqlite3.Row dict を rare_item_monitor が期待する形式に変換する。"""
    dpc_count = d.pop("daily_post_count", 0)
    dpc_date  = d.pop("daily_post_date", None)
    d["daily_post_count"] = {"date": dpc_date or "", "count": dpc_count or 0}
    d["posted"]           = bool(d.get("posted", 0))
    d["sold_out"]         = bool(d.get("sold_out", 0))
    d["sold_out_posted"]  = bool(d.get("sold_out_posted", 0))
    d.setdefault("price_history", [])
    return d


def get_all_rare_items() -> list[dict]:
    """全レアアイテムを返す（price_history は空リスト）。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM rare_items").fetchall()
        return [_row_to_rare_item(dict(r)) for r in rows]
    finally:
        conn.close()


def get_all_rare_items_with_history() -> list[dict]:
    """全レアアイテムを price_history 付きで返す（2クエリで N+1 回避）。"""
    items = get_all_rare_items()
    if not items:
        return items
    conn = get_db()
    try:
        ph_rows = conn.execute(
            "SELECT item_code, price, timestamp FROM rare_item_price_history ORDER BY item_code, timestamp"
        ).fetchall()
    finally:
        conn.close()
    ph_by_code: dict[str, list[dict]] = {}
    for row in ph_rows:
        ph_by_code.setdefault(row["item_code"], []).append(
            {"price": row["price"], "timestamp": row["timestamp"]}
        )
    for item in items:
        item["price_history"] = ph_by_code.get(item["item_code"], [])
    return items


def get_rare_item(item_code: str) -> dict | None:
    """item_code で1件取得（price_history 付き）。"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM rare_items WHERE item_code=?", (item_code,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = _row_to_rare_item(dict(row))
    d["price_history"] = get_rare_item_price_history(item_code)
    return d


def upsert_rare_item(item: dict) -> None:
    """rare_items を INSERT OR REPLACE する（price_history は含まない）。"""
    dpc = item.get("daily_post_count", {})
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO rare_items
              (item_code, name, price, keyword, category, first_seen, last_seen,
               last_checked, last_posted_at, posted, sold_out, sold_out_posted,
               restock_count, daily_post_count, daily_post_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get("item_code"),
            item.get("name"), item.get("price"), item.get("keyword"), item.get("category"),
            item.get("first_seen"), item.get("last_seen"), item.get("last_checked"),
            item.get("last_posted_at"),
            1 if item.get("posted") else 0,
            1 if item.get("sold_out") else 0,
            1 if item.get("sold_out_posted") else 0,
            item.get("restock_count", 0),
            dpc.get("count", 0) if isinstance(dpc, dict) else int(dpc or 0),
            dpc.get("date", "")  if isinstance(dpc, dict) else "",
        ))
        conn.commit()
    finally:
        conn.close()


def update_rare_item_status(item_code: str, **kwargs) -> None:
    """指定フィールドのみ UPDATE する。bool → int 変換あり。"""
    if not kwargs:
        return
    for f in ("posted", "sold_out", "sold_out_posted"):
        if f in kwargs:
            kwargs[f] = 1 if kwargs[f] else 0
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE rare_items SET {set_clause} WHERE item_code=?",
            (*kwargs.values(), item_code),
        )
        conn.commit()
    finally:
        conn.close()


def get_rare_item_price_history(item_code: str) -> list[dict]:
    """item_code の価格履歴を timestamp 昇順で返す。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT price, timestamp FROM rare_item_price_history WHERE item_code=? ORDER BY timestamp",
            (item_code,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_rare_item_price_history(item_code: str, price: int, timestamp: str) -> None:
    """価格履歴を1件追加する。"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO rare_item_price_history (item_code, price, timestamp) VALUES (?, ?, ?)",
            (item_code, price, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def save_rare_item_price_history_bulk(item_code: str, points: list[dict]) -> None:
    """item_code の価格履歴を全件置き換える（DELETE + INSERT）。"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM rare_item_price_history WHERE item_code=?", (item_code,))
        for p in points:
            conn.execute(
                "INSERT INTO rare_item_price_history (item_code, price, timestamp) VALUES (?, ?, ?)",
                (item_code, p.get("price"), p.get("timestamp")),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# price_history CRUD
# ---------------------------------------------------------------------------

def get_price_history(url: str) -> list[dict]:
    """URL の価格履歴を timestamp 昇順で返す。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT price, timestamp FROM price_history WHERE url=? ORDER BY timestamp",
        (url,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_price_history(url: str, price: int, timestamp: str) -> None:
    """価格履歴を1件追加する。"""
    conn = get_db()
    conn.execute(
        "INSERT INTO price_history (url, price, timestamp) VALUES (?, ?, ?)",
        (url, price, timestamp),
    )
    conn.commit()
    conn.close()


def get_all_price_history_urls() -> list[str]:
    """price_history に登録されている URL 一覧を返す。"""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT url FROM price_history WHERE url IS NOT NULL").fetchall()
    conn.close()
    return [row["url"] for row in rows]


def get_all_price_history_as_dict() -> dict[str, list[dict]]:
    """
    {url: [{price, timestamp}, ...]} の形式で全履歴を返す。
    product_selector._get_price_stats() の history 引数との互換性を保つ。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT url, price, timestamp FROM price_history ORDER BY url, timestamp"
    ).fetchall()
    conn.close()
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row["url"], []).append(
            {"price": row["price"], "timestamp": row["timestamp"]}
        )
    return result


# ---------------------------------------------------------------------------
# follow_log CRUD
# ---------------------------------------------------------------------------

def get_today_follow_log(date_str: str, target_follows: int = 20, target_likes: int = 50) -> dict:
    """指定日のフォローログを返す。存在しない場合はデフォルト値でレコードを作成する。"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM follow_log WHERE date = ?", (date_str,)
    ).fetchone()
    if row:
        conn.close()
        return dict(row)
    # 当日レコードが未作成なら INSERT して返す
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO follow_log
           (date, follows, likes, target_follows, target_likes, updated_at)
           VALUES (?, 0, 0, ?, ?, ?)""",
        (date_str, target_follows, target_likes, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM follow_log WHERE date = ?", (date_str,)
    ).fetchone()
    conn.close()
    return dict(row)


def upsert_follow_log(date_str: str, follows: int, likes: int,
                      target_follows: int = 20, target_likes: int = 50,
                      follower_gain_ig: int | None = None,
                      follower_gain_threads: int | None = None,
                      follower_gain_bluesky: int | None = None) -> None:
    """指定日のフォロー実績を上書き保存する。レコードが無ければ作成する。"""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO follow_log
               (date, follows, likes, target_follows, target_likes,
                follower_gain_ig, follower_gain_threads, follower_gain_bluesky, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               follows               = excluded.follows,
               likes                 = excluded.likes,
               target_follows        = excluded.target_follows,
               target_likes          = excluded.target_likes,
               follower_gain_ig      = COALESCE(excluded.follower_gain_ig,      follow_log.follower_gain_ig),
               follower_gain_threads = COALESCE(excluded.follower_gain_threads, follow_log.follower_gain_threads),
               follower_gain_bluesky = COALESCE(excluded.follower_gain_bluesky, follow_log.follower_gain_bluesky),
               updated_at            = excluded.updated_at""",
        (date_str, follows, likes, target_follows, target_likes,
         follower_gain_ig, follower_gain_threads, follower_gain_bluesky, now),
    )
    conn.commit()
    conn.close()


def get_follow_log_recent(days: int = 7) -> list[dict]:
    """直近 days 日のフォローログを新しい順で返す。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM follow_log ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# ab_test_results CRUD
# ---------------------------------------------------------------------------

def save_ab_result(pattern: str, post_id: str, engagement_rate: float,
                   followers_at_post: int, genre: str, posted_at: str,
                   views: int = 0) -> None:
    """A/Bテスト結果を1件保存する（post_id で UPSERT）。"""
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO ab_test_results
              (pattern, post_id, engagement_rate, followers_at_post, posted_at, genre, views)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (pattern, post_id, engagement_rate, followers_at_post, posted_at, genre, views))
        conn.commit()
    finally:
        conn.close()


def get_ab_stats() -> dict[str, dict]:
    """A/B 別の平均エンゲージメント率・投稿数・最古の posted_at を返す。

    Returns:
        {"A": {"avg_rate": float, "count": int, "started_at": str}, "B": {...}}
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT pattern,
                   AVG(engagement_rate) AS avg_rate,
                   COUNT(*)             AS count,
                   MIN(posted_at)       AS started_at
            FROM ab_test_results
            WHERE pattern IS NOT NULL
            GROUP BY pattern
        """).fetchall()
        return {row["pattern"]: {
            "avg_rate":   round(row["avg_rate"] or 0.0, 8),
            "count":      row["count"],
            "started_at": row["started_at"],
        } for row in rows}
    finally:
        conn.close()


def get_recent_ab_results(limit: int = 10) -> list[dict]:
    """直近 limit 件の A/B テスト結果を posted_at 降順で返す。"""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM ab_test_results
            ORDER BY posted_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_low_performance_products(follower_count: int) -> set[str]:
    """低パフォーマンス商品 URL のセットを返す（商品停止判定用）。

    呼び出し元: product_selector.select_top_products（低効果商品を選定対象から除外）

    対象条件（全て満たす場合のみ）:
    - 投稿後 48 時間以上経過
    - 当該 URL の投稿数 >= 5 件
    - 平均 engagement_rate が閾値未満
      follower_count < 200  → 閾値 0.001
      follower_count >= 200 → 閾値 0.003

    engagement_rate は learning_data.engagement_score / max(follower_count, 1) で近似。
    """
    rate_threshold  = 0.001 if follower_count < 200 else 0.003
    safe_followers  = max(follower_count, 1)
    cutoff          = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT url,
                   COUNT(id)                                               AS post_count,
                   AVG(CAST(engagement_score AS REAL) / ?)                AS avg_rate
            FROM learning_data
            WHERE url IS NOT NULL
              AND url != ''
              AND posted_at <= ?
            GROUP BY url
            HAVING post_count >= 5 AND avg_rate < ?
        """, (safe_followers, cutoff, rate_threshold)).fetchall()
        return {row["url"] for row in rows}
    finally:
        conn.close()


def get_recent_post_genres(n: int = 5) -> list[str]:
    """直近 n 件の投稿ジャンルを新しい順で返す（post_logs から取得）。

    呼び出し元: product_selector.select_top_products（同ジャンル連続投稿を避けるジャンル均等化）
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT genre FROM post_logs
            WHERE genre IS NOT NULL AND genre != '' AND status = 'success'
            ORDER BY posted_at DESC LIMIT ?
        """, (n,)).fetchall()
        return [row["genre"] for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 起動時に自動 init
# ---------------------------------------------------------------------------

init_db()


if __name__ == "__main__":
    print("=== database.py 動作確認 ===")
    stats = migrate()
    conn = get_db()
    pl = conn.execute("SELECT COUNT(*) FROM post_logs").fetchone()[0]
    ld = conn.execute("SELECT COUNT(*) FROM learning_data").fetchone()[0]
    pq = conn.execute("SELECT COUNT(*) FROM posts_queue").fetchone()[0]
    fh = conn.execute("SELECT COUNT(*) FROM follower_history").fetchone()[0]
    ri = conn.execute("SELECT COUNT(*) FROM rare_items").fetchone()[0]
    rp = conn.execute("SELECT COUNT(*) FROM rare_item_price_history").fetchone()[0]
    ph = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    conn.close()
    print(f"post_logs              : {pl} 件")
    print(f"learning_data          : {ld} 件")
    print(f"posts_queue            : {pq} 件")
    print(f"follower_history       : {fh} 件")
    print(f"rare_items             : {ri} 件")
    print(f"rare_item_price_history: {rp} 件")
    print(f"price_history          : {ph} 件")
    print(f"AB performance: {get_ab_performance()}")

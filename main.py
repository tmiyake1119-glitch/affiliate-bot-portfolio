"""
main.py — AffiliateBot メインエントリーポイント

【処理の全体フロー】
  1. 一時停止ファイル（pause.txt）チェック
  2. フォロワー戦略に基づく投稿可否判定
  3. トークン有効期限チェック・更新
  4. Step 1: 楽天APIからトレンド商品を取得
  5. Step 2–4: アカウント別に商品選定 → フォーマット → 投稿文生成 → キュー保存
  6. Step 6: Threads / Instagram / Bluesky への実投稿（BANリスク・日次上限チェック込み）
  7. Step 7: エンゲージメント分析・フォロワー数記録・A/Bテスト評価
  8. Step 8 (月曜): 週次レポート生成・A/B比率自動調整・投稿文改善
  9. Step 9 (日曜): ダイジェスト投稿・週次トレンド分析
  10. Step 10 (21時): エンゲージメント投稿・夜のフォローリマインダー
  11. Step 11–13: 比較投稿(水)・価格帯投稿(木)・月次ランキング(月末)
  12. Step 14: レアアイテム在庫監視
  13. Step 15: DBバックアップ

【マルチアカウント構成】
  accounts.json で管理する3アカウント:
    - mono_zukan_jp  : 雑貨・日用品（デフォルトアカウント）
    - osakezukan_jp  : お酒・飲料
    - hobbyzukan_jp  : ホビー・趣味
  各アカウントはジャンルフィルターを持ち、CtRウェイトも独立管理する。

【cronスケジュール（VPS設定）】
  1日4回実行 → 9:00 / 13:00 / 17:00 / 21:00 JST（目安）
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'agents'))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))

from logger_setup import setup_logger
logger = setup_logger()

from trend_agent      import get_trending_products
from product_selector import select_top_products
from formatter        import format_products
from post_generator   import generate_posts
from post_manager     import save_posts, print_queue_status, refill_queue
from threads_poster    import post_to_threads, get_next_post
from instagram_poster  import post_to_instagram
from bluesky_poster    import post_to_bluesky
from x_poster          import post_to_x
from analytics_agent   import fetch_all_insights, print_summary, fetch_follower_count, evaluate_ab_test
from weekly_report     import save_and_print_report
from discord_notifier  import send_discord, send_discord_error, send_discord_report, send_discord_error_embed, check_instagram_rename_reminder
from token_renewal     import check_and_renew, check_threads_token_expiry
from digest_poster      import post_digest
from engagement_poster  import post_engagement
from comparison_poster  import post_comparison
from monthly_ranking    import post_monthly_ranking
from price_range_poster import post_price_range
from rare_item_monitor  import run_rare_item_monitor
from image_generator    import generate_image
from image_uploader     import upload_image
from post_counter       import can_post, record_post
from ab_tester          import auto_adjust_ab_ratio
from ban_risk_monitor      import check_ban_risk, check_resume
from follower_strategy     import get_action_for_hour, check_follower_milestones
from strategy_agent        import run_strategy_meeting
from auto_recovery_agent   import check_recovery, run_auto_recovery, load_recovery_config
from follow_reminder       import send_morning_reminder, send_evening_reminder
from db_backup             import run_db_backup

# pause.txt が存在する間、全投稿処理をスキップする（緊急停止ファイル）
PAUSE_PATH    = os.path.join(os.path.dirname(__file__), 'pause.txt')
# マルチアカウント定義ファイル（name / genre_filter / threads_token などを管理）
ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), 'accounts.json')


def _load_accounts() -> list[dict]:
    """accounts.json を読み込み、トークン未設定項目を env 変数で補完して返す。

    accounts.json が存在しない場合は env 変数のみで "main" アカウントを構築する。
    これにより単一アカウント構成（旧来の動作）との後方互換性を保つ。
    """
    # Threads デフォルト認証情報（accounts.json で未指定のアカウントに適用）
    default_token   = os.getenv("THREADS_ACCESS_TOKEN", "")
    default_user_id = os.getenv("THREADS_USER_ID", "me")

    if not os.path.exists(ACCOUNTS_PATH):
        return [{"name": "main", "genre_filter": None,
                 "threads_token": default_token,
                 "threads_user_id": default_user_id}]

    with open(ACCOUNTS_PATH, 'r', encoding='utf-8') as f:
        accounts = json.load(f)

    for acc in accounts:
        if not acc.get("threads_token"):
            acc["threads_token"] = default_token
        if not acc.get("threads_user_id"):
            acc["threads_user_id"] = default_user_id

    return accounts


def _filter_for_account(formatted: list[dict], account: dict) -> list[dict]:
    """アカウントの genre_filter に合致する商品だけを返す（None = 全ジャンル対象）。

    genre_filter は accounts.json に配列形式で定義する。例:
      "genre_filter": ["お酒", "ビール"]  → 指定ジャンルのみ投稿
      "genre_filter": null               → 全ジャンルを投稿（mono_zukan_jp デフォルト）
    """
    genre_filter = account.get("genre_filter")  # None or ["ジャンルA", "ジャンルB"]
    if not genre_filter:
        return formatted
    return [p for p in formatted if p.get("genre") in genre_filter]


def main() -> None:
    start = datetime.now(timezone.utc)
    print(f"[{start.strftime('%Y-%m-%d %H:%M:%S')} UTC] Starting affiliate bot\n")
    try:
        _main(start)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[FATAL] 未捕捉の例外が発生しました:\n{tb}")
        send_discord_error_embed("main", str(e), "AffiliateBot 起動サイクル")
    finally:
        end = datetime.now(timezone.utc)
        print(f"[{end.strftime('%Y-%m-%d %H:%M:%S')} UTC] Bot cycle finished")


def _main(start: datetime) -> None:
    """メイン処理本体。main() の try/except から呼ばれる。

    start: UTC タイムスタンプ（実行サイクル開始時刻）
    """

    # ──────────────────────────────────────────
    # 緊急停止チェック: pause.txt が存在する間は全処理をスキップ
    # ──────────────────────────────────────────
    if os.path.exists(PAUSE_PATH):
        print("⏸ Bot is paused (pause.txt found). Skipping all posting steps.")
        send_discord("⏸ Bot is paused — pause.txt が存在するため投稿をスキップします。")
        return

    send_discord("🤖 AffiliateBotが起動しました")

    # フォロワー数連動の投稿戦略を取得
    # start_jst: JST換算の実行開始時刻（時間帯判定・時刻ログ用）
    start_jst = start + timedelta(hours=9)
    jst_hour = start_jst.hour  # JST の時（0–23）: Step選択・時刻限定処理の判定に使う
    # posting_action: "post" / "engagement" / "skip" のいずれか
    posting_action = get_action_for_hour(jst_hour)
    print(f"[Strategy] JST {jst_hour}時 → 投稿アクション: {posting_action}")

    if posting_action == "skip":
        print(f"[Strategy] この時間帯は投稿スキップ (JST {jst_hour}時)")
        send_discord(f"⏭ [Strategy] JST {jst_hour}時はスキップ時間帯のため投稿をスキップします。")
        return

    # ──────────────────────────────────────────
    # 定期チェック: Instagramアカウント名変更リマインダー（特定日のみ発火）
    # ──────────────────────────────────────────
    check_instagram_rename_reminder()

    # ──────────────────────────────────────────
    # トークン有効期限チェック・自動更新
    #   Instagram: 60日ごとに自動更新（Long-Lived Token）
    #   Threads  : 期限通知のみ（手動更新が必要）
    # ──────────────────────────────────────────
    renewal_msg = check_and_renew()
    if renewal_msg:
        send_discord(renewal_msg)

    threads_token_msg = check_threads_token_expiry()
    if threads_token_msg:
        send_discord(threads_token_msg)

    # accounts: マルチアカウントのリスト（accounts.json から読み込み）
    accounts = _load_accounts()
    print(f"Accounts loaded: {[a['name'] for a in accounts]}\n")

    # ══════════════════════════════════════════
    # Step 1: 楽天APIからトレンド商品を取得
    #   全アカウント共通で1回だけ取得し、以降はアカウントごとにフィルタリングする。
    #   取得件数は trend_agent.py の設定に依存（ジャンル×ランキングAPI）
    # ══════════════════════════════════════════
    print("--- Step 1: Fetching trending products ---")
    all_products = get_trending_products()  # 全ジャンルの楽天ランキング商品リスト
    print(f"Fetched {len(all_products)} products\n")

    # ══════════════════════════════════════════
    # Step 2–4: アカウント別に商品選定 → フォーマット → 投稿文生成 → キュー保存
    #   Step 2: select_top_products  — スコアリングで上位商品を選定（CTRウェイトはアカウント別）
    #   Step 3: format_products      — 楽天APIレスポンスを投稿用dict形式に整形
    #   Step 4: generate_posts       — OpenAI で投稿文を生成し posts_queue に保存
    # ④ account_name を渡してアカウント専用CTRウェイトで選定する
    # ══════════════════════════════════════════
    print("--- Step 2–4: Account-specific product selection, formatting and post generation ---")
    added_total  = 0   # 全アカウント合計の新規キュー追加数（サマリー表示用）
    top_products = []  # サマリー表示用（最後のアカウントの結果）
    formatted    = []  # フォーマット済み商品リスト（最後のアカウントの結果）

    for account in accounts:
        acc_name = account["name"]
        acc_top  = select_top_products(all_products, account_name=acc_name)
        if not acc_top:
            print(f"  [{acc_name}] No products selected — skipping.")
            continue
        acc_formatted = format_products(acc_top)
        acc_filtered  = _filter_for_account(acc_formatted, account)
        if not acc_filtered:
            print(f"  [{acc_name}] No products matching genre filter — skipping.")
            continue
        acc_posts = generate_posts(acc_filtered, account_name=acc_name)
        added = save_posts(acc_filtered, acc_posts)
        added_total += added
        print(f"  [{acc_name}] Selected {len(acc_top)}, filtered {len(acc_filtered)}, "
              f"added {added} to queue")
        top_products = acc_top
        formatted    = acc_formatted
    print_queue_status()
    print()

    # ──────────────────────────────────────────
    # Step 5b: キュー残数が少ない場合に自動補充（min_pending=3 を下回ったら追加生成）
    # ──────────────────────────────────────────
    refilled = refill_queue(min_pending=3)  # 補充された件数
    if refilled:
        print_queue_status()
    print()

    # ══════════════════════════════════════════
    # Step 6: 各SNSへの実投稿（アカウントループ）
    #   処理順: Threads → Instagram → Bluesky（X は APIクレジット有料のため無効化）
    #   各投稿前に以下のチェックを通過する必要がある:
    #     - BANリスク評価（CRITICAL: 全スキップ / WARNING: 継続）
    #     - 日次投稿上限チェック（post_counter.py）
    #     - AutoRecovery による停止フラグ（プラットフォーム別・アフィリ全体）
    # ══════════════════════════════════════════
    print("--- Step 6: Posting to Threads (per account) ---")

    # 自動回復チェック（停止期間終了の解除）& 異常検知
    # recovery_cfg: AutoRecovery の設定dict（停止フラグ・インターバル倍率など）
    recovery_cfg = check_recovery()
    recovery_cfg = run_auto_recovery()
    # _interval_multiplier: 投稿間隔の延長倍率（通常=1.0 / 回復中=2.0〜）
    _interval_multiplier    = recovery_cfg.get("post_interval_multiplier", 1.0)
    # _affiliate_suspended: True のとき全アカウントのアフィリ投稿を停止
    _affiliate_suspended    = recovery_cfg.get("affiliate_suspended", False)
    # _suspended_platforms: 停止中プラットフォーム名のset（例: {"threads", "instagram"}）
    _suspended_platforms    = set(recovery_cfg.get("suspended_platforms") or [])

    if _affiliate_suspended:
        until = recovery_cfg.get("suspended_until", "")[:19]
        print(f"  [AutoRecovery] アフィリ投稿停止中 (until {until} UTC) → Step 6 全スキップ")

    # BANリスク評価（投稿ループ開始前に1回チェック）
    total_sent = 0
    _ban_skip = check_resume()
    if _ban_skip:
        print("  [BanRisk] 停止中 → Step 6 全スキップ")
    else:
        ban_level = check_ban_risk()
        if ban_level == "CRITICAL":
            print("  [BanRisk] CRITICAL → Step 6 全スキップ")
            _ban_skip = True
        elif ban_level == "WARNING":
            print("  [BanRisk] WARNING — 投稿は続行")
    for account in accounts:
        if _ban_skip:
            print(f"  [BanRisk] 停止中 → {account['name']} スキップ")
            continue

        acc_name     = account["name"]
        genre_filter = account.get("genre_filter")
        print(f"\n  [{acc_name}] genre_filter={genre_filter}")

        # AutoRecovery: アフィリ停止中はスキップ
        if _affiliate_suspended:
            print(f"  [{acc_name}] [AutoRecovery] アフィリ停止中 → スキップ")
            continue

        # ① 日次上限チェック
        if not can_post(acc_name, "normal"):
            from post_counter import MAX_DAILY_POSTS
            print(f"  [{acc_name}] 日次上限到達（上限 {MAX_DAILY_POSTS.get(acc_name, 6)}件）— スキップ")
            continue

        post = get_next_post()
        if not post:
            print(f"  [{acc_name}] No pending posts in queue.")
            continue

        # If this account has a genre filter and the post doesn't match, put it back
        if genre_filter and post.get("genre") not in genre_filter:
            print(f"  [{acc_name}] Skipping post (genre={post.get('genre')} not in filter).")
            # The post was already marked sent by get_next_post; re-mark as pending
            from database import update_queue_entry_status
            update_queue_entry_status(post["id"], "pending", None)
            continue

        # ④ 画像生成（投稿前に実施してCloudinary URLを確保）
        image_path = generate_image(post)
        if not image_path:
            print(f"  [{acc_name}] 画像生成失敗 → 投稿スキップ")
            continue

        # ② Cloudinary アップロード — Instagram 用 URL を post dict に注入
        # 失敗時は post["image_url"] の元の楽天URLを維持する（instagram_poster が proxy してくれる）
        image_url = upload_image(image_path)
        if image_url:
            post["image_url"] = image_url
        else:
            print(f"  [{acc_name}] Cloudinary アップロード失敗 → 元の画像URLで継続")

        # ── TEST MODE: 投稿せずに内容をコンソール表示して終了 ──────────
        if os.getenv("TEST_MODE") == "true":
            print("\n===== TEST MODE =====")
            print(post.get("post_text", ""))
            print(f"image_path: {image_path}")
            print(f"image_url:  {image_url}")
            print("=====================\n")
            continue

        # AutoRecovery: Threads 停止チェック
        if "threads" in _suspended_platforms:
            print(f"  [{acc_name}] [AutoRecovery] Threads停止中 → スキップ")
        else:
            result = post_to_threads(post, account=account)
            if result["status"] == "success":
                total_sent += 1
                record_post(acc_name, "normal", post.get("url", ""), posted=True)  # ① ⑧
                send_discord(
                    f"✅ [{acc_name}] 投稿完了: {post['title'][:50]} "
                    f"({post['genre']}) {post['price']} [Group {post.get('ab_group', 'A')}]"
                )
            else:
                record_post(acc_name, "normal", post.get("url", ""), posted=False)  # ⑧
                send_discord_error_embed(acc_name, result['error'] or "", post.get('title', ''))

        # Instagram 投稿:
        #   instagram_access_token_env 指定あり → そのenv変数からトークン取得（osake/hobby）
        #   指定なし → デフォルトの INSTAGRAM_ACCESS_TOKEN を使用（mono_zukan_jp）
        #   どちらの場合も認証情報があれば必ず投稿を試みる
        ig_token_env   = account.get("instagram_access_token_env", "INSTAGRAM_ACCESS_TOKEN")
        ig_user_id_env = account.get("instagram_user_id_env",      "INSTAGRAM_USER_ID")
        has_ig_creds   = bool(os.getenv(ig_token_env)) and bool(os.getenv(ig_user_id_env))

        if "instagram" in _suspended_platforms:
            print(f"  [{acc_name}] [Instagram] [AutoRecovery] 停止中 → スキップ")
        elif not has_ig_creds:
            print(f"  [{acc_name}] [Instagram] 認証情報未設定（{ig_token_env}）→ スキップ")
        elif not post.get("image_url"):
            # Cloudinary・楽天URLの両方がない場合のみスキップ
            # （Cloudinary失敗時は楽天URLが残っているので instagram_poster の proxy に任せる）
            print(f"  [{acc_name}] [Instagram] 画像URLなし → スキップ")
        else:
            ig_result = post_to_instagram(post, account=account)
            if ig_result["status"] == "success":
                send_discord(f"✅ [Instagram/{acc_name}] 投稿完了: {post['title'][:50]}")
            elif ig_result["status"] == "error":
                send_discord_error_embed(f"Instagram/{acc_name}", ig_result['error'] or "", post.get('title', ''))
            elif ig_result["status"] == "skipped":
                print(f"  [{acc_name}] [Instagram] スキップ: {ig_result.get('error', '')}")

        # Bluesky は mono_zukan_jp のみ
        if acc_name == "mono_zukan_jp":
            if "bluesky" in _suspended_platforms:
                print("  [Bluesky] [AutoRecovery] 停止中 → スキップ")
            else:
                bsky_result = post_to_bluesky(post)
                if bsky_result["status"] == "success":
                    send_discord(f"✅ [Bluesky] 投稿完了: {post['title'][:50]}")
                elif bsky_result["status"] == "error":
                    send_discord_error_embed("Bluesky", bsky_result['error'] or "", post.get('title', ''))

        # X posting disabled - API requires paid credits
        # x_result = post_to_x(post)
        # if x_result["status"] == "success":
        #     send_discord(f"✅ [X] 投稿完了: {post['title'][:50]}")
        # elif x_result["status"] == "error":
        #     send_discord_error(f"❌ [X] 投稿失敗: {x_result['error']}")

        # AutoRecovery: 投稿間隔multiplier（複数アカウント間の待機）
        if _interval_multiplier > 1.0 and account != accounts[-1]:
            import time
            sleep_sec = int(30 * _interval_multiplier)
            print(f"  [AutoRecovery] 投稿間隔延長: {sleep_sec}秒待機 (×{_interval_multiplier})")
            time.sleep(sleep_sec)

    # ══════════════════════════════════════════
    # Step 7: エンゲージメント分析・学習データ更新
    #   Threads / Instagram API からいいね・リプライ・インプレッション等を取得し
    #   learning_data テーブルへ upsert する（product_selector のスコアリングに反映）
    # ══════════════════════════════════════════
    print("\n--- Step 7: Fetching engagement analytics ---")
    insights = fetch_all_insights()  # [{post_id, likes, replies, views, engagement_score, ...}, ...]
    print_summary(insights)

    # ──────────────────────────────────────────
    # Step 7b: フォロワー数を記録（全プラットフォーム）
    #   follower_history テーブルへ追記し、成長トレンドをトラッキングする
    # ──────────────────────────────────────────
    print("\n--- Step 7b: Recording follower counts ---")
    fetch_follower_count()

    # ──────────────────────────────────────────
    # Step 7f: A/Bテスト評価
    #   A/Bグループ別の平均エンゲージメント率を集計し、有意差があれば Discord に通知
    # ──────────────────────────────────────────
    print("\n--- Step 7f: A/B test evaluation ---")
    evaluate_ab_test()

    # フォロワー数がマイルストーン（100・500・1000 等）を超えた場合に Discord 通知
    check_follower_milestones()

    # ──────────────────────────────────────────
    # Step 7d: 戦略ミーティング + 朝のフォローリマインダー（9:00 JST のみ）
    #   run_strategy_meeting: 直近データを基に投稿戦略をClaude APIで生成しDiscordへ送信
    #   send_morning_reminder: 今日のフォロー目標をDiscordに通知
    # ──────────────────────────────────────────
    if start_jst.hour == 9:
        print("\n--- Step 7d: Strategy meeting (9:00 JST) ---")
        run_strategy_meeting()
        print("\n--- Step 7e: Morning follow reminder (9:00 JST) ---")
        send_morning_reminder()

    # ══════════════════════════════════════════
    # Step 8: 週次レポート・A/B自動調整・投稿文改善（月曜のみ）
    #   Step 8 : 週次レポートをDBから生成してDiscordに送信
    #   Step 8b: A/Bテスト結果に基づいてグループ比率を自動調整
    #   Step 8c: 低エンゲージメント投稿文を自動改善（月曜9時のみ）
    # ══════════════════════════════════════════
    if start.weekday() == 0:  # 月曜日 (0=Monday)
        print("\n--- Step 8: Generating weekly report (Monday) ---")
        report = save_and_print_report()
        send_discord_report(f"📊 週次レポート\n```\n{report}\n```")

        print("\n--- Step 8b: A/B ratio auto-adjustment ---")
        auto_adjust_ab_ratio()

        # Step 8c: 投稿文自動改善ループ（月曜9時のみ）
        if start_jst.hour == 9:
            print("\n--- Step 8c: Content optimizer (Monday 9:00 JST) ---")
            from content_optimizer import run_content_optimizer
            run_content_optimizer()

    # ══════════════════════════════════════════
    # Step 9: ダイジェスト投稿・週次トレンド分析（日曜のみ）
    #   post_digest    : 週間人気商品まとめ投稿
    #   run_trend_analyzer: 今週のトレンドをClaude APIで分析してDiscordに送信（日曜9時のみ）
    # ══════════════════════════════════════════
    if start.weekday() == 6:  # 日曜日 (6=Sunday)
        print("\n--- Step 9: Posting weekly digest (Sunday) ---")
        post_digest()

        # Step 9b: 週次トレンド分析（日曜9時のみ）
        if start_jst.hour == 9:
            print("\n--- Step 9b: Trend analyzer (Sunday 9:00 JST) ---")
            from trend_analyzer import run_trend_analyzer
            run_trend_analyzer()

    # ══════════════════════════════════════════
    # Step 10: エンゲージメント投稿・夜のフォローリマインダー（21時 or engagement アクション）
    #   post_engagement : フォロワーとの対話を促す質問・アンケート形式の投稿
    #   send_evening_reminder: 夜のフォロー進捗をDiscordに通知（21時のみ）
    # ══════════════════════════════════════════
    if start_jst.hour == 21 or posting_action == "engagement":  # 21:00 JST
        print("\n--- Step 10: Posting daily engagement post ---")
        post_engagement()
    if start_jst.hour == 21:  # 21:00 JST のみ夜のリマインダー
        print("\n--- Step 10c: Evening follow reminder (21:00 JST) ---")
        send_evening_reminder()

    # ──────────────────────────────────────────
    # Step 10b: 1日のサマリー通知（22時のみ）
    #   本日の投稿数・エンゲージメント合計をDiscordに送信
    # ──────────────────────────────────────────
    if start_jst.hour == 22:  # 22:00 JST
        print("\n--- Step 10b: Daily summary Discord notification ---")
        from daily_summary_notifier import post_daily_summary
        post_daily_summary()

    # ──────────────────────────────────────────
    # Step 11: 商品比較投稿（水曜のみ）
    #   同ジャンル2商品を比較する形式で投稿しエンゲージメントを高める
    # ──────────────────────────────────────────
    if start.weekday() == 2:  # 水曜日 (2=Wednesday)
        print("\n--- Step 11: Posting product comparison (Wednesday) ---")
        post_comparison()

    # ──────────────────────────────────────────
    # Step 12: 価格帯別特集投稿（木曜のみ）
    #   「1000円以下で買えるおすすめ」等の予算別まとめを投稿
    # ──────────────────────────────────────────
    if start.weekday() == 3:  # 木曜日 (3=Thursday)
        print("\n--- Step 12: Posting budget special (Thursday) ---")
        post_price_range()

    # ──────────────────────────────────────────
    # Step 13: 月次ランキング投稿（月末最終日のみ）
    #   今月最も反響のあった商品TOP5を投稿する
    # ──────────────────────────────────────────
    import calendar
    last_day_of_month = calendar.monthrange(start.year, start.month)[1]  # 月の最終日（28〜31）
    if start.day == last_day_of_month:
        print("\n--- Step 13: Posting monthly ranking (last day of month) ---")
        post_monthly_ranking()

    # ══════════════════════════════════════════
    # Step 14: レアアイテム在庫監視（毎回実行）
    #   楽天APIで在庫状況を確認し、入荷・売切れを検知したらDiscordに通知する
    # ══════════════════════════════════════════
    print("\n--- Step 14: Rare item stock monitor ---")
    run_rare_item_monitor()

    # ══════════════════════════════════════════
    # Step 15: DBバックアップ（毎回実行）
    #   affiliate_bot.db を data/backups/ にコピーする（7日以上古いバックアップは自動削除）
    # ══════════════════════════════════════════
    print("\n--- Step 15: DB backup ---")
    run_db_backup()

    # Summary
    end = datetime.now(timezone.utc)
    elapsed = (end - start).total_seconds()
    print(f"\n{'='*40}")
    print(f"  Summary")
    print(f"{'='*40}")
    print(f"[{start_jst.strftime('%Y-%m-%d %H:%M:%S')} JST] Starting affiliate bot")
    print(f"  Start      : {start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  End        : {end.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Elapsed    : {elapsed:.1f}s")
    print(f"  Accounts   : {len(accounts)}")
    print(f"  Fetched    : {len(all_products)} products")
    print(f"  Selected   : {len(top_products)} products")
    print(f"  Queued     : {added_total} new post(s) (+{refilled} refill)")
    print(f"  Sent       : {total_sent} post(s)")
    print(f"  Analyzed   : {len(insights)} post(s)")
    print(f"{'='*40}")


if __name__ == "__main__":
    main()

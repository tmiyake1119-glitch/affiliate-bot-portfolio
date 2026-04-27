import os
import random
import sys
from datetime import datetime, timezone

import requests

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from discord_notifier import send_discord
from database import insert_post_log

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID      = os.getenv("THREADS_USER_ID", "me")

# Fallback pool (used when no weekday-specific posts exist)
ENGAGEMENT_POSTS = [
    "こんばんはだよ！ずかぽんだぽん🔍\n今日も楽天でお得なアイテムを探してたよ！\n良いものが見つかったらどんどんシェアするね😊",
    "夜の楽天チェック、してる人いる？🌙\nずかぽんと一緒にお得を探そうだよ！\n今夜も良い発見があるといいなぽん！",
    "ボク、今日も図鑑を更新したよ！📖\n楽天でコスパ最高なアイテムを発見しまくってるだよ。\nみんなのお役に立てたら嬉しいぽん✨",
    "楽天でお得に買うコツ、ずかぽん流💡\n\n①ポイント倍率アップの日を狙うだよ！\n②お気に入り登録でセールを逃さないだよ！\n③レビュー100件以上は信頼度高めだぽん！\n\nこれで節約できる金額が変わるよ！",
    "知ってた？楽天スーパーセールは月1回あるんだよ！🛒\nずかぽんはカートにいれておいて一気買いするだよ。\nこれが図鑑的にも正解のお買い物術だぽん！",
    "レビュー4.5以上の商品はハズレが少ないよ📊\nずかぽん調べだけど、かなり信頼できる指標だよ！\nレビュー100件以上あれば安心して買えるぽん。",
    "ふるさと納税、まだやってない人いる？🏡\n節税しながら美味しいものが届くんだよ！\n楽天ふるさと納税はポイントも貯まって一石二鳥だぽん！",
    "良い買い物できたときの達成感、最高だよね✨\n値段じゃなくて、自分にぴったりなものを選べたとき。\nずかぽんも今日そんな発見があったよ！",
    "夜のネットショッピングタイム🛍️\nゆっくり比較して掘り出し物を探すのが好きだよ！\nみんなは今気になってるものある？教えてほしいぽん！",
    # 投票型
    "まとめ買い派？それとも必要な時だけ派？🛒\nずかぽんはまとめ買い派だよ！\n\nA: まとめ買いでポイント効率UP\nB: 必要な時だけ、無駄ゼロ\n\nどっち派か教えてだよ👇",
    "楽天で買うなら優先するのはどっち？💰\nずかぽんはポイント派だぽん！\n\nA: ポイント還元率重視\nB: 価格の安さ重視\n\nコメントで教えてほしいよ！",
    "届いた商品の開封、どっち派だよ？📦\nずかぽんはもちろんすぐ開けるだよ！\n\nA: 届いたらすぐ開ける\nB: 落ち着いてから後で開ける\n\n正直に教えてだぽん😂",
    "お買い物前にレビューは読む？⭐\nずかぽんは必ず読むだよ！\n\nA: 必ず読んでから買う\nB: あまり読まない・直感派\n\nどちらが多数派か気になるぽん！",
    "ネットショッピングするのはいつ？🌙\nずかぽんは夜派だよ！\n\nA: 朝にまとめてチェック\nB: 夜にじっくり選ぶ\n\nぜひ教えてほしいだよ👇",
]

# Day-of-week themed posts (0=Monday … 6=Sunday)
WEEKDAY_POSTS: dict[int, list[str]] = {
    0: [  # Monday — 今週の目標・お買い物計画
        "月曜日🌱 今週のお買い物計画を立てよう！\n\n欲しいものリストを作っておくと\n衝動買いが減って節約につながります💡\n今週もお得な情報をシェアしていきます！",
        "新しい週がスタート🗓️\n今週の目標：本当に必要なものだけを賢く買う。\n一緒に良い買い物しましょう！",
        "月曜日は計画の日📋\n今月の楽天ポイント残高チェックしましたか？\n期限切れになる前に使い切ろう！",
        "月曜日の投票🗳️ 今週のショッピング予定は？\n\nA: 今週は節約モード、買わない\nB: 気になるものがあれば買う\n\nコメントで教えてください！",
    ],
    1: [  # Tuesday — お得な情報・ポイント活用術
        "火曜日💳 ポイント活用術を紹介！\n\n楽天ポイントは期間限定ポイントから使うのが鉄則。\n気づいたら失効してた…なんてもったいないですよ😅",
        "今日はお得情報デー🔥\n楽天カード＋楽天市場の組み合わせで\nポイント還元率がぐっと上がります。\n知ってる人は絶対やってるやつ。",
        "火曜日は情報収集の日📊\nセール前にほしいものをカートに入れておく習慣、\nおすすめです。比較もしやすいし忘れない！",
        "火曜日の投票💳 楽天ポイントの使い方は？\n\nA: 貯めてまとめて使う\nB: 毎回少しずつ使う\n\nどちら派か教えてください！",
    ],
    2: [  # Wednesday — 週の折り返し・おすすめ商品
        "水曜日🐪 週の折り返し！\n\n今週もお得な商品を紹介してきました。\n見逃した方はプロフィールからチェックを👆\nお気に入り登録もお忘れなく！",
        "週の真ん中、今日のおすすめを特集中✨\n毎日使うものこそ、コスパ重視で選びたい。\n今週紹介した商品、どれかお気に召しましたか？",
        "水曜日は比較検討の日🔍\n同じカテゴリの商品を並べて選ぶと\nコスパの違いがよく分かります。\n今週紹介した家電、ぜひ見比べてみて！",
        "水曜日の投票🔍 ネットで買う前にどうする？\n\nA: 口コミ・レビューを念入りに確認\nB: 直感と価格で即決\n\n正直なところを教えてください！",
    ],
    3: [  # Thursday — 週末に向けてのお買い物提案
        "木曜日🛒 週末のお買い物準備はできてる？\n\n週末セールに備えて\nほしいものリストを整理しておこう！\n明日・明後日がチャンスかも🔥",
        "木曜日は先取りチェックの日👀\n週末は配送が込み合うことも。\n木曜日に注文すると週末中に届くことが多いです📦",
        "今週末に向けておすすめをまとめてみました🗂️\n食品・家電・日用品…どれも生活をちょっと豊かにしてくれるもの。\n気になるものがあればぜひチェックを！",
        "木曜日の投票📦 週末セール、参加する？\n\nA: がっつり参加！カート準備済み\nB: 様子見、必要なら買う\n\n今週末の参加予定を教えてください！",
    ],
    4: [  # Friday — 週末セール情報・今週のまとめ
        "金曜日🎉 週末がきた！\n\n楽天では週末にタイムセールが増えます。\n今夜チェックしておくのがおすすめ😊\n今週紹介した商品もまだ間に合います！",
        "TGIF🙌 今週一週間ありがとうございました。\n週末はゆっくりショッピング計画でも立てませんか？\n来週もお得情報をどんどんシェアします！",
        "金曜日は今週のまとめ📝\n今週紹介した中で一番人気だったのは…\n家電カテゴリでした！引き続き良品を探していきます🔍",
        "金曜日の投票🎉 週末の夜、何してる？\n\nA: ネットショッピングでお得探し\nB: ゆっくり休む、買い物は後日\n\n金曜の夜の過ごし方を教えてください！",
    ],
    5: [  # Saturday — 週末特集・ゆっくりお買い物
        "土曜日☀️ ゆっくりお買い物日和！\n\n週末は比較検討する時間がある。\n焦らずじっくり選んで、最高の一品を見つけよう🛍️",
        "週末特集スタート🎊\n今日は特にお得な商品を厳選してご紹介。\nコーヒーでも飲みながらゆっくりチェックしてください☕",
        "土曜日はお買い物の日🛒\n今日一日、楽しいショッピングを！\nポイントUPキャンペーンが走ってることも多いのでチェックを👀",
        "土曜日の投票☀️ 週末のショッピングスタイルは？\n\nA: スマホでネット、ゆっくり比較\nB: 実店舗に行って実物を確認\n\nどっち派か教えてください！",
    ],
    6: [  # Sunday — 来週の準備・おすすめまとめ
        "日曜日🌸 今週もありがとうございました！\n\n来週に向けて必要なものを確認しよう。\n日用品の補充はまとめ買いがお得ですよ🏠",
        "週末最終日📅 来週の準備はできてる？\n消耗品や食品のストックを確認しておくと\n平日が楽になります。まとめ買いでポイントもお得！",
        "日曜日のおすすめまとめ🗓️\n今週紹介した商品を振り返り。\n気になってた商品、今週中に決断するのもあり！\n来週もたくさんの良品をご紹介します😊",
        "日曜日の投票🌸 今週一番お得だと思った買い物は？\n\nA: 食品・日用品のまとめ買い\nB: 気になってたガジェット・家電\nC: 特になし、今週は自粛\n\n教えてもらえると来週の参考にします！",
    ],
}



def post_engagement() -> dict:
    """Pick a day-of-week themed engagement post and publish it to Threads."""
    weekday = datetime.now(timezone.utc).weekday()  # 0=Mon, 6=Sun
    pool    = WEEKDAY_POSTS.get(weekday, ENGAGEMENT_POSTS)
    text    = random.choice(pool) + "\n\n#楽天市場 #買って良かった #おすすめ #暮らし"
    print(f"[Engagement] Posting:\n{text[:60]}...")

    log_entry = {
        "title":     text[:60],
        "genre":     "engagement",
        "price":     "",
        "url":       "",
        "account":   "mono_zukan_jp",
        "ab_group":  None,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "status":    None,
        "post_id":   None,
        "error":     None,
    }

    url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, params={
                "media_type":   "TEXT",
                "text":         text,
                "access_token": THREADS_ACCESS_TOKEN,
            }, timeout=15)
            resp.raise_for_status()
            creation_id = resp.json()["id"]

            resp2 = requests.post(publish_url, params={
                "creation_id":  creation_id,
                "access_token": THREADS_ACCESS_TOKEN,
            }, timeout=15)
            resp2.raise_for_status()
            post_id = resp2.json()["id"]

            log_entry["status"]  = "success"
            log_entry["post_id"] = post_id
            print(f"[Engagement] Posted | post_id={post_id} (attempt {attempt}/3)")
            send_discord(f"💬 エンゲージメント投稿完了 post_id={post_id}\n「{text[:60]}…」")
            break
        except requests.HTTPError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"[Engagement RETRY {attempt}/3] {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[Engagement RETRY {attempt}/3] {last_error}")
        if attempt < 3:
            import time
            time.sleep(5)
    else:
        log_entry["status"] = "error"
        log_entry["error"]  = last_error
        print(f"[Engagement FAIL] {last_error}")

    insert_post_log(log_entry)
    return log_entry


if __name__ == "__main__":
    result = post_engagement()
    print(f"\nStatus: {result['status']}")
    if result.get("error"):
        print(f"Error : {result['error']}")

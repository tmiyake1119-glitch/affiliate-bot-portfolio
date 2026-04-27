import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(__file__))

from analytics_agent import get_best_posting_hours, get_follower_diff
from database import get_all_post_logs, get_all_learning_data

REPORT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'weekly_report.txt')


def generate_weekly_report() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    post_log      = get_all_post_logs()
    learning_data = get_all_learning_data()

    # Filter successful posts from the past 7 days
    recent_posts = [
        p for p in post_log
        if p.get("status") == "success"
        and p.get("posted_at")
        and datetime.fromisoformat(p["posted_at"]) >= cutoff
    ]

    # Build engagement lookup by post_id
    engagement_by_id = {e["post_id"]: e for e in learning_data if e.get("post_id")}

    # Per-genre stats
    genre_posts: dict[str, list[dict]] = {}
    for p in recent_posts:
        genre_posts.setdefault(p.get("genre", "unknown"), []).append(p)

    genre_stats: list[tuple] = []
    for genre, posts in sorted(genre_posts.items()):
        scores = [
            engagement_by_id[p["post_id"]]["engagement"]
            for p in posts
            if p.get("post_id") and p["post_id"] in engagement_by_id
        ]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        genre_stats.append((genre, len(posts), avg_score))

    # Best performing post overall
    recent_ids = {p["post_id"] for p in recent_posts if p.get("post_id")}
    recent_engagement = [e for e in learning_data if e.get("post_id") in recent_ids]
    best = max(recent_engagement, key=lambda e: e["engagement"]) if recent_engagement else None

    # Build report text
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "=" * 52,
        "  Weekly Report",
        f"  Generated : {now_str}",
        f"  Period    : past 7 days",
        "=" * 52,
        "",
        f"  Total posts sent : {len(recent_posts)}",
        "",
        "  Posts & Engagement by Genre",
        "  " + "-" * 40,
    ]

    if genre_stats:
        for genre, count, avg in genre_stats:
            lines.append(f"  {genre:<14} {count} post(s)   avg score = {avg:.1f}")
    else:
        lines.append("  No posts found for this period.")

    lines += ["", "  Best Performing Post", "  " + "-" * 40]
    if best:
        lines += [
            f"  Genre    : {best.get('genre', '-')}",
            f"  Title    : {best.get('title', '-')[:60]}",
            f"  Score    : {best['engagement']}  "
            f"(likes={best.get('likes', 0)} replies={best.get('replies', 0)} reposts={best.get('reposts', 0)})",
            f"  Posted   : {best.get('posted_at', '-')[:10]}",
        ]
    else:
        lines.append("  No engagement data available yet.")

    # A/B test results
    ab_scores: dict[str, list[int]] = {}
    for e in learning_data:
        if e.get("post_id") in recent_ids:
            group = e.get("ab_group")
            if group is None:
                continue  # ab_group 未設定（digest/engagement/comparison 等）はスキップ
            ab_scores.setdefault(group, []).append(e["engagement"])

    lines += ["", "  A/B Test Results", "  " + "-" * 40]
    if len(ab_scores) >= 2:
        for group in sorted(ab_scores):
            scores = ab_scores[group]
            avg = sum(scores) / len(scores)
            lines.append(f"  Group {group}: {len(scores)} post(s)   avg score = {avg:.1f}")
        winner = max(ab_scores, key=lambda g: sum(ab_scores[g]) / len(ab_scores[g]))
        lines.append(f"  Winner: Group {winner} 🏆")
    elif len(ab_scores) == 1:
        group = next(iter(ab_scores))
        scores = ab_scores[group]
        avg = sum(scores) / len(scores)
        lines.append(f"  Group {group}: {len(scores)} post(s)   avg score = {avg:.1f}")
        lines.append("  (Waiting for both groups to accumulate data)")
    else:
        lines.append("  No A/B data available yet.")

    # Optimal posting hours
    best_hours = get_best_posting_hours(min_posts=10)
    lines += ["", "  最適投稿時間帯 (JST)", "  " + "-" * 40]
    if best_hours:
        hour_strs = [f"{h:02d}:00" for h in best_hours]
        lines.append(f"  Top 3 hours: {' / '.join(hour_strs)}")
        lines.append("  (エンゲージメントスコアの平均が高い時間帯)")
    else:
        lines.append("  データ不足 (10件以上の投稿が必要)")

    # Follower count — all platforms
    lines += ["", "  フォロワー数", "  " + "-" * 40]
    for pf_key, pf_label in [("threads", "Threads"), ("instagram", "Instagram"), ("bluesky", "Bluesky")]:
        count, diff = get_follower_diff(platform=pf_key)
        if count is not None:
            sign = "+" if diff and diff >= 0 else ""
            diff_str = f"  (先週比 {sign}{diff})" if diff is not None else ""
            lines.append(f"  {pf_label}: {count}{diff_str}")
        else:
            lines.append(f"  {pf_label}: データなし")

    lines += ["", "=" * 52]

    report = "\n".join(lines)
    return report


def save_and_print_report() -> str:
    report = generate_weekly_report()

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)

    print(report)
    print(f"\nReport saved to: {os.path.abspath(REPORT_PATH)}")
    return report


if __name__ == "__main__":
    save_and_print_report()

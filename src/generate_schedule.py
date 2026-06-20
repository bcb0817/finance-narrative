import random
import os
import sys
import base64
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Tuple


# =========================
# 設定
# =========================

REPO = "bcb0817/Automated-X-finance-posting-bot"
WORKFLOW_PATH = ".github/workflows/post.yml"

MIN_POSTS = 35
MAX_POSTS = 40
MIN_GAP_MINUTES = 12
MIN_NOON_POSTS = 3

AVOID_MINUTES = {0, 1, 2, 3, 4, 58, 59}

JST_OFFSET_HOURS = 9

WINDOWS = [
    ("early",   "早朝", 4 * 60 + 30,  6 * 60, 3),
    ("morning", "朝",   6 * 60,        9 * 60, 10),
    ("noon",    "昼",  11 * 60,       13 * 60, 7),
    ("evening", "夜",  17 * 60,       23 * 60, 20),
]

NOON_START = 11 * 60
NOON_END = 13 * 60


def minute_to_hhmm(minute: int) -> str:
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def jst_minute_to_utc_cron(minute_jst: int) -> Tuple[int, int]:
    utc_total = (minute_jst - JST_OFFSET_HOURS * 60) % (24 * 60)
    utc_h, utc_m = divmod(utc_total, 60)
    return utc_h, utc_m


def utc_cron_to_jst_minute(utc_h: int, utc_m: int) -> int:
    utc_total = utc_h * 60 + utc_m
    return (utc_total + JST_OFFSET_HOURS * 60) % (24 * 60)


def format_cron_line(minute_jst: int) -> str:
    utc_h, utc_m = jst_minute_to_utc_cron(minute_jst)
    return f"'{utc_m} {utc_h} * * *'  # JST {minute_to_hhmm(minute_jst)}"


def count_in_window(selected: List[int], start: int, end: int) -> int:
    return sum(1 for m in selected if start <= m < end)


def allocate_counts(total: int) -> List[Tuple[str, str, int, int, int]]:
    early = max(1, round(total * 3 / 40))
    morning = max(1, round(total * 10 / 40))
    noon = max(MIN_NOON_POSTS, round(total * 7 / 40))
    evening = total - early - morning - noon

    if evening < 1:
        raise RuntimeError(
            f"投稿数が少なすぎます: total={total}, evening={evening}。"
            f"MIN_POSTS を増やすか MIN_NOON_POSTS を下げてください。"
        )

    counts = {
        "early": early,
        "morning": morning,
        "noon": noon,
        "evening": evening,
    }

    return [
        (name, label, start, end, counts[name])
        for name, label, start, end, _ in WINDOWS
    ]


def has_enough_gap(candidate: int, selected: List[int], gap: int) -> bool:
    return all(abs(candidate - s) >= gap for s in selected)


def sample_from_window(
    start: int,
    end: int,
    count: int,
    already_selected: List[int],
    gap: int,
) -> List[int]:
    slots = [
        m for m in range(start, end)
        if m % 60 not in AVOID_MINUTES
    ]
    random.shuffle(slots)

    picked: List[int] = []
    for m in slots:
        if has_enough_gap(m, already_selected + picked, gap):
            picked.append(m)
        if len(picked) >= count:
            break
    return picked


def validate_schedule(selected: List[int], gap: int) -> None:
    if not selected:
        raise RuntimeError("スケジュールが空です。")

    sorted_sel = sorted(selected)

    for m in sorted_sel:
        if 0 <= m < 4 * 60 + 30:
            raise RuntimeError(f"深夜投稿: JST {minute_to_hhmm(m)}")
        if m >= 23 * 60:
            raise RuntimeError(f"23時以降: JST {minute_to_hhmm(m)}")

    noon_count = count_in_window(sorted_sel, NOON_START, NOON_END)
    if noon_count < MIN_NOON_POSTS:
        raise RuntimeError(
            f"昼帯(11:00-13:00 JST)の投稿が不足: {noon_count}回 < 最低{MIN_NOON_POSTS}回"
        )

    for i in range(len(sorted_sel) - 1):
        diff = sorted_sel[i + 1] - sorted_sel[i]
        if diff < gap:
            raise RuntimeError(
                f"間隔不足: JST {minute_to_hhmm(sorted_sel[i])} "
                f"-> {minute_to_hhmm(sorted_sel[i+1])} ({diff}分)"
            )


def verify_cron_conversion(selected_jst: List[int], cron_lines: List[str]) -> None:
    if len(selected_jst) != len(cron_lines):
        raise RuntimeError("JST時刻数と cron 行数が一致しません。")

    for minute_jst, line in zip(sorted(selected_jst), cron_lines):
        utc_h, utc_m = jst_minute_to_utc_cron(minute_jst)
        expected = f"'{utc_m} {utc_h} * * *'"
        if expected not in line:
            raise RuntimeError(
                f"UTC変換不一致: JST {minute_to_hhmm(minute_jst)} "
                f"→ 期待 {expected}, 実際 {line}"
            )

        roundtrip = utc_cron_to_jst_minute(utc_h, utc_m)
        if roundtrip != minute_jst:
            raise RuntimeError(
                f"UTC→JST 逆変換不一致: JST {minute_to_hhmm(minute_jst)} "
                f"→ UTC {utc_m:02d}:{utc_h:02d} → JST {minute_to_hhmm(roundtrip)}"
            )


def print_conversion_log(selected_jst: List[int]) -> None:
    print("=" * 60)
    print("UTC cron → JST 実行時刻 確認ログ")
    print("(GitHub Actions の schedule は常に UTC で解釈されます)")
    print("-" * 60)
    print(f"{'UTC cron':<18} {'JST実行':<10} {'時間帯'}")
    print("-" * 60)

    window_labels = {
        "early": "早朝 04:30-06:00",
        "morning": "朝 06:00-09:00",
        "noon": "昼 11:00-13:00",
        "evening": "夜 17:00-23:00",
    }

    for minute_jst in sorted(selected_jst):
        utc_h, utc_m = jst_minute_to_utc_cron(minute_jst)
        label = "範囲外"
        for name, _lbl, start, end, _ in WINDOWS:
            if start <= minute_jst < end:
                label = window_labels[name]
                break
        print(
            f"{utc_m:02d} {utc_h:02d} * * *".ljust(18)
            + f"JST {minute_to_hhmm(minute_jst)}".ljust(10)
            + label
        )

    print("-" * 60)
    for name, label, start, end, _ in WINDOWS:
        count = count_in_window(selected_jst, start, end)
        print(f"{label}: {count}回")
    print("=" * 60)


def generate_crons() -> Tuple[List[str], List[int]]:
    total = random.randint(MIN_POSTS, MAX_POSTS)
    window_counts = allocate_counts(total)

    for attempt in range(1, 1001):
        selected: List[int] = []
        ok = True

        for _name, _label, start, end, count in window_counts:
            picked = sample_from_window(start, end, count, selected, MIN_GAP_MINUTES)
            if len(picked) < count:
                ok = False
                break
            selected.extend(picked)

        if not ok:
            continue

        selected = sorted(selected)

        try:
            validate_schedule(selected, MIN_GAP_MINUTES)
        except RuntimeError:
            continue

        cron_lines = [format_cron_line(m_jst) for m_jst in selected]
        verify_cron_conversion(selected, cron_lines)

        gaps = [selected[i + 1] - selected[i] for i in range(len(selected) - 1)]

        print("=" * 48)
        print(f"本日の投稿数: {total}回")
        print(f"最低投稿間隔: {min(gaps) if gaps else 'N/A'}分")
        print(f"生成試行回数: {attempt}回")
        for name, label, start, end, count in window_counts:
            times = [minute_to_hhmm(m) for m in selected if start <= m < end]
            print(f"{label}: {len(times)}回 / 予定{count}回  {', '.join(times)}")
        print("=" * 48)

        print_conversion_log(selected)
        return cron_lines, selected

    raise RuntimeError(
        "スケジュール生成失敗。MIN_GAP_MINUTESを下げるかMAX_POSTSを下げてください。"
    )


def get_file_sha(token: str, repo: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read())["sha"]
    except urllib.error.HTTPError as e:
        print(f"SHA取得失敗: {e.code} {e.reason}")
        print(e.read().decode("utf-8", errors="replace"))
        raise


def update_file_via_api(token: str, repo: str, path: str, content: str, sha: str) -> None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    data = json.dumps({
        "message": "Daily schedule reset",
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, method="PUT")
    try:
        with urllib.request.urlopen(req) as res:
            body = json.loads(res.read())
            print("更新成功！")
            print(f"commit: {body.get('commit', {}).get('html_url', 'N/A')}")
    except urllib.error.HTTPError as e:
        print(f"API更新失敗: {e.code} {e.reason}")
        print(e.read().decode("utf-8", errors="replace"))
        raise


def build_post_yml(crons: List[str]) -> str:
    cron_lines = "\n".join([f"    - cron: {c}" for c in crons])
    return f"""name: X Finance Auto Post Bot

# schedule の cron は GitHub Actions 仕様により UTC。
# コメントの JST は実際の日本時間での実行目安。
# このファイルは毎日 0:00 JST に reset.yml が自動更新します。

on:
  schedule:
{cron_lines}
  workflow_dispatch:
    inputs:
      mode:
        description: '投稿モード（link / normal / diagram / test）'
        required: false
        default: 'test'

concurrency:
  group: x-finance-auto-post-bot
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  post:
    runs-on: ubuntu-latest
    env:
      FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Determine post mode
        id: mode
        run: |
          if [ "${{{{ github.event_name }}}}" = "workflow_dispatch" ]; then
            echo "mode=${{{{ github.event.inputs.mode }}}}" >> $GITHUB_OUTPUT
          else
            RAND=$((RANDOM % 2))
            if [ "$RAND" = "0" ]; then
              echo "mode=link" >> $GITHUB_OUTPUT
            else
              echo "mode=diagram" >> $GITHUB_OUTPUT
            fi
          fi

      - name: Post to X
        id: post
        env:
          API_KEY: ${{{{ secrets.API_KEY }}}}
          API_KEY_SECRET: ${{{{ secrets.API_KEY_SECRET }}}}
          ACCESS_TOKEN: ${{{{ secrets.ACCESS_TOKEN }}}}
          ACCESS_TOKEN_SECRET: ${{{{ secrets.ACCESS_TOKEN_SECRET }}}}
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
        run: python src/post.py ${{{{ steps.mode.outputs.mode }}}}

      - name: Commit posted history
        if: success() && steps.mode.outputs.mode != 'test'
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/posted_history.json
          git diff --staged --quiet || git commit -m "Update posted history"
          git push
"""


def write_local_post_yml(content: str) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    target = repo_root / WORKFLOW_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def main() -> None:
    local_only = "--local-only" in sys.argv

    crons, _selected = generate_crons()
    content = build_post_yml(crons)

    if local_only:
        path = write_local_post_yml(content)
        print(f"ローカル更新完了: {path}")
        print(f"{len(crons)}個の UTC cron を書き込みました。")
        return

    token = os.environ.get("GH_PAT")
    if not token:
        raise RuntimeError(
            "環境変数 GH_PAT が設定されていません。"
            "ローカルのみ更新する場合は --local-only を指定してください。"
        )

    sha = get_file_sha(token, REPO, WORKFLOW_PATH)
    update_file_via_api(token, REPO, WORKFLOW_PATH, content, sha)
    print(f"スケジュール更新完了！{len(crons)}個のcronを設定しました。")


if __name__ == "__main__":
    main()

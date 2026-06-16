import random
import os
import base64
import json
import urllib.request
import urllib.error
from typing import List, Tuple


# =========================
# 設定
# =========================

REPO = "bcb0817/Automated-X-finance-posting-bot"
WORKFLOW_PATH = ".github/workflows/post.yml"

MIN_POSTS = 35
MAX_POSTS = 40
MIN_GAP_MINUTES = 12

# 毎時00分前後と58〜59分を避ける
AVOID_MINUTES = {0, 1, 2, 3, 4, 58, 59}

JST_OFFSET_HOURS = 9

WINDOWS = [
    ("early",   "早朝", 4 * 60 + 30,  6 * 60, 3),
    ("morning", "朝",   6 * 60,        9 * 60, 10),
    ("noon",    "昼",  11 * 60,       13 * 60, 7),
    ("evening", "夜",  17 * 60,       23 * 60, 20),
]


def minute_to_hhmm(minute: int) -> str:
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def jst_minute_to_utc_cron(minute_jst: int) -> Tuple[int, int]:
    utc = (minute_jst - JST_OFFSET_HOURS * 60) % (24 * 60)
    return divmod(utc, 60)


def allocate_counts(total: int) -> List[Tuple[str, str, int, int, int]]:
    early   = round(total * 3 / 40)
    morning = round(total * 10 / 40)
    noon    = round(total * 7 / 40)
    evening = total - early - morning - noon

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

    for i in range(len(sorted_sel) - 1):
        diff = sorted_sel[i + 1] - sorted_sel[i]
        if diff < gap:
            raise RuntimeError(
                f"間隔不足: JST {minute_to_hhmm(sorted_sel[i])} "
                f"-> {minute_to_hhmm(sorted_sel[i+1])} ({diff}分)"
            )


def generate_crons() -> List[str]:
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

        crons: List[str] = []
        for m_jst in selected:
            utc_h, utc_m = jst_minute_to_utc_cron(m_jst)
            crons.append(f"'{utc_m} {utc_h} * * *'  # JST {minute_to_hhmm(m_jst)}")

        gaps = [selected[i+1] - selected[i] for i in range(len(selected) - 1)]

        print("=" * 48)
        print(f"本日の投稿数: {total}回")
        print(f"最低投稿間隔: {min(gaps) if gaps else 'N/A'}分")
        print(f"生成試行回数: {attempt}回")
        for name, label, start, end, count in window_counts:
            times = [minute_to_hhmm(m) for m in selected if start <= m < end]
            print(f"{label}: {len(times)}回 / 予定{count}回  {', '.join(times)}")
        print("=" * 48)

        return crons

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
            HOUR=$(TZ=Asia/Tokyo date +%H)
            if [ "$HOUR" = "04" ] || [ "$HOUR" = "06" ] || [ "$HOUR" = "11" ] || [ "$HOUR" = "17" ]; then
              echo "mode=link" >> $GITHUB_OUTPUT
            else
              RAND=$((RANDOM % 2))
              if [ "$RAND" = "0" ]; then
                echo "mode=normal" >> $GITHUB_OUTPUT
              else
                echo "mode=diagram" >> $GITHUB_OUTPUT
              fi
            fi
          fi

      - name: Post to X
        env:
          API_KEY: ${{{{ secrets.API_KEY }}}}
          API_KEY_SECRET: ${{{{ secrets.API_KEY_SECRET }}}}
          ACCESS_TOKEN: ${{{{ secrets.ACCESS_TOKEN }}}}
          ACCESS_TOKEN_SECRET: ${{{{ secrets.ACCESS_TOKEN_SECRET }}}}
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
        run: |
          cd src
          python post.py ${{{{ steps.mode.outputs.mode }}}}
"""


def main() -> None:
    token = os.environ.get("GH_PAT")
    if not token:
        raise RuntimeError("環境変数 GH_PAT が設定されていません。")

    crons = generate_crons()
    content = build_post_yml(crons)
    sha = get_file_sha(token, REPO, WORKFLOW_PATH)
    update_file_via_api(token, REPO, WORKFLOW_PATH, content, sha)
    print(f"スケジュール更新完了！{len(crons)}個のcronを設定しました。")


if __name__ == "__main__":
    main()

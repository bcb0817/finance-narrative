import random
import os
import base64
import json
import urllib.request
import urllib.error
from typing import List

def generate_crons() -> List[str]:
    crons = []

    # 1日の総投稿数をランダムに決定（40〜50回）
    total = random.randint(40, 50)

    # 時間帯ごとの配分
    early_count = round(total * 3 / 40)
    morning_count = round(total * 10 / 40)
    noon_count = round(total * 7 / 40)
    evening_count = total - early_count - morning_count - noon_count

    def sample_with_gap(slots: List[int], count: int, gap: int = 5) -> List[int]:
        """最低gap分間隔でランダムに選ぶ"""
        selected = []
        available = slots.copy()
        random.shuffle(available)
        for m in available:
            if all(abs(m - s) >= gap for s in selected):
                selected.append(m)
            if len(selected) >= count:
                break
        return sorted(selected)

    # 早朝 JST 4:30〜6:00 = UTC 19:30〜21:00
    early_slots = list(range(19 * 60 + 30, 21 * 60))
    early = sample_with_gap(early_slots, min(early_count, len(early_slots)))
    for m in early:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")

    # 朝 JST 6:00〜9:00 = UTC 21:00〜0:00
    morning_slots = list(range(21 * 60, 24 * 60))
    morning = sample_with_gap(morning_slots, min(morning_count, len(morning_slots)))
    for m in morning:
        h, mn = divmod(m % (24 * 60), 60)
        crons.append(f"'{mn} {h % 24} * * *'")

    # 昼 JST 11:00〜13:00 = UTC 2:00〜4:00
    noon_slots = list(range(2 * 60, 4 * 60))
    noon = sample_with_gap(noon_slots, min(noon_count, len(noon_slots)))
    for m in noon:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")

    # 夜 JST 17:00〜23:00 = UTC 8:00〜14:00
    evening_slots = list(range(8 * 60, 14 * 60))
    evening = sample_with_gap(evening_slots, min(evening_count, len(evening_slots)))
    for m in evening:
        h, mn = divmod(m, 60)
        crons.append(f"'{mn} {h} * * *'")

    print(f"本日の投稿数: {total}回")
    return crons

def get_file_sha(token: str, repo: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    })
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())["sha"]

def update_file_via_api(token: str, repo: str, path: str, content: str, sha: str) -> None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    data = json.dumps({
        "message": "Daily schedule reset",
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }, method="PUT")
    try:
        with urllib.request.urlopen(req) as res:
            print("更新成功！")
    except urllib.error.HTTPError as e:
        print(f"API更新失敗: {e.code} {e.reason}")
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
            HOUR=$(date -u +%H)
            if [ "$HOUR" = "19" ] || [ "$HOUR" = "21" ] || [ "$HOUR" = "02" ] || [ "$HOUR" = "08" ]; then
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

if __name__ == "__main__":
    token = os.environ["GH_PAT"]
    repo = "bcb0817/Automated-X-finance-posting-bot"
    path = ".github/workflows/post.yml"

    crons = generate_crons()
    content = build_post_yml(crons)
    sha = get_file_sha(token, repo, path)
    update_file_via_api(token, repo, path, content, sha)
    print(f"スケジュール更新完了！{len(crons)}個のcronを設定しました")

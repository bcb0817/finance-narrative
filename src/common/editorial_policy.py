"""Editorial-v2 switch. Collection schedules and API budgets are not changed."""
import os


def enabled():
    return os.getenv('EDITORIAL_POLICY_ENABLED', 'true').lower() in ('true', '1', 'yes')


RULES = '''一投稿一論点。根拠の具体的事実から自然に書き始める。
結論→理由→注目点の固定構成、ランダムな書き出し、文字数の下限は禁止。
短い事実速報に解釈を足さない。出典名を本文末尾に記載しURLは本文に出さない。
根拠のない因果、市場は好感、織り込み済み、価格予測、売買推奨は不可。
不確かな解釈は省略。一般論だけの締め、次の確認、今後に注目、不要な見出し・絵文字・問いかけ・ハッシュタグを省く。
架空の体験や一人称は使わない。数字・単位・対象期間・条件・確実性を変えない。
Xの重み付き280以内（日本語は概ね2）。長さを揃えず意味の完結を優先する。'''

# When recovery is enabled, the stricter distribution_quality.RULES take priority.

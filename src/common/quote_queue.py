"""他アカウント投稿の手動引用候補。Xへの書き込み機能は意図的に持たない。"""
from __future__ import annotations
import hashlib,json,os
from datetime import datetime,timedelta
from pathlib import Path
try: from runtime import JST
except ImportError: from common.runtime import JST

STATUSES={"pending","approved","rejected","expired","manually_posted"}

def _queue_dir(create=False) -> Path:
    root=Path(os.getenv("OUTPUT_DIR","outputs"))
    if not root.is_absolute(): root=Path(__file__).resolve().parents[2]/root
    path=root/"quote_queue"
    if create: path.mkdir(parents=True,exist_ok=True)
    return path
def queue_path() -> Path: return _queue_dir()/"pending.jsonl"
def _drafts(topic: dict,post: dict, *, generate_ai: bool=True) -> list[dict]:
    base=str(topic.get("topic","")).strip(); misconception=str(topic.get("possible_misconception","")).strip()
    fallback=[{"type":"why_it_matters","text":f"重要なのは見出しそのものより、{base}が市場の前提をどう変えるかです。一次情報と次の確認材料を分けて見たい。"},
            {"type":"second_order_effect","text":f"直接の反応だけでなく、{base}が関連企業の需要・コスト・投資判断へ波及するかが次の焦点です。"},
            {"type":"contrarian_or_missing_point","text":misconception or f"議論で抜けやすいのは、{base}の事実確認と市場解釈を分ける視点です。"}]
    if not generate_ai or os.getenv("QUOTE_DRAFT_AI_ENABLED","true").lower() not in ("1","true","yes"): return fallback
    try:
        try:
            from openai_config import OpenAIRole
            from openai_service import OpenAIService
        except ImportError:
            from common.openai_config import OpenAIRole
            from common.openai_service import OpenAIService
        item={"type":"object","additionalProperties":False,"properties":{"type":{"type":"string","enum":["why_it_matters","second_order_effect","contrarian_or_missing_point"]},"text":{"type":"string"}},"required":["type","text"]}
        schema={"type":"object","additionalProperties":False,"properties":{"drafts":{"type":"array","minItems":3,"maxItems":3,"items":item}},"required":["drafts"]}
        prompt="Xアプリで人間が手動引用するコメント案を3種類作成。自動投稿しない。元情報にない数字・期日・顧客名を作らない。\n"+json.dumps({"topic":topic,"source":post},ensure_ascii=False)
        data=OpenAIService().structured(prompt,schema,role=OpenAIRole.GENERATE,operation="quote_comment_drafts")
        return data.get("drafts",fallback)
    except Exception:
        return fallback

def enqueue_from_topics(
    topics: list[dict],now: datetime|None=None, *,
    generate_ai_drafts: bool=True,
) -> list[dict]:
    if os.getenv("QUOTE_QUEUE_ENABLED","true").lower() not in ("1","true","yes"): return []
    now=now or datetime.now(JST); created=[]; path=_queue_dir(create=True)/"pending.jsonl"; existing=path.read_text(encoding="utf-8") if path.exists() else ""
    existing_rows=[]
    for line in existing.splitlines():
        try: existing_rows.append(json.loads(line))
        except json.JSONDecodeError: pass
    maximum=int(os.getenv("QUOTE_QUEUE_MAX_PER_DAY","3")); today_count=sum(str(r.get("created_at","")).startswith(now.date().isoformat()) for r in existing_rows)
    for topic in topics:
        if today_count+len(created)>=maximum: break
        for post in topic.get("representative_posts",[])[:3]:
            if not post.get("post_id") or str(post.get("post_id")) in existing: continue
            cid=hashlib.sha256(f"{post.get('post_id')}:{topic.get('topic')}".encode()).hexdigest()[:16]
            row={"candidate_id":cid,"source_post_id":str(post.get("post_id")),"source_post_url":str(post.get("url", "")),
                 "source_username":str(post.get("username","")),"account_category":"unclassified","source_excerpt":str(post.get("excerpt",""))[:280],
                 "topic":topic.get("topic",""),"detected_topic":topic.get("topic",""),"tickers":topic.get("tickers",[]),"why_relevant":topic.get("consensus_view","") or topic.get("dissenting_view",""),
                 "source_reliability":topic.get("source_reliability","unknown"),"primary_source":bool(topic.get("primary_source_available",False)),
                 "risk_flags":["x_information_unverified"] if topic.get("news_confirmation_status")!="confirmed" else [],
                 "comment_drafts":_drafts(topic,post,generate_ai=generate_ai_drafts),"created_at":now.isoformat(),"expires_at":(now+timedelta(hours=24)).isoformat(),"status":"pending"}
            created.append(row)
    if created:
        with path.open("a",encoding="utf-8",newline="\n") as fh:
            for row in created: fh.write(json.dumps(row,ensure_ascii=False)+"\n")
        md=_queue_dir(create=True)/f"{now:%Y-%m-%d}.md"
        lines=[f"# 手動引用投稿候補 {now:%Y-%m-%d}","","自動投稿されません。Xアプリで内容と原典を確認してください。",""]
        for row in created:
            lines += [f"## {row['detected_topic']}",f"- URL: {row['source_post_url']}",f"- 投稿者: @{row['source_username']}",f"- 信頼度: {row['source_reliability']}","- コメント案:"]+[f"  - {d['type']}: {d['text']}" for d in row["comment_drafts"]]+[""]
        md.write_text("\n".join(lines),encoding="utf-8")
    return created

def list_queue(*,today=False,pending=False,now:datetime|None=None) -> list[dict]:
    now=now or datetime.now(JST); rows=[]
    if not queue_path().exists(): return rows
    for line in queue_path().read_text(encoding="utf-8").splitlines():
        try: row=json.loads(line)
        except json.JSONDecodeError: continue
        if today and not str(row.get("created_at","")).startswith(now.date().isoformat()): continue
        if pending and row.get("status")!="pending": continue
        rows.append(row)
    return rows

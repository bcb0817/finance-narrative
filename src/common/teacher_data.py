"""Human-curated reference posts stored as reusable patterns, not copy text."""
from __future__ import annotations
import json
from datetime import datetime
from common.runtime import JST, state_dir

def path(): return state_dir()/"teacher_examples.jsonl"

def load_examples(limit:int=50) -> list[dict]:
    rows=[]
    try:
        for line in path().read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
                if isinstance(row,dict) and row.get("status","active")=="active": rows.append(row)
            except json.JSONDecodeError: pass
    except OSError: pass
    return rows[-max(1,limit):]

def add_example(example:dict) -> dict:
    url=str(example.get("url") or "").strip()
    if not url.startswith("https://x.com/"): raise ValueError("teacher URL must be an https://x.com/ post")
    if any(row.get("url")==url for row in load_examples(1000)): return {"status":"duplicate","url":url}
    row={**example,"url":url,"registered_at":datetime.now(JST).isoformat(),"status":"active"}
    target=path(); target.parent.mkdir(parents=True,exist_ok=True)
    with target.open("a",encoding="utf-8",newline="\n") as handle: handle.write(json.dumps(row,ensure_ascii=False)+"\n")
    return {"status":"added","url":url}

def prompt_context(topic:str="",limit:int=3) -> str:
    rows=load_examples(); needle=topic.lower()
    rows=sorted(rows,key=lambda row:int(bool(needle and needle in str(row.get("topic","")).lower())),reverse=True)[:limit]
    patterns=[{key:row.get(key) for key in ("pattern","hook_pattern","structure_pattern","visual_pattern","diagram_recommendation","reuse_rules")} for row in rows]
    if not patterns: return ""
    return "\n人力選定した参考投稿の再利用可能な型:\n"+json.dumps(patterns,ensure_ascii=False)+"\n原文・固有表現・画像はコピーせず構成原則だけ利用する。"

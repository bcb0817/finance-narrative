"""定番シリーズ／スレッドの草案のみを作る。X投稿機能は持たない。"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
try: from runtime import JST,output_dir
except ImportError: from common.runtime import JST,output_dir

def load_series() -> list[dict]:
    path=Path(__file__).resolve().parents[2]/"config"/"content_series.json"
    return json.loads(path.read_text(encoding="utf-8")).get("series",[])

def create_draft(series_id:str,sources:list[dict],generator=None) -> Path:
    series=next((s for s in load_series() if s.get("series_id")==series_id),None)
    if not series: raise ValueError(f"unknown series: {series_id}")
    evidence=[{"title":s.get("title",""),"text":s.get("text",""),"source":s.get("source","")} for s in sources[:10]]
    if generator:
        body=generator(series,evidence)
    else:
        body="\n\n".join(f"- {s['title']}: {s['text'][:180]}" for s in evidence) or "データ不足"
    out=output_dir("series_drafts")/f"{series_id}_{datetime.now(JST):%Y%m%d_%H%M}.md"
    out.write_text(f"# {series['title']}（草案）\n\n自動投稿されません。\n\n{body}\n",encoding="utf-8")
    return out

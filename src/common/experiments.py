"""投稿メタデータをvariant単位で集計する軽量実験管理。"""
from __future__ import annotations
from collections import defaultdict
import json, os
import statistics
from pathlib import Path

def load_experiments(path:Path|None=None) -> list[dict]:
    path=path or Path(__file__).resolve().parents[2]/"config"/"experiments.json"
    try: rows=json.loads(path.read_text(encoding="utf-8")).get("experiments",[])
    except (OSError,json.JSONDecodeError): return []
    maximum=int(os.getenv("MAX_ACTIVE_EXPERIMENTS","3"))
    active=[r for r in rows if r.get("status")=="active"]
    return active[:maximum]

def variant_summary(posts:list[dict],metrics:list[dict]) -> list[dict]:
    latest={}
    for row in metrics:
        if row.get("stage")=="24h": latest[str(row.get("tweet_id"))]=row
    groups=defaultdict(list)
    for post in posts:
        variant=post.get("experiment_variant") or post.get("post_type") or "unassigned"
        metric=latest.get(str(post.get("tweet_id")))
        if metric: groups[variant].append(metric)
    result=[]
    for variant,rows in groups.items():
        growth=[float(r["growth_score"]) for r in rows if r.get("growth_score") is not None]
        values=[float(r["impressions_per_hour"]) for r in rows if r.get("impressions_per_hour") is not None]
        minimum=int(os.getenv("MIN_EXPERIMENT_SAMPLE_PER_VARIANT","5"))
        sample=len(rows)
        def robust(items):
            if not items: return (None,None,None,0)
            ordered=sorted(items); trim=max(1,int(len(ordered)*.1)) if len(ordered)>=10 else 0
            trimmed=ordered[trim:-trim] if trim else ordered
            q1,q3=(statistics.quantiles(ordered,n=4)[0],statistics.quantiles(ordered,n=4)[2]) if len(ordered)>=4 else (min(ordered),max(ordered))
            iqr=q3-q1; outliers=sum(v<q1-1.5*iqr or v>q3+1.5*iqr for v in ordered)
            return (sum(items)/len(items),statistics.median(items),sum(trimmed)/len(trimmed),outliers)
        gmean,gmedian,gtrim,gout=robust(growth); imean,imedian,itrim,iout=robust(values)
        confidence="insufficient" if sample<10 else ("early" if sample<20 else ("directional" if sample<50 else "reliable"))
        result.append({"variant":variant,"sample_size":sample,
                       "mean_growth_score":gmean,"median_growth_score":gmedian,"trimmed_mean_growth_score":gtrim,
                       "mean_impressions_per_hour":imean,"median_impressions_per_hour":imedian,
                       "trimmed_mean_impressions_per_hour":itrim,"outlier_count":max(gout,iout),
                       "confidence":confidence,"minimum_sample":minimum})
    return sorted(result,key=lambda r:(r["mean_growth_score"] if r["mean_growth_score"] is not None else -1,
                                      r["mean_impressions_per_hour"] if r["mean_impressions_per_hour"] is not None else -1),reverse=True)

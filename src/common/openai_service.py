"""中央OpenAIサービス: Responses、Structured Outputs、利用量、Embedding、Moderation。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

try:
    from runtime import JST, state_dir
    from api_costs import ensure_openai_budget, record_openai_usage
    from openai_config import OpenAIRole, env_bool, model_for, role_for_model, validate_models
except ImportError:  # pragma: no cover
    from common.runtime import JST, state_dir
    from common.api_costs import ensure_openai_budget, record_openai_usage
    from common.openai_config import OpenAIRole, env_bool, model_for, role_for_model, validate_models

logger = logging.getLogger(__name__)


class OpenAIConfigurationError(RuntimeError): pass
class OpenAIDisabledError(RuntimeError): pass
class DailyLimitError(RuntimeError): pass


def _openai_dir(*, create: bool = False) -> Path:
    path = state_dir() / "openai"
    if create: path.mkdir(parents=True, exist_ok=True)
    return path


def usage_path() -> Path: return _openai_dir() / "api_usage.jsonl"
def embeddings_path() -> Path: return _openai_dir() / "post_embeddings.jsonl"


def _usage_value(usage: Any, name: str) -> int:
    if usage is None: return 0
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict): value = usage.get(name)
    return int(value or 0)


def _record_usage(*, role: OpenAIRole, model: str, endpoint: str, started: float,
                  success: bool, fallback: bool, operation: str, usage=None, error="") -> None:
    details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    row = {"timestamp": datetime.now(JST).isoformat(), "role": role.value, "model": model,
           "endpoint": endpoint, "input_tokens": _usage_value(usage, "input_tokens"),
           "cached_input_tokens": _usage_value(details, "cached_tokens"),
           "output_tokens": _usage_value(usage, "output_tokens"),
           "reasoning_tokens": _usage_value(output_details, "reasoning_tokens"),
           "latency_ms": round((time.perf_counter()-started)*1000), "success": success,
           "fallback_used": fallback, "error_type": error, "operation_name": operation}
    _openai_dir(create=True)
    with usage_path().open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def today_counts() -> dict[str, int]:
    today = datetime.now(JST).date().isoformat(); counts: dict[str, int] = {}
    if usage_path().exists():
        for line in usage_path().read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
                if str(row.get("timestamp", "")).startswith(today):
                    key=str(row.get("role", "unknown")); counts[key]=counts.get(key,0)+1
            except json.JSONDecodeError: pass
    return counts


def _daily_limit(role: OpenAIRole) -> int | None:
    if role == OpenAIRole.ANALYZE: return int(os.getenv("OPENAI_TERRA_MAX_CALLS_PER_DAY", "3"))
    if role == OpenAIRole.DEEP_ANALYZE: return int(os.getenv("OPENAI_SOL_MAX_CALLS_PER_DAY", "1"))
    if role == OpenAIRole.IMAGE: return int(os.getenv("OPENAI_IMAGE_MAX_PER_DAY", "0"))
    return None


def _check_role(role: OpenAIRole) -> None:
    errors=validate_models()
    if errors: raise OpenAIConfigurationError("; ".join(errors))
    enabled = {OpenAIRole.DEEP_ANALYZE: env_bool("OPENAI_DEEP_ANALYSIS_ENABLED", False),
               OpenAIRole.IMAGE: env_bool("OPENAI_IMAGE_ENABLED", False),
               OpenAIRole.EMBED: env_bool("OPENAI_EMBEDDING_ENABLED", True),
               OpenAIRole.MODERATE: env_bool("OPENAI_MODERATION_ENABLED", True)}
    if role in enabled and not enabled[role]: raise OpenAIDisabledError(f"{role.value} is disabled")
    limit=_daily_limit(role)
    if limit is not None and today_counts().get(role.value,0) >= limit: raise DailyLimitError(f"{role.value} daily limit reached")


def _transient(exc: Exception) -> bool:
    code=getattr(exc,"status_code",None)
    return code == 429 or (isinstance(code,int) and 500 <= code < 600) or isinstance(exc, TimeoutError) or "model_not_found" in str(exc).lower()


def _reasoning_effort(value: str | None) -> str | None:
    """Translate legacy effort values to values accepted by current Responses models."""
    if value == "minimal":
        return "low"
    return value


class OpenAIService:
    def __init__(self, client=None):
        if not os.getenv("OPENAI_API_KEY") and client is None: raise OpenAIConfigurationError("OPENAI_API_KEY is not configured")
        self.client = client or OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def text(self, prompt: str, *, role=OpenAIRole.GENERATE, max_tokens=800,
             schema: dict | None=None, operation="text", reasoning: str | None=None):
        _check_role(role); ensure_openai_budget(); model=model_for(role); started=time.perf_counter()
        kwargs={"model":model,"input":prompt,"max_output_tokens":max_tokens}
        if effort := _reasoning_effort(reasoning): kwargs["reasoning"]={"effort":effort}
        if schema: kwargs["text"]={"format":{"type":"json_schema","name":"result","strict":True,"schema":schema}}
        try:
            response=self.client.responses.create(**kwargs)
            record_openai_usage(response, model)
            _record_usage(role=role,model=model,endpoint="responses",started=started,success=True,fallback=False,operation=operation,usage=getattr(response,"usage",None))
            raw=getattr(response,"output_text","") or ""
            return json.loads(raw) if schema else raw
        except Exception as exc:
            _record_usage(role=role,model=model,endpoint="responses",started=started,success=False,fallback=False,operation=operation,error=type(exc).__name__)
            if role in (OpenAIRole.GENERATE,OpenAIRole.ANALYZE) and env_bool("OPENAI_FALLBACK_ENABLED",True) and _transient(exc):
                return self.text(prompt,role=OpenAIRole.FALLBACK,max_tokens=max_tokens,schema=schema,operation=operation,reasoning="minimal")
            raise

    def structured(self, prompt: str, schema: dict, *, role=OpenAIRole.REVIEW, operation="structured") -> dict:
        for attempt in range(2):
            try: return self.text(prompt,role=role,max_tokens=1600,schema=schema,operation=operation,reasoning="minimal" if role != OpenAIRole.ANALYZE else "medium")
            except (json.JSONDecodeError, ValueError):
                if attempt: raise
        raise ValueError("structured output failed")

    def embed(self, text: str, *, operation="embedding") -> list[float]:
        role=OpenAIRole.EMBED; _check_role(role); ensure_openai_budget(); model=model_for(role); started=time.perf_counter()
        try:
            response=self.client.embeddings.create(model=model,input=text)
            _record_usage(role=role,model=model,endpoint="embeddings",started=started,success=True,fallback=False,operation=operation,usage=getattr(response,"usage",None))
            return list(response.data[0].embedding)
        except Exception as exc:
            _record_usage(role=role,model=model,endpoint="embeddings",started=started,success=False,fallback=False,operation=operation,error=type(exc).__name__); raise

    def moderate(self, text: str) -> bool:
        role=OpenAIRole.MODERATE; _check_role(role); model=model_for(role); started=time.perf_counter()
        try:
            response=self.client.moderations.create(model=model,input=text)
            _record_usage(role=role,model=model,endpoint="moderations",started=started,success=True,fallback=False,operation="pre_post_moderation")
            return not bool(response.results[0].flagged)
        except Exception as exc:
            _record_usage(role=role,model=model,endpoint="moderations",started=started,success=False,fallback=False,operation="pre_post_moderation",error=type(exc).__name__)
            if env_bool("OPENAI_MODERATION_FAIL_CLOSED",True): return False
            return True

    def image_draft(self, prompt: str, *, operation="manual_image_draft"):
        """人間確認用の画像草案だけを生成する。初期設定では必ず無効。"""
        role=OpenAIRole.IMAGE; _check_role(role); model=model_for(role); started=time.perf_counter()
        try:
            response=self.client.images.generate(model=model,prompt=prompt)
            _record_usage(role=role,model=model,endpoint="images",started=started,success=True,fallback=False,operation=operation)
            return response
        except Exception as exc:
            _record_usage(role=role,model=model,endpoint="images",started=started,success=False,fallback=False,operation=operation,error=type(exc).__name__); raise


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or len(a)!=len(b): return 0.0
    denom=math.sqrt(sum(x*x for x in a))*math.sqrt(sum(y*y for y in b))
    return sum(x*y for x,y in zip(a,b))/denom if denom else 0.0


def semantic_duplicate(text: str, *, title="", source_url="", service: OpenAIService | None=None) -> dict:
    service=service or OpenAIService(); content=f"{title}\n{text}".strip(); digest=hashlib.sha256(content.encode("utf-8")).hexdigest()
    rows=[]
    if embeddings_path().exists():
        for line in embeddings_path().read_text(encoding="utf-8").splitlines():
            try: rows.append(json.loads(line))
            except json.JSONDecodeError: pass
    existing=next((r for r in rows if r.get("content_hash")==digest),None)
    vector=existing.get("embedding") if existing else service.embed(content)
    cutoff=datetime.now(JST)-timedelta(days=int(os.getenv("OPENAI_EMBEDDING_LOOKBACK_DAYS","30")))
    best=max((cosine_similarity(vector,r.get("embedding",[])) for r in rows if r.get("content_hash")!=digest and str(r.get("created_at",""))>=cutoff.isoformat()),default=0.0)
    block=float(os.getenv("OPENAI_DUPLICATE_SIMILARITY_BLOCK","0.92")); warn=float(os.getenv("OPENAI_DUPLICATE_SIMILARITY_WARN","0.84"))
    if not existing:
        row={"tweet_id":"","source_url":source_url,"title":title,"post_text":text,"created_at":datetime.now(JST).isoformat(),"model":model_for(OpenAIRole.EMBED),"embedding":vector,"content_hash":digest}
        _openai_dir(create=True)
        with embeddings_path().open("a",encoding="utf-8",newline="\n") as fh: fh.write(json.dumps(row,ensure_ascii=False)+"\n")
    return {"status":"block" if best>=block else ("warn" if best>=warn else "ok"),"similarity":best,"content_hash":digest,"reused":existing is not None}


def configuration_status() -> dict:
    errors=validate_models(); counts=today_counts()
    return {"valid":not errors,"errors":errors,"api_key_configured":bool(os.getenv("OPENAI_API_KEY")),
            "responses_api":env_bool("OPENAI_USE_RESPONSES_API",True),"counts":counts,
            "roles":{r.value:{"model":model_for(r),"enabled": not (r==OpenAIRole.DEEP_ANALYZE and not env_bool("OPENAI_DEEP_ANALYSIS_ENABLED",False)) and not (r==OpenAIRole.IMAGE and not env_bool("OPENAI_IMAGE_ENABLED",False))} for r in OpenAIRole}}


class _CompletionsProxy:
    def __init__(self, service): self.service=service
    def create(self, *args, **kwargs):
        model=str(kwargs.get("model")); requested=kwargs.pop("openai_role",None)
        role=OpenAIRole(requested) if requested else role_for_model(model); messages=kwargs.get("messages",[])
        prompt="\n".join(str(m.get("content","")) for m in messages)
        result=self.service.text(prompt,role=role,max_tokens=int(kwargs.get("max_completion_tokens",800)),schema=None,operation="legacy_adapter",reasoning=kwargs.get("reasoning_effort"))
        content=json.dumps(result,ensure_ascii=False) if isinstance(result,dict) else result
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],usage=None)


class CompatibleClient:
    def __init__(self, service=None):
        self.service=service or OpenAIService(); self.chat=SimpleNamespace(completions=_CompletionsProxy(self.service))
    def __getattr__(self,name): return getattr(self.service.client,name)

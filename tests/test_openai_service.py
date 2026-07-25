import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src")); sys.path.insert(0,str(ROOT/"src"/"common"))
from openai_config import ALLOWED_MODELS, OpenAIRole, model_for, validate_models
from openai_service import (DailyLimitError, OpenAIDisabledError, OpenAIService,
                            cosine_similarity, semantic_duplicate, usage_path)


class FakeResponses:
    def __init__(self, outputs=None, error=None): self.outputs=list(outputs or ["ok"]); self.error=error; self.calls=[]
    def create(self, **kw):
        self.calls.append(kw)
        if self.error: err=self.error; self.error=None; raise err
        return SimpleNamespace(output_text=self.outputs.pop(0),usage=SimpleNamespace(input_tokens=2,output_tokens=1))
class FakeEmbeddings:
    def __init__(self, vector=(1.0,0.0)): self.vector=vector; self.calls=0
    def create(self, **kw): self.calls+=1; return SimpleNamespace(data=[SimpleNamespace(embedding=list(self.vector))],usage=None)
class FakeModerations:
    def __init__(self, flagged=False, error=None): self.flagged=flagged; self.error=error
    def create(self, **kw):
        if self.error: raise self.error
        return SimpleNamespace(results=[SimpleNamespace(flagged=self.flagged)])
class FakeClient:
    def __init__(self, responses=None, embeddings=None, moderations=None):
        self.responses=responses or FakeResponses(); self.embeddings=embeddings or FakeEmbeddings(); self.moderations=moderations or FakeModerations()


class OpenAIServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.env=patch.dict(os.environ,{"STATE_DIR":self.tmp.name,"OPENAI_API_KEY":"test-secret","OPENAI_GENERATE_MODEL":"gpt-5.4-mini","OPENAI_REVIEW_MODEL":"gpt-5-nano","OPENAI_CLASSIFICATION_MODEL":"gpt-5-nano","OPENAI_ANALYSIS_MODEL":"gpt-5.6-terra","OPENAI_DEEP_ANALYSIS_MODEL":"gpt-5.6-sol","OPENAI_EMBEDDING_MODEL":"text-embedding-3-small","OPENAI_MODERATION_MODEL":"omni-moderation-latest","OPENAI_IMAGE_MODEL":"gpt-image-2","OPENAI_FALLBACK_MODEL":"gpt-5-mini","OPENAI_EMBEDDING_ENABLED":"true","OPENAI_MODERATION_ENABLED":"true","OPENAI_DEEP_ANALYSIS_ENABLED":"false","OPENAI_IMAGE_ENABLED":"false"},clear=False); self.env.start()
    def tearDown(self): self.env.stop(); self.tmp.cleanup()

    def test_role_models_are_allowed_and_exact(self):
        self.assertFalse(validate_models())
        self.assertEqual(model_for(OpenAIRole.GENERATE),"gpt-5.4-mini")
        self.assertEqual(model_for(OpenAIRole.ANALYZE),"gpt-5.6-terra")
        self.assertTrue(all(model_for(r) in ALLOWED_MODELS for r in OpenAIRole))
    def test_rejects_ambiguous_and_luna_models(self):
        for bad in ("gpt-5.6","gpt-5.6-luna","other"):
            with patch.dict(os.environ,{"OPENAI_GENERATE_MODEL":bad}): self.assertTrue(validate_models())
    def test_generation_uses_generate_role(self):
        fake=FakeClient(); out=OpenAIService(fake).text("日本語",operation="post_generation")
        self.assertEqual(out,"ok"); self.assertEqual(fake.responses.calls[0]["model"],"gpt-5.4-mini")
    def test_structured_review_uses_schema_and_nano(self):
        schema={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False}
        fake=FakeClient(responses=FakeResponses(['{"ok":true}']))
        self.assertTrue(OpenAIService(fake).structured("x",schema)["ok"])
        self.assertEqual(fake.responses.calls[0]["model"],"gpt-5-nano"); self.assertTrue(fake.responses.calls[0]["text"]["format"]["strict"])
    def test_analysis_uses_terra(self):
        fake=FakeClient(); OpenAIService(fake).text("metrics",role=OpenAIRole.ANALYZE)
        self.assertEqual(fake.responses.calls[0]["model"],"gpt-5.6-terra")
    def test_deep_and_image_disabled_without_api_call(self):
        fake=FakeClient(); service=OpenAIService(fake)
        with self.assertRaises(OpenAIDisabledError): service.text("x",role=OpenAIRole.DEEP_ANALYZE)
        with self.assertRaises(OpenAIDisabledError): service.text("x",role=OpenAIRole.IMAGE)
        self.assertFalse(fake.responses.calls)
    def test_moderation_ng_and_failure_closed(self):
        self.assertFalse(OpenAIService(FakeClient(moderations=FakeModerations(True))).moderate("x"))
        self.assertFalse(OpenAIService(FakeClient(moderations=FakeModerations(error=RuntimeError("offline")))).moderate("x"))
    def test_embedding_save_reuse_and_similarity_thresholds(self):
        emb=FakeEmbeddings((1.0,0.0)); service=OpenAIService(FakeClient(embeddings=emb))
        first=semantic_duplicate("same",title="題",service=service); second=semantic_duplicate("same",title="題",service=service)
        self.assertEqual(emb.calls,1); self.assertTrue(second["reused"]); self.assertAlmostEqual(cosine_similarity([1,0],[1,0]),1)
        semantic_duplicate("near",title="別",service=service)
        self.assertEqual(semantic_duplicate("third",title="三",service=service)["status"],"block")
    def test_warn_threshold(self):
        emb=FakeEmbeddings((1.0,0.0)); service=OpenAIService(FakeClient(embeddings=emb)); semantic_duplicate("a",service=service)
        service.client.embeddings.vector=(0.86,(1-0.86**2)**0.5)
        self.assertEqual(semantic_duplicate("b",service=service)["status"],"warn")
    def test_transient_falls_back_but_auth_does_not(self):
        transient=RuntimeError("server"); transient.status_code=500
        fake=FakeClient(responses=FakeResponses(["fallback"],error=transient))
        self.assertEqual(OpenAIService(fake).text("x"),"fallback"); self.assertEqual(fake.responses.calls[-1]["model"],"gpt-5-mini")
        auth=RuntimeError("auth"); auth.status_code=401
        with self.assertRaises(RuntimeError): OpenAIService(FakeClient(responses=FakeResponses(error=auth))).text("x")
    def test_usage_contains_no_api_key(self):
        OpenAIService(FakeClient()).text("secret prompt")
        raw=usage_path().read_text(encoding="utf-8")
        self.assertNotIn("test-secret",raw); self.assertNotIn("secret prompt",raw)
        self.assertEqual(json.loads(raw.splitlines()[0])["input_tokens"],2)

if __name__=="__main__": unittest.main()

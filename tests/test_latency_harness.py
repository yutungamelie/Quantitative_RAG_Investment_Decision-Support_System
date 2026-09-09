"""Local checks of measurement plumbing, not Gemini or application benchmarks."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('latency_lab',Path(__file__).parents[1]/'stock_ai_latency_lab.py')
lab=importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class Frame:
    def __init__(self): self.values=[100,101]
    def copy(self,deep=True): return copy.deepcopy(self)
    def to_json(self,**kwargs): return json.dumps(self.values)


class Fixture:
    def __init__(self):
        self.calls={'fetch':0,'retriever':0,'llm':0,'generate':0}
        self.last_inputs=None
        def fetch(ticker,period='6mo'):
            self.calls['fetch']+=1
            return Frame()
        self.scoring=SimpleNamespace(fetch_price_history=fetch)
        def get_retriever(k=4):
            self.calls['retriever']+=1
            return SimpleNamespace(invoke=lambda q:[SimpleNamespace(metadata={'filename':'a.md'},page_content='reference')])
        self.retriever=SimpleNamespace(get_investment_retriever=get_retriever)
        def search(q,k=5): return self.retriever.get_investment_retriever(k=k).invoke(q)
        def get_llm():
            self.calls['llm']+=1
            return object()
        def rank(top_n=5,ticker_pool=None):
            for ticker in ticker_pool: self.scoring.fetch_price_history(ticker)
            return [{'ticker':t} for t in ticker_pool]
        def prepare(data):
            candidates=self.fd.rank_candidate_stocks(top_n=5,ticker_pool=data['stock_pool'])
            docs=self.fd.search_investment_rules(data['user_question'],k=5)
            return dict(user_question=data['user_question'],cash='70,224 元',candidate_scores='scores',position_plan='plan',rag_context='context',candidate_list=candidates,references=[{'filename':d.metadata['filename'],'content':d.page_content} for d in docs])
        outer=self
        class Chain:
            def invoke(self,inputs):
                outer.last_inputs=inputs
                outer.calls['generate']+=1
                return SimpleNamespace(content='分批 風險 不保證獲利',usage_metadata={'output_tokens':10},response_metadata={'finish_reason':'STOP'})
        def chain():
            self.fd.get_llm()
            return Chain()
        def generate(user_question,cash,stock_pool):
            prep=self.fd.prepare_decision_inputs(dict(user_question=user_question,cash=cash,stock_pool=stock_pool))
            msg=self.fd.get_final_decision_chain().invoke({k:prep[k] for k in ['user_question','cash','candidate_scores','position_plan','rag_context']})
            return dict(answer=msg.content,success=True,references=prep['references'],candidate_list=prep['candidate_list'],position_plan_list=[])
        self.fd=SimpleNamespace(rank_candidate_stocks=rank,search_investment_rules=search,get_llm=get_llm,get_final_decision_chain=chain,prepare_decision_inputs=prepare,generate_final_decision=generate)
        self.r=lab.Recorder()
        self.hooks=lab.RuntimeHooks(self.fd,self.scoring,self.retriever,self.r,ttl=300)
        self.case={'id':'q01','question':'question','expected_sources':['a.md'],'expected_keywords':['分批','風險']}


class Tests(unittest.TestCase):
    def test_baseline_preserves_output_and_timers(self):
        f=Fixture()
        original=f.fd.generate_final_decision('question',70224,lab.POOL)
        with ExitStack() as stack:
            f.hooks.install(stack)
            row=lab.run_one(f.fd,f.r,f.case,'baseline',1,70224)
            self.assertEqual(row['answer'],original['answer'])
            self.assertTrue(row['success'])
            self.assertTrue(row['checks_passed'])
            self.assertEqual(row['candidate_count'],5)
            self.assertEqual(row['cache_events']['market_misses'],5)
            self.assertAlmostEqual(sum(row['stage_s'].values()),row['end_to_end_s'],places=5)
        self.assertEqual(f.fd.get_llm,f.hooks.original_get_llm)

    def test_market_cache_deep_copy_and_expiration(self):
        f=Fixture()
        f.r.begin('market_cache')
        with patch.object(lab.time,'monotonic',return_value=0):
            a=f.hooks.fetch('0050')
            a.values.append(999)
        with patch.object(lab.time,'monotonic',return_value=100):
            b=f.hooks.fetch('0050')
            self.assertEqual(b.values,[100,101])
            self.assertEqual(f.calls['fetch'],1)
        with patch.object(lab.time,'monotonic',return_value=301):
            f.hooks.fetch('0050')
            self.assertEqual(f.calls['fetch'],2)

    def test_resource_reuse_does_not_cache_answers(self):
        f=Fixture()
        with ExitStack() as stack:
            f.hooks.install(stack)
            first=lab.run_one(f.fd,f.r,f.case,'reuse',1,70224)
            second=lab.run_one(f.fd,f.r,f.case,'reuse',2,70224)
        self.assertFalse(first['resource_cache_warm'])
        self.assertTrue(second['resource_cache_warm'])
        self.assertEqual(f.calls['retriever'],1)
        self.assertEqual(f.calls['llm'],1)
        self.assertEqual(f.calls['generate'],2)
        self.assertEqual(f.calls['fetch'],10)

    def test_concise_changes_only_llm_input(self):
        f=Fixture()
        with ExitStack() as stack:
            f.hooks.install(stack)
            base=lab.run_one(f.fd,f.r,f.case,'baseline',1,70224)
            concise=lab.run_one(f.fd,f.r,f.case,'concise',1,70224)
        self.assertEqual(f.r.prepared['user_question'],'question')
        self.assertIn(lab.CONCISE_INSTRUCTION,f.last_inputs['user_question'])
        self.assertEqual(base['base_prompt_input_fingerprint'],concise['base_prompt_input_fingerprint'])

    def test_pairing_excludes_changed_data_and_failed_generation(self):
        f=Fixture()
        with ExitStack() as stack:
            f.hooks.install(stack)
            b=lab.run_one(f.fd,f.r,f.case,'baseline',1,70224)
            v=lab.run_one(f.fd,f.r,f.case,'reuse',1,70224)
        b['end_to_end_s']=20
        v['end_to_end_s']=15
        result=lab.paired_comparison([b,v],'reuse')
        self.assertEqual(result['eligible_pairs'],1)
        self.assertEqual(result['median_reduction_percent'],25)
        v['market_data_fingerprint']='different'
        self.assertEqual(lab.paired_comparison([b,v],'reuse')['excluded_pairs'],{'market_data_changed':1})
        v['success']=False
        self.assertEqual(lab.paired_comparison([b,v],'reuse')['excluded_pairs'],{'generation_failure':1})

    def test_quality_failure_not_filtered_out_to_inflate_speed_claim(self):
        f=Fixture()
        with ExitStack() as stack:
            f.hooks.install(stack)
            b=lab.run_one(f.fd,f.r,f.case,'baseline',1,70224)
            v=lab.run_one(f.fd,f.r,f.case,'reuse',1,70224)
        v['checks_passed']=False
        result=lab.paired_comparison([b,v],'reuse')
        self.assertEqual(result['eligible_pairs'],1)
        self.assertEqual(result['variant_checks_passed'],0)

    def test_small_sample_p95_and_secret_redaction(self):
        self.assertEqual(lab.distribution(list(range(1,11)))['p95_s_nearest_rank'],10)
        self.assertEqual(lab.redact({'answer':['secret-value in text']},['secret-value']),{'answer':['[REDACTED] in text']})


if __name__=='__main__': unittest.main()

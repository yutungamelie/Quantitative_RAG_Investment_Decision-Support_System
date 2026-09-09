"""Latency experiments for the existing Stock AI Assistant.

Examples (run in the existing virtual environment):
  python stock_ai_latency_lab.py profile --questions 2 --output latency_profile.json
  python stock_ai_latency_lab.py compare --experiment reuse --questions 2 --output latency_smoke.json
  python stock_ai_latency_lab.py compare --experiment reuse --questions 10 --rounds 3 --output latency_reuse.json

All commands make real Gemini and market-data requests. Source files and the
existing vector database are not modified. Experimental changes apply only
inside this process; adopt them in the app after reviewing the results.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


POOL = ['0050', '006208', '00878', '00919', '2330']
STAGES = ['market_data', 'scoring_and_ranking', 'retrieval', 'chain_setup', 'llm_generation', 'other']
CONCISE_INSTRUCTION = (
    '\n\n額外的表達要求：請精簡回答，保留系統要求的六個段落，全文以約 400 個中文字為目標。'
    '避免重複解釋；保留股票代號、分數、建議金額、股數、重要風險與不保證獲利的聲明。'
    '不得為了縮短而改動數值。'
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {'n': len(values), 'mean_s': statistics.mean(values),
            'median_s': statistics.median(values),
            'p95_s_nearest_rank': ordered[math.ceil(.95 * len(values)) - 1]}


def check_plan(plan, cash):
    """Check structured allocation output, NOT numbers in natural-language answers."""
    if not plan:
        return {'nonempty': False, 'within_cash_budget': True, 'share_counts_match': None}
    return {
        'nonempty': True,
        'within_cash_budget': sum(p['suggested_amount'] for p in plan) <= int(cash * .8),
        'share_counts_match': all(p['estimated_shares'] == int(p['suggested_amount'] // float(p['close'])) for p in plan),
    }


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    return value


class Recorder:
    def __init__(self):
        self.begin('baseline')

    def begin(self, mode):
        self.mode = mode
        self.stage = Counter()
        self.cache = Counter()
        self.market_fingerprints = {}
        self.prepared = None
        self.usage = None
        self.finish_reason = None


class RuntimeHooks:
    """Measure real project calls; no fake market data, embeddings, or answers."""
    def __init__(self, fd, scoring, retriever, recorder, ttl=300):
        self.fd, self.scoring, self.retriever = fd, scoring, retriever
        self.r = recorder
        self.ttl = ttl
        self.market_cache, self.retriever_cache = {}, {}
        self.llm_cached = None
        self.original_fetch = scoring.fetch_price_history
        self.original_rank = fd.rank_candidate_stocks
        self.original_retrieve = fd.search_investment_rules
        self.original_get_retriever = retriever.get_investment_retriever
        self.original_get_llm = fd.get_llm
        self.original_chain = fd.get_final_decision_chain
        self.original_prepare = fd.prepare_decision_inputs

    def fetch(self, ticker, period='6mo'):
        start = time.perf_counter()
        key = (ticker, period)
        cached = self.market_cache.get(key)
        try:
            if self.r.mode == 'market_cache' and cached and time.monotonic() - cached[0] < self.ttl:
                self.r.cache['market_hits'] += 1
                result = cached[1].copy(deep=True)
            else:
                self.r.cache['market_misses'] += 1
                result = self.original_fetch(ticker, period=period)
                if self.r.mode == 'market_cache':
                    self.market_cache[key] = (time.monotonic(), result.copy(deep=True))
            # Hashing is outside the reported fetch time; it appears in scoring.
        finally:
            self.r.stage['market_data'] += time.perf_counter() - start
        self.r.market_fingerprints[str(key)] = hashlib.sha256(
            result.to_json(orient='split', date_format='iso', double_precision=15).encode()
        ).hexdigest()
        return result

    def rank(self, *args, **kwargs):
        start, before = time.perf_counter(), self.r.stage['market_data']
        try:
            return self.original_rank(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - start
            self.r.stage['scoring_and_ranking'] += max(0, elapsed - (self.r.stage['market_data'] - before))

    def retrieve(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return self.original_retrieve(*args, **kwargs)
        finally:
            self.r.stage['retrieval'] += time.perf_counter() - start

    def get_retriever(self, k=4):
        if self.r.mode == 'reuse' and k in self.retriever_cache:
            self.r.cache['retriever_hits'] += 1
            return self.retriever_cache[k]
        self.r.cache['retriever_misses'] += 1
        result = self.original_get_retriever(k=k)
        if self.r.mode == 'reuse':
            self.retriever_cache[k] = result
        return result

    def get_llm(self):
        if self.r.mode == 'reuse' and self.llm_cached is not None:
            self.r.cache['llm_client_hits'] += 1
            return self.llm_cached
        self.r.cache['llm_client_misses'] += 1
        result = self.original_get_llm()
        if self.r.mode == 'reuse':
            self.llm_cached = result
        return result

    def prepare(self, *args, **kwargs):
        result = self.original_prepare(*args, **kwargs)
        self.r.prepared = result
        return result

    def get_chain(self):
        start = time.perf_counter()
        try:
            chain = self.original_chain()
        finally:
            self.r.stage['chain_setup'] += time.perf_counter() - start
        recorder = self.r

        class TimedChain:
            def invoke(self, inputs):
                actual_inputs = dict(inputs)
                if recorder.mode == 'concise':
                    actual_inputs['user_question'] += CONCISE_INSTRUCTION
                start = time.perf_counter()
                try:
                    message = chain.invoke(actual_inputs)
                    recorder.usage = getattr(message, 'usage_metadata', None)
                    metadata = getattr(message, 'response_metadata', {}) or {}
                    recorder.finish_reason = str(metadata.get('finish_reason', ''))
                    return message
                finally:
                    recorder.stage['llm_generation'] += time.perf_counter() - start

        return TimedChain()

    def install(self, stack):
        for obj, name, fn in [
            (self.scoring, 'fetch_price_history', self.fetch),
            (self.fd, 'rank_candidate_stocks', self.rank),
            (self.fd, 'search_investment_rules', self.retrieve),
            (self.retriever, 'get_investment_retriever', self.get_retriever),
            (self.fd, 'get_llm', self.get_llm),
            (self.fd, 'get_final_decision_chain', self.get_chain),
            (self.fd, 'prepare_decision_inputs', self.prepare),
        ]:
            stack.enter_context(patch.object(obj, name, fn))


def run_one(fd, recorder, case, mode, round_no, cash):
    recorder.begin(mode)
    started = time.perf_counter()
    row = {'case_id': case['id'], 'question': case['question'], 'round': round_no,
           'variant': mode, 'success': False, 'checks_passed': False}
    try:
        result = fd.generate_final_decision(user_question=case['question'], cash=cash, stock_pool=POOL)
        row['end_to_end_s'] = time.perf_counter() - started
        answer = result['answer']
        keywords = [k for k in case['expected_keywords'] if k in answer]
        disclaimer = any(k in answer for k in ['不保證獲利', '不構成正式投資建議', '決策輔助'])
        row.update(success=bool(result['success']), answer=answer,
                   answer_characters=len(answer), hit_keywords=keywords,
                   has_disclaimer=disclaimer,
                   checks_passed=bool(result['success'] and len(keywords) >= 2 and disclaimer),
                   references=result.get('references', []),
                   candidate_count=len(result.get('candidate_list', [])),
                   candidate_list=result.get('candidate_list', []),
                   position_plan=result.get('position_plan_list', []),
                   structured_plan_checks=check_plan(result.get('position_plan_list', []), cash),
                   answer_numeric_consistency=None)
    except Exception as exc:
        row.update(end_to_end_s=time.perf_counter()-started, error_type=type(exc).__name__)
    recorder.stage['other'] = max(0, row['end_to_end_s'] - sum(recorder.stage.values()))
    row['stage_s'] = {s: recorder.stage[s] for s in STAGES}
    row['cache_events'] = dict(recorder.cache)
    row['market_data_fingerprint'] = digest(recorder.market_fingerprints) if recorder.market_fingerprints else None
    row['market_symbols_observed'] = len(recorder.market_fingerprints)
    row['usage_metadata'] = recorder.usage
    row['finish_reason'] = recorder.finish_reason
    if recorder.prepared:
        p = recorder.prepared
        relevant = {k:p[k] for k in ['user_question', 'cash', 'candidate_scores', 'position_plan', 'rag_context']}
        row['base_prompt_input_fingerprint'] = digest(relevant)
        row['retrieved_filenames'] = [d['filename'] for d in p['references']]
        expected = set(case['expected_sources'])
        row['decision_context_source_recall'] = len(expected & set(row['retrieved_filenames'])) / len(expected)
    else:
        row['base_prompt_input_fingerprint'] = None
    hits, misses = recorder.cache['market_hits'], recorder.cache['market_misses']
    row['market_cache_state'] = 'warm' if hits and not misses else 'mixed' if hits else 'cold_or_disabled'
    row['resource_cache_warm'] = bool(recorder.cache['retriever_hits'] and recorder.cache['llm_client_hits'])
    return row


def paired_comparison(rows, experiment, warm_only=False):
    by_pair = {}
    for row in rows:
        by_pair.setdefault((row['round'], row['case_id']), {})[row['variant']] = row
    matched, excluded = [], Counter()
    for group in by_pair.values():
        baseline, variant = group.get('baseline'), group.get(experiment)
        if baseline is None or variant is None:
            excluded['incomplete_pair'] += 1
            continue
        if not baseline['success'] or not variant['success']:
            excluded['generation_failure'] += 1
            continue
        if baseline['candidate_count'] != len(POOL) or variant['candidate_count'] != len(POOL):
            excluded['incomplete_stock_data'] += 1
            continue
        if not baseline['market_data_fingerprint'] or baseline['market_data_fingerprint'] != variant['market_data_fingerprint']:
            excluded['market_data_changed'] += 1
            continue
        if not baseline['base_prompt_input_fingerprint'] or baseline['base_prompt_input_fingerprint'] != variant['base_prompt_input_fingerprint']:
            excluded['quantitative_or_rag_inputs_changed'] += 1
            continue
        if warm_only and experiment == 'market_cache' and variant['market_cache_state'] != 'warm':
            excluded['cache_not_fully_warm'] += 1
            continue
        if warm_only and experiment == 'reuse' and not variant['resource_cache_warm']:
            excluded['resources_not_fully_warm'] += 1
            continue
        matched.append((baseline, variant))
    b = distribution([x['end_to_end_s'] for x,y in matched])
    v = distribution([y['end_to_end_s'] for x,y in matched])
    return {'eligible_pairs':len(matched),'excluded_pairs':dict(excluded),
            'baseline':b,'variant':v,
            'median_reduction_percent':100*(b['median_s']-v['median_s'])/b['median_s'] if b and b['median_s'] else None,
            'baseline_checks_passed':sum(x['checks_passed'] for x,y in matched),
            'variant_checks_passed':sum(y['checks_passed'] for x,y in matched),
            'manual_answer_review_required':True}


def summarize(rows, experiment):
    output = {}
    for mode in dict.fromkeys(r['variant'] for r in rows):
        all_rows = [r for r in rows if r['variant']==mode]
        good = [r for r in all_rows if r['success']]
        output[mode] = {
            'attempts':len(all_rows), 'successes':len(good),
            'checks_passed':sum(r['checks_passed'] for r in all_rows),
            'successful_latency':distribution([r['end_to_end_s'] for r in good]),
            'stage_mean_s_successes_only':{s:statistics.mean(r['stage_s'][s] for r in good) if good else None for s in STAGES},
            'answer_characters':distribution([r['answer_characters'] for r in good]),
            'market_cache_state_counts':dict(Counter(r['market_cache_state'] for r in all_rows)),
            'resource_warm_requests':sum(r['resource_cache_warm'] for r in all_rows),
        }
        # Character counts are not seconds: use a separate schema.
        if output[mode]['answer_characters']:
            c = output[mode]['answer_characters']
            output[mode]['answer_characters'] = {'n':c['n'],'mean':c['mean_s'],'median':c['median_s'],'p95':c['p95_s_nearest_rank']}
    if experiment:
        output['matched_comparison'] = paired_comparison(rows,experiment)
        if experiment in ['market_cache','reuse']:
            output['warm_matched_comparison'] = paired_comparison(rows,experiment,warm_only=True)
    return output


def save_report(path, report, secrets):
    path.parent.mkdir(parents=True, exist_ok=True)
    report['updated_at_utc'] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_name(path.name+'.partial')
    temporary.write_text(json.dumps(redact(report,secrets),ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['profile','compare'])
    p.add_argument('--experiment',choices=['market_cache','reuse','concise'])
    p.add_argument('--repo',type=Path,default=Path.cwd())
    p.add_argument('--questions',type=int,default=2)
    p.add_argument('--rounds',type=int,default=1)
    p.add_argument('--ttl',type=int,default=300)
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args()
    if args.mode=='compare' and not args.experiment:
        p.error('compare requires --experiment')
    if args.mode=='profile' and args.experiment:
        p.error('--experiment is only used with compare')
    if args.questions<1 or args.rounds<1 or args.ttl<1:
        p.error('questions, rounds, and ttl must be positive')
    if args.output.exists():
        p.error('Output already exists; choose a new filename to preserve earlier results.')
    repo = args.repo.resolve()
    sys.path.insert(0,str(repo))
    source_paths = [repo/'env_settings.py']
    for folder in ['data','quant','decision','rag','tests']:
        source_paths.extend(sorted((repo/folder).glob('*.py')))
    report = {'started_at_utc':datetime.now(timezone.utc).isoformat(),
              'status':'starting','mode':args.mode,'experiment':args.experiment,
              'python':platform.python_version(),'platform':platform.platform(),
              'stock_pool':POOL,'cash':70224,'cache_ttl_seconds':args.ttl,
              'rounds':args.rounds,'unique_questions':args.questions,
              'rows':[],'errors':[],
              'protocol':'Sequential paired trials; baseline/variant order alternates. No answer caching. Market-cache TTL includes misses and expires normally. Compare only pairs with all 5 candidates and identical observed price data plus base prompt inputs. No fixed price snapshot is imposed; changing data is detected and excluded from matched comparisons. First-use and warm requests are recorded separately.',
              'quality_note':'Keyword/disclaimer checks and structured allocation checks do not verify factual accuracy, citation coverage, or numbers in generated prose. Review answers manually before making quality-preservation claims.',
              'latency_note':'End-to-end timing includes data fetching, scoring, retrieval, chain setup, generation, and formatting; one-time index creation and imports are excluded. No first-token/streaming measurement.',
              'concise_instruction':CONCISE_INSTRUCTION if args.experiment=='concise' else None,
              'source_file_sha256':{str(f.relative_to(repo)):hashlib.sha256(f.read_bytes()).hexdigest() for f in source_paths if f.is_file()},
              'corpus_document_sha256':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((repo/'rag_docs').glob('*.md'))},
              'versions':{}}
    secrets = []
    try:
        from env_settings import EnvSettings
        from tests.eval_cases import EVAL_CASES
        import rag.build_vector_db as builder
        import rag.retriever as retriever
        import decision.final_decision_chain as fd
        import quant.scoring as scoring
        from chromadb import PersistentClient
        settings = EnvSettings()
        secrets = [settings.GOOGLE_API_KEY]
        report['gemini_model'] = settings.GEMINI_MODEL
        for package in ['langchain-core','langchain-google-genai','langchain-chroma','chromadb','pandas','yfinance']:
            report['versions'][package] = importlib.metadata.version(package)
        if args.questions>len(EVAL_CASES):
            raise ValueError(f'Only {len(EVAL_CASES)} evaluation questions are available')
        cases = EVAL_CASES[:args.questions]
        report['case_ids'] = [c['id'] for c in cases]
        calls = args.questions*args.rounds*(2 if args.mode=='compare' else 1)
        report['answer_calls_planned'] = calls
        print(f'Planned generated answers: {calls}; real Gemini and market-data requests.',flush=True)
        with tempfile.TemporaryDirectory(prefix='stock_ai_latency_',ignore_cleanup_errors=True) as tmp:
            vector_dir = Path(tmp)/'investment_rules'
            with ExitStack() as stack:
                stack.enter_context(patch.object(builder,'VECTOR_DB_DIR',vector_dir))
                stack.enter_context(patch.object(retriever,'VECTOR_DB_DIR',vector_dir))
                start = time.perf_counter()
                builder.build_vector_db()
                report['index_setup_s'] = time.perf_counter()-start
                report['indexed_chunks'] = PersistentClient(path=str(vector_dir)).get_collection('investment_rules').count()
                r = Recorder()
                hooks = RuntimeHooks(fd,scoring,retriever,r,ttl=args.ttl)
                hooks.install(stack)
                report['status'] = 'running'
                trial = 0
                for round_no in range(1,args.rounds+1):
                    for case_index,case in enumerate(cases):
                        order = ['baseline']
                        if args.mode=='compare':
                            order = ['baseline',args.experiment]
                            if (round_no+case_index)%2==0:
                                order.reverse()
                        for variant in order:
                            trial += 1
                            print(f'[{trial}/{calls}] round={round_no} case={case["id"]} variant={variant}',flush=True)
                            row = run_one(fd,r,case,variant,round_no,70224)
                            report['rows'].append(row)
                            report['summary'] = summarize(report['rows'],args.experiment)
                            save_report(args.output,report,secrets)
                            print(f'  success={row["success"]} checks={row["checks_passed"]} total={row["end_to_end_s"]:.2f}s | '+', '.join(f'{s}={row["stage_s"][s]:.2f}s' for s in STAGES),flush=True)
                report['status'] = 'completed'
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        print('Interrupted; completed trials will be saved.',flush=True)
    except Exception as exc:
        report['status'] = 'failed'
        error = {'type':type(exc).__name__}
        if isinstance(exc,ModuleNotFoundError):
            error['missing_module'] = exc.name
        report['errors'].append(error)
        print(f'Experiment stopped: {type(exc).__name__}. See the report; API keys are not printed.',flush=True)
    report['summary'] = summarize(report['rows'],args.experiment)
    save_report(args.output,report,secrets)
    print(f'Report saved: {args.output.resolve()}',flush=True)
    return 0 if report['status']=='completed' else 1


if __name__=='__main__':
    raise SystemExit(main())

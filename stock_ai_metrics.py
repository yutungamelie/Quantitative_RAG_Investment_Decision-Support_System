"""Measure an existing Stock AI Assistant checkout; never print API keys.

Static: python stock_ai_metrics.py --repo /path/to/stock_ai_assistant
Live:   python stock_ai_metrics.py --repo /path/to/stock_ai_assistant --live

Live mode calls Gemini embeddings/generation and Yahoo Finance. It executes
the repository's existing evaluators against a NEW temporary vector database.
It does not modify source code, the existing vector database, or GitHub.
"""
from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def read_assignment(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f'Missing assignment: {name}')


def latency_summary(values):
    if not values:
        return None
    ordered = sorted(values)
    return {'n':len(values), 'mean_seconds':statistics.mean(values),
            'median_seconds':statistics.median(values),
            'p95_seconds_nearest_rank':ordered[math.ceil(0.95*len(values))-1]}


def source_metrics(rows, k):
    if not rows:
        return None
    hit_rates, recalls, reciprocals = [], [], []
    for row in rows:
        expected = set(row['expected_sources'])
        retrieved = row['retrieved_sources'][:k]
        hit_rates.append(bool(expected & set(retrieved)))
        recalls.append(len(expected & set(retrieved))/len(expected))
        rank = next((i for i,s in enumerate(retrieved,1) if s in expected), None)
        reciprocals.append(1/rank if rank else 0)
    return {'n':len(rows), 'hit_rate':statistics.mean(hit_rates),
            'macro_source_recall':statistics.mean(recalls),
            'mrr_at_k':statistics.mean(reciprocals)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, default=Path('stock_ai_live_metrics.json'))
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    repo = args.repo.resolve()
    cases = read_assignment(repo/'tests/eval_cases.py', 'EVAL_CASES')
    tickers = read_assignment(repo/'data/tickers.py', 'TAIWAN_STOCKS')
    docs = [{'filename':p.name,'content':p.read_text(encoding='utf-8')}
            for p in sorted((repo/'rag_docs').glob('*.md'))]
    result = {
        'started_at_utc':datetime.now(timezone.utc).isoformat(),
        'mode':'live' if args.live else 'static', 'python':platform.python_version(),
        'platform':platform.platform(),
        'documents':len(docs), 'configured_instruments':len(tickers),
        'instrument_types':dict(Counter(t['type'] for t in tickers.values())),
        'unique_questions':len({c['question'] for c in cases}),
        'expected_source_labels':sum(len(c['expected_sources']) for c in cases),
        'expected_keyword_labels':sum(len(c['expected_keywords']) for c in cases),
        'chunks':None, 'indexed_chunks':None, 'retrieval':None, 'answers':None,
        'citation_coverage':None,
        'citation_coverage_note':'Requires claim-level annotation and evidence checking; reference-list presence is not citation coverage.',
        'errors':[], 'versions':{},
        'protocol':'Sequential run, no warm-up, existing 10 cases. Final-answer latency includes price fetching, scoring, RAG retrieval, and Gemini generation. API failures are excluded from success latency and reported separately.'
    }
    for package in ['langchain-text-splitters','langchain-chroma','langchain-google-genai','chromadb','pandas','yfinance']:
        try:
            result['versions'][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result['versions'][package] = None

    build_tree = ast.parse((repo/'rag/build_vector_db.py').read_text(encoding='utf-8-sig'))
    constructor = next(n.value for n in ast.walk(build_tree) if isinstance(n,ast.Assign)
                       and any(isinstance(t,ast.Name) and t.id=='splitter' for t in n.targets))
    config = {kw.arg:ast.literal_eval(kw.value) for kw in constructor.keywords}
    result['chunking_config'] = config
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        splitter = RecursiveCharacterTextSplitter(**config)
        result['chunks_per_file'] = [{'filename':d['filename'],'chunks':len(splitter.split_text(d['content']))} for d in docs]
        result['chunks'] = sum(d['chunks'] for d in result['chunks_per_file'])
    except ImportError as exc:
        result['errors'].append({'stage':'text_splitting','type':type(exc).__name__})

    exit_code = 0
    if args.live:
        sys.path.insert(0,str(repo))
        try:
            from env_settings import EnvSettings
            import rag.build_vector_db as builder
            import rag.retriever as retriever
            import tests.test_rag_retrieval as retrieval_eval
            import tests.test_final_decision as answer_eval
            settings = EnvSettings()
            result['gemini_model'] = settings.GEMINI_MODEL
            result['embedding_model'] = 'models/gemini-embedding-001'
            with tempfile.TemporaryDirectory(prefix='stock_ai_benchmark_', ignore_cleanup_errors=True) as tmp:
                builder.VECTOR_DB_DIR = Path(tmp)/'investment_rules'
                retriever.VECTOR_DB_DIR = builder.VECTOR_DB_DIR
                start = time.perf_counter()
                builder.build_vector_db()
                result['index_build_seconds'] = time.perf_counter()-start
                from chromadb import PersistentClient
                result['indexed_chunks'] = PersistentClient(path=str(builder.VECTOR_DB_DIR)).get_collection('investment_rules').count()

                search = retrieval_eval.search_investment_rules
                retrieval_timings = []
                def timed_search(*pos,**kw):
                    start = time.perf_counter()
                    value = search(*pos,**kw)
                    retrieval_timings.append(time.perf_counter()-start)
                    return value
                retrieval_eval.search_investment_rules = timed_search
                try:
                    rows = retrieval_eval.evaluate_retrieval()
                    result['retrieval'] = {'original_passed':sum(r['passed'] for r in rows),'total':len(rows),
                        'at_k':{str(k):source_metrics(rows,k) for k in [1,3,5]},
                        'latency':latency_summary(retrieval_timings),'rows':rows,
                        'note':'@1 and @3 use prefixes of the existing top-5 results. Source labels do not establish passage-level answer correctness.'}
                finally:
                    retrieval_eval.search_investment_rules = search

                generate = answer_eval.generate_final_decision
                original_cases = answer_eval.EVAL_CASES
                answer_rows = []
                for case in original_cases:
                    captured = {}
                    def timed_generate(*pos,**kw):
                        start = time.perf_counter()
                        value = generate(*pos,**kw)
                        captured.update({'elapsed_seconds':time.perf_counter()-start,'result':value})
                        return value
                    answer_eval.generate_final_decision = timed_generate
                    answer_eval.EVAL_CASES = [case]
                    try:
                        evaluated = answer_eval.evaluate_final_answers()[0]
                        raw = captured['result']
                        answer_rows.append({**evaluated,'elapsed_seconds':captured['elapsed_seconds'],
                                            'answer':raw['answer'],'references':raw.get('references',[]),
                                            'strict_passed':bool(evaluated['passed'] and raw['success'])})
                    except Exception as exc:
                        answer_rows.append({'id':case['id'],'success':False,'strict_passed':False,'error_type':type(exc).__name__})
                    finally:
                        answer_eval.generate_final_decision = generate
                        answer_eval.EVAL_CASES = original_cases
                successful = [r for r in answer_rows if r['success']]
                result['answers'] = {
                    'total':len(answer_rows),'successes':len(successful),
                    'original_passed':sum(bool(r.get('passed')) for r in answer_rows),
                    'strict_passed':sum(r['strict_passed'] for r in answer_rows),
                    'latency_successes_only':latency_summary([r['elapsed_seconds'] for r in successful]),
                    'reference_list_presence_rate_successes_only':sum(bool(r['references']) for r in successful)/len(successful) if successful else None,
                    'rows':answer_rows,
                    'note':'Keyword/disclaimer checks are not factual accuracy. Review saved answers and references to annotate citation coverage.'}
        except Exception as exc:
            # Do not serialize provider exception text: it can contain sensitive URLs.
            result['errors'].append({'stage':'live','type':type(exc).__name__})
            exit_code = 1
    result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(f'Saved metrics to {args.output.resolve()}')
    if result['errors']:
        print('Some measurements are unavailable; inspect errors in the JSON report.')
    return exit_code


if __name__=='__main__':
    raise SystemExit(main())

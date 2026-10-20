"""Offline evidence, paragraph isolation and dependency accounting checks."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deep_research.cli import export_run
from deep_research.engine import Config, Research, locate_quote, validate_sections
from deep_research.metrics import Metrics


class FakeLLM:
    def __init__(self, metrics, *args, **kwargs):
        self.metrics = metrics
        self.model = 'fake-offline'
        self.responses = {}
        self.calls = []

    async def ask(self, name, system, payload, parents=()):
        self.calls.append((name, copy.deepcopy(payload)))
        event = self.metrics.start('model', name, parents)
        self.metrics.end(event)
        return copy.deepcopy(self.responses[name]), event

    async def close(self):
        pass


class NoNetworkWeb:
    def __init__(self, *args, **kwargs):
        self.search_calls = 0
        self.fetch_calls = 0

    async def search(self, *args, **kwargs):
        self.search_calls += 1
        raise AssertionError('This operation must not search')

    async def fetch(self, *args, **kwargs):
        self.fetch_calls += 1
        raise AssertionError('This operation must not fetch')

    async def close(self):
        pass


class EvidenceValidationTests(unittest.TestCase):
    def test_exact_quote_offsets_and_whitespace_normalization(self):
        text = '前言。\n结果：速度为 2 倍，条件是同一平台。\n尾注。'
        quote = '速度为 2 倍，条件是同一平台。'
        start, end = locate_quote(text, quote)
        self.assertEqual(text[start:end], quote)
        self.assertEqual(start, text.index(quote))
        start, end = locate_quote(text, '速度为2倍，条件是同一平台。')
        self.assertEqual(text[start:end], quote)

    def test_fabricated_paraphrased_and_oversize_quotes_rejected(self):
        text = '速度为 2 倍，条件是同一平台。'
        for quote in ('速度为 20 倍', '性能提升了一倍', '', None, '字'*301):
            with self.subTest(quote=quote):
                self.assertIsNone(locate_quote(text, quote))

    def test_unknown_citation_rejected_even_alongside_a_valid_one(self):
        for unknown in ('E999', 'S1'):
            with self.subTest(unknown=unknown), self.assertRaisesRegex(ValueError, '未知引用'):
                validate_sections([dict(id='P1', title='结果', body=f'结论 [E1] [{unknown}]')], {'E1'})
        valid = [dict(id='P1', title='结果', body='结论 [E1]')]
        self.assertEqual(validate_sections(valid, {'E1'}), valid)

    def test_endpoint_is_not_a_source_link(self):
        valid = [dict(id='P1', title='接口', body='服务默认监听 http://localhost:8000 [E1]')]
        self.assertEqual(validate_sections(valid, {'E1'}), valid)
        with self.assertRaisesRegex(ValueError, '超链接'):
            validate_sections([dict(id='P1', title='来源', body='[伪来源](https://invented.invalid) [E1]')], {'E1'})


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.llm_patch = patch('deep_research.engine.LLM', FakeLLM)
        self.web_patch = patch('deep_research.engine.Web', NoNetworkWeb)
        self.llm_patch.start()
        self.web_patch.start()
        self.addCleanup(self.llm_patch.stop)
        self.addCleanup(self.web_patch.stop)
        self.runner = Research(self.output, Config(mode='B'))

    async def asyncTearDown(self):
        await self.runner.close()

    async def test_unanswered_alignment_stops_before_any_search(self):
        self.runner.llm.responses['align'] = {
            'questions': ['报告给谁看？'], 'plan': ['核查关键证据'],
            'assumptions': ['读者暂未确定']}
        state = await self.runner.run('研究一个主题', interactive=False, accept_assumptions=False)
        self.assertEqual(state['status'], 'needs_alignment')
        self.assertEqual([name for name, _ in self.runner.llm.calls], ['align'])
        self.assertEqual(self.runner.web.search_calls, 0)
        self.assertEqual(self.runner.web.fetch_calls, 0)
        self.assertFalse((self.output/'report.md').exists())
        saved = json.loads((self.output/'state.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['alignment']['questions'], ['报告给谁看？'])

    async def test_process_drops_fabrication_and_stores_actual_source_offsets(self):
        text = '摘要\n在相同平台测试，耗时由10秒降到5秒。\n结束'
        quote = '在相同平台测试，耗时由10秒降到5秒。'
        self.runner.llm.responses['extract_evidence'] = {'evidence': [
            dict(quote=quote, claim='该测试耗时减半', conditions='相同平台', date='', stance='support'),
            dict(quote='耗时由10秒降到1秒。', claim='伪造数字'),
            dict(quote='性能提升了十倍。', claim='伪造结论'),
        ]}
        fetch = self.runner.metrics.start('fetch', 'fetch')
        self.runner.metrics.end(fetch)
        await self.runner.process([(dict(id='S1', title='测试'), text, fetch)], '耗时比较')
        self.assertEqual(self.runner.rejected_quotes, 2)
        self.assertEqual(len(self.runner.evidence), 1)
        evidence = self.runner.evidence[0]
        self.assertEqual(evidence['id'], 'E1')
        self.assertEqual(evidence['source_id'], 'S1')
        self.assertEqual(evidence['quote'], text[evidence['start']:evidence['end']])
        self.assertEqual(evidence['quote'], quote)

    async def test_revise_changes_only_target_without_search_or_input_mutation(self):
        original = dict(
            question='测试报告', effective_brief='写给同事', revision=0, status='complete',
            sources=[dict(id='S1', title='测试', url='https://example.org/source')],
            evidence=[dict(id='E1', source_id='S1', quote='原文', start=0, end=2, conditions='实验条件')],
            queries=['原查询'], sections=[
                dict(id='P1', title='第一段', body='原有判断 [E1]\n\n保留空白。  '),
                dict(id='P2', title='第二段', body='这一段较长，需要修改。[E1]'),
                dict(id='P3', title='第三段', body='标点、换行\n和 Unicode：é 不改。'),
            ])
        encode = lambda value: json.dumps(value, ensure_ascii=False).encode('utf-8')
        original_bytes = encode(original)
        untouched = {s['id']: encode(s) for s in original['sections'] if s['id'] != 'P2'}
        self.runner.llm.responses['revise'] = dict(title='第二段', body='简短结论。[E1]')
        revised = await self.runner.revise(original, 'P2', '压短一点', research=False)
        self.assertEqual(encode(original), original_bytes)
        self.assertEqual(revised['sections'][1]['body'], '简短结论。[E1]')
        for section in revised['sections']:
            if section['id'] != 'P2':
                self.assertEqual(encode(section), untouched[section['id']])
        self.assertEqual([name for name, _ in self.runner.llm.calls], ['revise'])
        self.assertEqual(self.runner.web.search_calls, 0)
        self.assertEqual(self.runner.web.fetch_calls, 0)
        self.assertEqual(revised['revision'], 1)
        saved = json.loads((self.output/'state.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, revised)


class ExportTests(unittest.TestCase):
    def test_export_allowlist_omits_private_inputs_and_rejects_pasted_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'run'
            source.mkdir()
            state = dict(status='complete', question='研究', sections=[], evidence=[],
                         sources=[dict(id='S1', title='来源', url='https://example.org',
                                       raw_path='/private/raw-page.txt')],
                         summaries=[dict(summary='不导出完整摘要')])
            (source/'state.json').write_text(json.dumps(state), encoding='utf-8')
            (source/'report.md').write_text('正常报告', encoding='utf-8')
            (source/'metrics.json').write_text('{}', encoding='utf-8')
            (source/'events.jsonl').write_text('{}\n', encoding='utf-8')
            (source/'prompts.jsonl').write_text('完整私密提示词', encoding='utf-8')
            (source/'.env').write_text('PRIVATE=test', encoding='utf-8')
            (source/'pages').mkdir()
            (source/'pages'/'S1.txt').write_text('网页完整正文', encoding='utf-8')
            output = root/'public'
            with patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''}):
                export_run(source, output)
                self.assertEqual({p.name for p in output.iterdir()},
                                 {'report.md', 'metrics.json', 'events.jsonl', 'evidence.json'})
                exported = json.loads((output/'evidence.json').read_text(encoding='utf-8'))
                self.assertNotIn('raw_path', exported['sources'][0])
                self.assertNotIn('summaries', exported)
                self.assertEqual(exported['sources'][0]['url'], 'https://example.org')
                (source/'report.md').write_text('意外粘贴 sk-'+'a'*32, encoding='utf-8')
                rejected = root/'rejected'
                with self.assertRaisesRegex(ValueError, '密钥'):
                    export_run(source, rejected)
                self.assertFalse(rejected.exists())


class MetricsTests(unittest.TestCase):
    def test_parallel_branches_count_total_calls_but_take_longest_dependency_path(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = Metrics(Path(directory))
            def span(kind, name, parents, duration):
                event = metrics.start(kind, name, parents)
                metrics.end(event)
                metrics.events[-1]['elapsed_s'] = duration
                return event
            root = span('model', 'plan', [], 2)
            left = span('model', 'left', [root], 3)
            right = span('model', 'right', [root], 5)
            join = span('join', 'merge', [left, right], 0)
            span('model', 'write', [join], 7)
            result = metrics.summary()
            self.assertEqual(result['model_calls'], 4)
            self.assertEqual(result['critical_path_model_rounds'], 3)
            self.assertEqual(result['dependency_path_seconds'], 14)
            self.assertFalse(result['token_totals_complete'])
            self.assertEqual(result['calls_without_usage'], 4)


if __name__ == '__main__':
    unittest.main()

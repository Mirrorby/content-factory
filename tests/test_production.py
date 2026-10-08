import copy
from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch
import production as p
from factory.buffer_api import Buffer


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.config, self.phrases = p.load_config()
        self.character = self.config['characters'][0]
        self.state = {'jobs': [], 'usage': {}}
        self.time = datetime(2026, 1, 30, 5, 17, tzinfo=timezone.utc)
        self.empty = dict.fromkeys(p.PLATFORMS, 0)

    def test_first_batch_then_eight_after_four_days(self):
        self.assertEqual(p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time), 10)
        self.assertEqual(len({j['spec']['phrase']['id'] for j in self.state['jobs']}), 10)
        for i, j in enumerate(self.state['jobs']):
            for delivery in j['deliveries'].values():
                delivery['status'] = 'sent' if i < 8 else 'scheduled'
        counts = dict.fromkeys(p.PLATFORMS, 2)
        self.assertEqual(p.plan(self.config, self.phrases, self.character, self.state, counts,
                                self.time+timedelta(days=4)), 8)
        self.assertEqual(len(self.state['jobs']), 18)

    def test_resume_does_not_reserve_duplicate_batch(self):
        p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time)
        before = copy.deepcopy(self.state)
        self.assertEqual(p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time), 0)
        self.assertEqual(self.state, before)

    def test_backlog_counts_before_generation(self):
        p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time)
        self.assertEqual(p.needed(self.empty, self.state['jobs']), 0)
        self.assertEqual(p.plan(self.config, self.phrases, self.character, self.state, self.empty,
                                self.time+timedelta(days=4)), 0)

    def test_stalled_platform_caps_backlog(self):
        p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time)
        for j in self.state['jobs']:
            j['deliveries']['youtube']['status'] = 'sent'
            j['deliveries']['instagram']['status'] = 'sent'
        self.assertEqual(p.plan(self.config, self.phrases, self.character, self.state, self.empty,
                                self.time+timedelta(days=4)), 0)

    def test_96_hours_crosses_month_without_cron_reset(self):
        last = self.time.isoformat()
        self.assertFalse(p.due(last, self.time+timedelta(hours=95)))
        self.assertTrue(p.due(last, self.time+timedelta(hours=96)))

    def test_unknown_submission_prevents_duplicate(self):
        p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time)
        job = self.state['jobs'][0]
        job['status'] = 'ready'
        class API:
            calls = 0
            def queue(self, channels): return []
            def create(self, *args):
                self.calls += 1
                raise RuntimeError('Simulated response loss after server accepts request')
        api = API()
        snapshots = []
        save = lambda: snapshots.append(copy.deepcopy(self.state))
        with patch.object(p, 'expose_video', return_value='https://media.example/test.mp4'):
            with self.assertRaises(RuntimeError):
                p.deliver(api, None, self.config, self.character, self.state, save)
        self.assertEqual(job['deliveries']['youtube']['status'], 'unknown')
        self.assertEqual(snapshots[-1]['jobs'][0]['deliveries']['youtube']['status'], 'unknown')
        with self.assertRaises(RuntimeError):
            p.reconcile(api, self.character, self.state, save)
        self.assertEqual(api.calls, 1)

    def test_full_queue_keeps_waiting_video(self):
        p.plan(self.config, self.phrases, self.character, self.state, self.empty, self.time)
        self.state['jobs'][0]['status'] = 'ready'
        class API:
            def queue(self, channels): return [{}]*10
            def create(self, *args): raise AssertionError('Full queue must not receive posts')
        p.deliver(API(), None, self.config, self.character, self.state, lambda: None)
        self.assertTrue(all(d['status']=='waiting' for d in self.state['jobs'][0]['deliveries'].values()))

    def test_queue_reads_all_pages(self):
        api = Buffer('fake', 'org')
        pages = [dict(posts=dict(edges=[{'node':{'id':'a'}}], pageInfo=dict(hasNextPage=True,endCursor='next'))),
                 dict(posts=dict(edges=[{'node':{'id':'b'}}], pageInfo=dict(hasNextPage=False,endCursor=None)))]
        with patch.object(api, 'query', side_effect=pages) as query:
            self.assertEqual(len(api.queue(['ch'])), 2)
            self.assertEqual(query.call_args.args[1]['after'], 'next')

    def test_cards_fit_two_lines(self):
        import textwrap
        for phrase in self.phrases:
            for key in ('hook','game','reaction','cta'):
                self.assertLessEqual(len(textwrap.wrap(phrase[key], width=29)), 2, phrase['id'])


if __name__ == '__main__':
    unittest.main()

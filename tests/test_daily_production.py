import copy
import unittest
from datetime import datetime, date, timezone
from unittest.mock import Mock, patch
import production as p
from daily_production import plan_daily, slots, tomorrow
from factory.buffer_api import Buffer

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.config, self.phrases = p.load_config()
        self.character = copy.deepcopy(self.config['characters'][0])
        self.character['channels'] = {name:name for name in p.PLATFORMS}
        self.channels = {name: {'timezone':'America/New_York', 'postingSchedule':[
            dict(day=day, paused=False, times=['19:15','08:45'])
            for day in ('mon','tue','wed','thu','fri','sat','sun')]} for name in p.PLATFORMS}
        self.state = {'jobs':[], 'usage':{}}
        self.current = datetime(2026,10,9,20,tzinfo=timezone.utc)
    def test_two_distinct_and_idempotent(self):
        jobs, count = plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        self.assertEqual(count,2)
        self.assertEqual(len({j['spec']['phrase']['id'] for j in jobs}),2)
        self.assertEqual(jobs[0]['target_date'],'2026-10-10')
        self.assertEqual(jobs[0]['deliveries']['youtube']['target_due_at'],'2026-10-10T12:45:00Z')
        again, count = plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        self.assertEqual(count,0)
        self.assertEqual(again,jobs)
        self.assertEqual(len(self.state['jobs']),2)
        self.assertTrue(all(j['spec']['actor_asset'] is None for j in jobs))
    def test_next_day_adds_only_two(self):
        for current in (self.current, datetime(2026,10,10,10,tzinfo=timezone.utc)):
            plan_daily(self.config,self.phrases,self.character,self.state,self.channels,current)
        self.assertEqual(len(self.state['jobs']),4)
        self.assertEqual(len({j['id'] for j in self.state['jobs']}),4)
    def test_posting_timezone_and_dst(self):
        self.assertEqual(tomorrow(datetime(2026,10,10,1,tzinfo=timezone.utc),self.config),date(2026,10,11))
        self.assertEqual(slots(date(2026,11,1),self.channels['youtube'])[0],'2026-11-01T13:45:00Z')
    def test_exact_buffer_timestamp(self):
        api=Buffer('test','test');api.query=Mock(return_value={'createPost':{'post':{'id':'test'}}})
        api.create('test','youtube','https://example.com/test.mp4','Test',self.config['caption'],due_at='2026-10-10T12:45:00Z')
        payload=api.query.call_args.args[1]['input']
        self.assertEqual(payload['mode'],'customScheduled')
        self.assertEqual(payload['dueAt'],'2026-10-10T12:45:00Z')
    def test_expired_approval_never_publishes(self):
        jobs,_=plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        job=jobs[0];job.update(status='ready',sha256='sha',telegram_review={'sha256':'sha'},review={'approved_sha256':'sha'})
        api=Mock()
        with patch('production.now',return_value=datetime(2026,10,12,tzinfo=timezone.utc)):
            p.deliver(api,None,self.config,self.character,{'jobs':[job]},lambda:None)
        api.create.assert_not_called()
        api.queue.assert_not_called()
        self.assertTrue(all(d['status']=='expired' for d in job['deliveries'].values()))
if __name__=='__main__': unittest.main()

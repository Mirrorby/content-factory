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
        self.assertTrue(all(j['spec']['actor_seconds'] == 4 for j in jobs))
        self.config['actor_seconds'] = 8
        frozen, count = plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        self.assertEqual(count, 0)
        self.assertTrue(all(j['spec']['actor_seconds'] == 4 for j in frozen))
    def test_next_day_adds_only_two(self):
        for current in (self.current, datetime(2026,10,10,10,tzinfo=timezone.utc)):
            plan_daily(self.config,self.phrases,self.character,self.state,self.channels,current)
        self.assertEqual(len(self.state['jobs']),4)
        self.assertEqual(len({j['id'] for j in self.state['jobs']}),4)
    def test_scene_cycle_snapshots_and_retries(self):
        from datetime import timedelta
        scene_count = len(self.character['scenes'])
        for day in range(scene_count // 2):
            jobs, _ = plan_daily(self.config,self.phrases,self.character,self.state,
                                self.channels,self.current + timedelta(days=day))
            self.assertNotEqual(jobs[0]['spec']['scene']['id'], jobs[1]['spec']['scene']['id'])
        scenes = [j['spec']['scene']['id'] for j in self.state['jobs']]
        self.assertEqual(len(scenes), len(set(scenes)))
        usage = copy.deepcopy(self.state['scene_usage'])
        plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        self.assertEqual(self.state['scene_usage'], usage)
        saved_setting = self.state['jobs'][0]['spec']['scene']['setting']
        self.character['scenes'][0]['setting'] = 'Changed config'
        self.assertEqual(self.state['jobs'][0]['spec']['scene']['setting'], saved_setting)
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

    def test_audio_recovery_only_for_confirmed_uncharged_failure(self):
        from daily_production import retryable_audio_failure
        renderer = {'clips':[{'status':'blocked_or_empty'}]}
        reason = ('We encountered an issue with the audio for your prompt. '
                  'You have not been charged for this attempt.')
        response = {'done':True,'response':{'generateVideoResponse':{
            'raiMediaFilteredReasons':[reason]}}}
        self.assertTrue(retryable_audio_failure(renderer,response))
        self.assertFalse(retryable_audio_failure({'clips':[{'status':'pending'}]},response))
        self.assertFalse(retryable_audio_failure(renderer,{'done':False}))
        self.assertFalse(retryable_audio_failure(renderer,{'done':True,'error':{'code':500}}))
        response['response']['generateVideoResponse']['raiMediaFilteredReasons']=['Content safety rejection']
        self.assertFalse(retryable_audio_failure(renderer,response))
    def test_audio_recovery_preserves_slots_and_has_only_one_attempt(self):
        from daily_production import recover_audio_failure
        jobs,_=plan_daily(self.config,self.phrases,self.character,self.state,self.channels,self.current)
        job=jobs[0];job['status']='rendering'
        renderer={'clips':[{'status':'blocked_or_empty'}]}
        response={'done':True,'response':{'generateVideoResponse':{'raiMediaFilteredReasons':[
            'We encountered an issue with the audio for your prompt. You have not been charged for this attempt.']}}}
        with patch('review_telegram.read_json',side_effect=[renderer,response]):
            replacement=recover_audio_failure(Mock(),self.state,job,lambda:None)
        self.assertEqual(len(self.state['jobs']),2)
        self.assertEqual(replacement['deliveries'],job['deliveries'])
        self.assertEqual(replacement['spec']['phrase'],job['spec']['phrase'])
        self.assertEqual(self.state['archive'][0]['id'],job['id'])
        replacement['status']='rendering'
        self.assertIsNone(recover_audio_failure(Mock(),self.state,replacement,lambda:None))

if __name__=='__main__': unittest.main()

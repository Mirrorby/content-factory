import copy
import unittest
from review_telegram import authorize_callback, decide, status_label


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.job = dict(id='prod-maya-000011', status='ready', sha256='new-video',
                        deliveries={p:{'status':'waiting'} for p in ('youtube','tiktok','instagram')})
        self.state = {'characters':{'maya':{'jobs':[self.job]}}}
        self.query = {'data':'cf:a:prod-maya-000011', 'from':{'id':123},
                      'message':{'message_id':9,'chat':{'id':123,'type':'private'}}}
        self.receipt = dict(status='sent',chat_id='123',bot_id='456',message_id=9,video_sha256='new-video')

    def check(self):
        return authorize_callback(self.query,'123','456',self.state,lambda _: self.receipt)

    def test_owner_can_approve_exact_video(self):
        job, receipt, action = self.check()
        self.assertTrue(decide(job,receipt,action))
        self.assertEqual(job['review']['approved_sha256'],'new-video')

    def test_other_user_cannot_approve(self):
        self.query['from']['id']=987
        self.assertIsNone(self.check())

    def test_wrong_message_cannot_approve(self):
        self.query['message']['message_id']=10
        self.assertIsNone(self.check())

    def test_changed_video_cannot_use_old_receipt(self):
        self.receipt['video_sha256']='old-video'
        self.assertIsNone(self.check())

    def test_unconfirmed_delivery_cannot_approve(self):
        self.receipt['status']='sending'
        self.assertIsNone(self.check())

    def test_reject_cannot_be_reversed_by_repeat_press(self):
        self.assertTrue(decide(self.job,self.receipt,'r'))
        self.assertFalse(decide(self.job,self.receipt,'a'))
        self.assertEqual(self.job['status'],'rejected')
        self.assertNotIn('approved_sha256',self.job['review'])
        self.assertTrue(all(d['status']=='rejected' for d in self.job['deliveries'].values()))

    def test_repeat_approve_does_not_reset_delivery(self):
        decide(self.job,self.receipt,'a')
        self.job['deliveries']['youtube']={'status':'scheduled','post_id':'existing'}
        before=copy.deepcopy(self.job)
        self.assertFalse(decide(self.job,self.receipt,'a'))
        self.assertEqual(self.job,before)

    def test_unknown_buffer_submission_is_visible(self):
        decide(self.job,self.receipt,'a')
        self.job['deliveries']['youtube']['status']='unknown'
        self.assertIn('проверка',status_label(self.job))


if __name__ == '__main__':
    unittest.main()

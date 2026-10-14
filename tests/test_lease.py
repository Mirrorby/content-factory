import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from botocore.exceptions import ClientError
from factory.lease import Lease


class Store:
    bucket, prefix = 'private', 'production/'
    def __init__(self, value=None, uploaded=0):
        self.client = self
        self.value, self.uploaded, self.etag, self.writes = value, uploaded, '1', 0
    def get_object(self, **kwargs):
        if self.value is None:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        return {'Body': io.BytesIO(json.dumps(self.value).encode()), 'ETag': self.etag,
                'LastModified': datetime.fromtimestamp(self.uploaded, timezone.utc)}
    def put(self, name, data, **kwargs):
        if (kwargs.get('IfNoneMatch') and self.value is not None or
            kwargs.get('IfMatch') and kwargs['IfMatch'] != self.etag):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.value = json.loads(data); self.writes += 1; self.etag = str(self.writes+1)
        return {'ETag': self.etag}


class LeaseTests(unittest.TestCase):
    @patch('factory.lease.time.time', return_value=1000)
    def test_only_one_owner_and_conditional_release(self, _):
        store = Store()
        first, second = Lease(store, 'production', 21600), Lease(store, 'worker', 180)
        self.assertTrue(first.acquire()); self.assertFalse(second.acquire())
        first.release(); self.assertTrue(second.acquire())
        first.release()
        self.assertEqual(store.value['owner'], second.owner)
        with self.assertRaisesRegex(RuntimeError, 'lease lost'):
            first.assert_owned()

    @patch('factory.lease.time.time', return_value=1000)
    def test_stale_worker_recovers_but_active_generator_stays_protected(self, _):
        self.assertTrue(Lease(Store({'worker': True}, uploaded=100), 'production', 21600).acquire())
        self.assertFalse(Lease(Store({'run_id': 'old'}, uploaded=100), 'worker', 180).acquire())
        self.assertTrue(Lease(Store({'owner':'old', 'expires_at':999}), 'worker', 180).acquire())
        self.assertFalse(Lease(Store({'owner':'active', 'expires_at':1001}), 'worker', 180).acquire())

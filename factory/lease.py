"""Shared R2 leases: atomic acquisition, expiration, owner-checked release."""
import json
import time
import uuid
from botocore.exceptions import ClientError


class Lease:
    def __init__(self, store, kind, seconds):
        self.store, self.kind, self.seconds = store, kind, seconds
        self.owner = uuid.uuid4().hex
        self.etag = None

    def _read(self):
        try:
            r = self.store.client.get_object(Bucket=self.store.bucket, Key=self.store.prefix+'lock.json')
            return json.loads(r['Body'].read()), r['ETag'], r['LastModified'].timestamp()
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return None, None, None
            raise

    def _put(self, value, etag):
        try:
            return self.store.put('lock.json', json.dumps(value).encode(),
                                  **({'IfMatch': etag} if etag else {'IfNoneMatch': '*'}))['ETag']
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('PreconditionFailed', '412', 'ConditionalRequestConflict', '409'):
                return None
            raise

    def acquire(self):
        for _ in range(3):
            value, etag, uploaded = self._read()
            if value is not None:
                # Old Workers leases may expire after 3 minutes; old generators
                # remain protected for the full six-hour runner bound.
                expires = value.get('expires_at', uploaded + (180 if value.get('worker') else 21600))
                if not isinstance(expires, (int, float)) or expires > time.time():
                    return False
            current = time.time()
            self.etag = self._put(dict(owner=self.owner, kind=self.kind,
                                       created_at=current, expires_at=current+self.seconds), etag)
            if self.etag:
                return True
        return False

    def assert_owned(self):
        value, etag, _ = self._read()
        if not value or value.get('owner') != self.owner or value.get('expires_at', 0) <= time.time():
            raise RuntimeError('Production lease lost; stop before modifying state.')
        self.etag = etag

    def release(self):
        if not self.etag:
            return
        value, etag, _ = self._read()
        if value and value.get('owner') == self.owner:
            # A conditional tombstone avoids delete racing with a new owner.
            self._put(dict(owner=self.owner, kind=self.kind, expires_at=0, released=True), etag)

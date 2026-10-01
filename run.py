"""Cloud entry point: python run.py. Same command for GitHub and Railway."""
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path

from factory import core


class R2Store:
    def __init__(self):
        import boto3
        from botocore.config import Config
        required = ('GEMINI_API_KEY', 'R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID',
                    'R2_SECRET_ACCESS_KEY', 'R2_BUCKET')
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise RuntimeError('Добавь GitHub Secrets: ' + ', '.join(missing))
        account = os.environ['R2_ACCOUNT_ID']
        if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
            raise RuntimeError('R2_ACCOUNT_ID должен быть ID аккаунта из 32 символов, не URL.')
        self.bucket = os.environ['R2_BUCKET']
        self.prefix = f'content-factory/v1/{core.JOB_ID}/'
        self.client = boto3.client(
            's3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
            aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
            aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
            region_name='auto', config=Config(
                retries={'max_attempts': 2, 'mode': 'standard'},
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required',
            ),
        )

    def put(self, name, data, **kwargs):
        return self.client.put_object(Bucket=self.bucket, Key=self.prefix + name,
                                      Body=data, **kwargs)

    def restore(self, job):
        # Restore only known files. Paths from object listing cannot escape job.
        allowed = {'state.json', 'params.json', 'spec.json', 'final.mp4', 'subtitle.srt'}
        for i in range(1, 4):
            allowed.update({f'scene-{i:02}.png', f'scene-{i:02}-response.json'})
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for item in page.get('Contents', []):
                name = item['Key'][len(self.prefix):]
                if name in allowed:
                    self.client.download_file(self.bucket, item['Key'], str(job / name))

    def upload_file(self, path, name=None):
        self.client.upload_file(str(path), self.bucket, self.prefix + (name or path.name))


def main():
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', core.JOB_ID):
        raise RuntimeError('job_id: 1–64 латинских букв, цифр, дефисов или подчёркиваний.')
    store = R2Store()
    job = core.REPO / 'storage/content_factory' / core.JOB_ID
    job.mkdir(parents=True, exist_ok=True)
    # No automatic takeover: a killed runner may have an in-flight paid request.
    owner = {'id': str(uuid.uuid4()), 'run_id': os.environ.get('GITHUB_RUN_ID', 'local')}
    try:
        store.put('lock.json', json.dumps(owner).encode(), IfNoneMatch='*')
    except Exception:
        raise RuntimeError(
            'Не удалось создать блокировку R2. Проверь доступ и наличие lock.json. '
            'При оставшейся блокировке сначала убедись, что предыдущий запуск завершён. '
            'Платных запросов в этом запуске ещё не было.'
        ) from None
    original_atomic = core.atomic_bytes
    original_save = core.save_json

    def durable_atomic(path, data):
        original_atomic(path, data)
        path = Path(path)
        if path.parent == job:
            # pending reaches R2 BEFORE POST; response reaches R2 BEFORE ready.
            store.put(path.name, data)

    def durable_save(path, value):
        if Path(path).name == 'state.json' and value.get('status') == 'ready':
            # Publish ready only AFTER durable media upload.
            store.upload_file(job / 'final.mp4')
            store.upload_file(job / 'subtitle.srt')
        original_save(path, value)

    try:
        store.restore(job)
        core.atomic_bytes = durable_atomic
        core.save_json = durable_save
        core.main()
        print(f'Результат в R2: {store.prefix}final.mp4', flush=True)
        # Don't expose signed URLs, response JSON or API credentials in public logs.
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as output:
                output.write(f'Готово: `{store.prefix}final.mp4` — скачай через панель R2.\n')
    finally:
        core.atomic_bytes = original_atomic
        core.save_json = original_save
        # Private diagnostics; never upload these as public Actions artifacts.
        for name in ('preflight.log', 'render.log'):
            if (job / name).is_file():
                try:
                    store.upload_file(job / name)
                except Exception:
                    print('Не удалось сохранить журнал в R2.', flush=True)
        try:
            store.client.delete_object(Bucket=store.bucket, Key=store.prefix + 'lock.json')
        except Exception:
            print('Блокировка осталась в R2. Перед повтором проверь завершение этого запуска.')


if __name__ == '__main__':
    # A cancellation should terminate the render process group and run finally.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        main()
    except KeyboardInterrupt:
        print('Запуск остановлен. Уже сохранённые кадры остаются в R2.')
        sys.exit(130)
    except Exception as exc:
        print(f'ОСТАНОВЛЕНО: {type(exc).__name__}: {exc}', flush=True)
        sys.exit(1)

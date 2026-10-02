"""Deliver an existing video to the owner's configured private Telegram chat."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path
import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError


class Stop(Exception):
    pass


def deliver(store, bucket, prefix, token, chat_id):
    key = prefix + 'telegram-delivery.json'
    def save(value, **kwargs):
        store.put_object(Bucket=bucket, Key=key, Body=json.dumps(value).encode(), **kwargs)
    try:
        state = json.loads(store.get_object(Bucket=bucket, Key=key)['Body'].read())
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('404', 'NoSuchKey'):
            raise
        state = None
    if state:
        if state.get('chat_id') != chat_id or state.get('bot_id') != token.split(':')[0]:
            raise Stop('Saved delivery belongs to another recipient or bot. Check configuration.')
        if state.get('status') == 'sent':
            print('Already delivered; duplicate skipped.')
            return
        raise Stop('Previous delivery result is uncertain. Check your chat and telegram-delivery.json in R2 before retrying.')

    def api(method, **kwargs):
        response = requests.post(f'https://api.telegram.org/bot{token}/{method}',
            timeout=(20, 300), allow_redirects=False, **kwargs)
        body = response.json()
        if response.status_code != 200 or not body.get('ok'):
            code = body.get('error_code', response.status_code)
            code = code if isinstance(code, int) else 'unknown'
            raise Stop(f'Telegram error {code}. Check bot token, your chat ID and that you pressed Start. Check chat before retrying.')
        return body['result']

    chat = api('getChat', data={'chat_id': chat_id})
    if str(chat.get('id')) != chat_id or chat.get('type') != 'private':
        raise Stop('Recipient must be the configured private chat.')
    if chat_id == token.split(':')[0]:
        raise Stop('Use your personal chat ID, not the bot ID.')
    source = prefix + 'final.mp4'
    head = store.head_object(Bucket=bucket, Key=source)
    if not 0 < head.get('ContentLength', 0) <= 50_000_000:
        raise Stop('Video must be nonempty and at most 50 MB for this delivery workflow.')
    with tempfile.TemporaryDirectory() as directory:
        video = Path(directory) / 'final.mp4'
        store.download_file(bucket, source, str(video))
        if not 0 < video.stat().st_size <= 50_000_000:
            raise Stop('Downloaded video size is outside the supported range.')
        state = {'status': 'sending', 'chat_id': chat_id, 'bot_id': token.split(':')[0],
                 'source': source, 'run_id': os.environ.get('GITHUB_RUN_ID')}
        save(state, IfNoneMatch='*')
        caption = ('Иногда перед нами несколько путей. Что для тебя сейчас действительно важно?'
                   '\n\nИзображения созданы с помощью ИИ.')
        print('Sending existing video to your configured private chat.', flush=True)
        with video.open('rb') as media:
            result = api('sendVideo', data={'chat_id': chat_id, 'caption': caption},
                         files={'video': ('final.mp4', media, 'video/mp4')})
        if not result.get('message_id') or str(result.get('chat', {}).get('id')) != chat_id:
            raise Stop('Unexpected delivery response. Check the chat before retrying.')
        state.update(status='sent', message_id=result['message_id'])
        save(state)
    print('Video delivered to Telegram. No generation or social publication was performed.', flush=True)


def main():
    names = ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET',
             'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise Stop('Missing GitHub Secrets: ' + ', '.join(missing))
    job = os.environ.get('JOB_ID', 'pilot-001')
    account = os.environ['R2_ACCOUNT_ID'].strip()
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    chat = os.environ['TELEGRAM_CHAT_ID'].strip()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', job):
        raise Stop('Invalid JOB_ID.')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
        raise Stop('Invalid R2_ACCOUNT_ID.')
    if not re.fullmatch(r'[1-9][0-9]*', chat) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise Stop('Invalid Telegram token or personal chat ID.')
    store = boto3.client('s3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto', config=Config(retries={'max_attempts': 2, 'mode': 'standard'},
        request_checksum_calculation='when_required', response_checksum_validation='when_required'))
    deliver(store, os.environ['R2_BUCKET'], f'content-factory/v1/{job}/', token, chat)


if __name__ == '__main__':
    try:
        main()
    except Stop as exc:
        print(str(exc), flush=True)
        sys.exit(1)
    except Exception as exc:
        # requests exceptions include the bot token in the URL: never print them.
        print(f'Stopped ({type(exc).__name__}). Check Telegram and saved R2 state before retrying.', flush=True)
        sys.exit(1)

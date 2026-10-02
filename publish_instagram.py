"""Publish an existing R2 video through Instagram Login; no generation."""
import json
import os
import re
import sys
import time
import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError


class Stop(Exception):
    pass


def publish(client, bucket, prefix, user_id, token, sleep=time.sleep):
    key = prefix + 'instagram-publication.json'

    def save(state, **kwargs):
        client.put_object(Bucket=bucket, Key=key, Body=json.dumps(state).encode(), **kwargs)

    try:
        state = json.loads(client.get_object(Bucket=bucket, Key=key)['Body'].read())
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('NoSuchKey', '404'):
            raise
        state = None
    if state:
        if state.get('user_id') != user_id:
            raise Stop('Saved publication belongs to another account. Check INSTAGRAM_USER_ID.')
        if state.get('status') == 'published':
            print('Already published; duplicate skipped.')
            return
        raise Stop('Unfinished publication exists in R2. Check instagram-publication.json and Instagram before another attempt. Do not delete state blindly.')

    def api(method, path, **kwargs):
        response = requests.request(method, 'https://graph.instagram.com/v26.0/' + path,
            headers={'Authorization': 'Bearer ' + token}, timeout=(20, 90),
            allow_redirects=False, **kwargs)
        body = response.json()
        if not 200 <= response.status_code < 300 or 'error' in body:
            # Full diagnostic stays private. Never echo URLs, tokens or API messages.
            client.put_object(Bucket=bucket, Key=prefix + 'instagram-error.json',
                              Body=json.dumps(body).encode())
            code = body.get('error', {}).get('code')
            subcode = body.get('error', {}).get('error_subcode')
            safe_code = code if isinstance(code, int) else 'unknown'
            safe_subcode = subcode if isinstance(subcode, int) else 'none'
            raise Stop(f'Instagram HTTP {response.status_code}, code {safe_code}, subcode {safe_subcode}. Details: instagram-error.json in private R2.')
        return body

    # Read-only preflight before claiming the publication.
    api('GET', user_id, params={'fields': 'id,username'})
    head = client.head_object(Bucket=bucket, Key=prefix + 'final.mp4')
    if not head.get('ContentLength'):
        raise Stop('Source video is empty.')
    url = client.generate_presigned_url('get_object', Params={
        'Bucket': bucket, 'Key': prefix + 'final.mp4',
        'ResponseContentType': 'video/mp4'}, ExpiresIn=3600)
    state = {'status': 'creating', 'user_id': user_id,
             'run_id': os.environ.get('GITHUB_RUN_ID'), 'source': prefix + 'final.mp4'}
    # Conditional claim is durable BEFORE any Instagram write; no automatic POST retry.
    save(state, IfNoneMatch='*')
    print('Creating Reels container.', flush=True)
    result = api('POST', user_id + '/media', data={
        'media_type': 'REELS', 'video_url': url, 'share_to_feed': 'true',
        'caption': 'Иногда перед нами несколько путей. Что для тебя сейчас действительно важно?\n\nТест Content Factory. Изображения созданы с помощью ИИ.'})
    container = str(result.get('id', ''))
    if not re.fullmatch(r'\d+', container):
        raise Stop('No valid container ID returned. Inspect publication state.')
    state.update(status='processing', container_id=container)
    save(state)
    for attempt in range(5):
        sleep(60)
        status = api('GET', container, params={'fields': 'status_code,status'})
        state['container_status'] = status.get('status_code')
        save(state)
        if status.get('status_code') == 'FINISHED':
            break
        if status.get('status_code') in ('ERROR', 'EXPIRED'):
            client.put_object(Bucket=bucket, Key=prefix + 'instagram-error.json', Body=json.dumps(status).encode())
            raise Stop('Instagram could not process video. See instagram-error.json in R2.')
        if status.get('status_code') != 'IN_PROGRESS':
            raise Stop('Unexpected container status; inspect R2 state before retrying.')
        print(f'Instagram processing video ({attempt + 1}/5).', flush=True)
    else:
        raise Stop('Processing exceeded five minutes. Container ID saved in R2; inspect before retrying.')
    state['status'] = 'publishing'
    save(state)
    print('Publishing Reel.', flush=True)
    result = api('POST', user_id + '/media_publish', data={'creation_id': container})
    media_id = str(result.get('id', ''))
    if not re.fullmatch(r'\d+', media_id):
        raise Stop('Publication result unknown. Check Instagram before retrying.')
    state.update(status='published', media_id=media_id)
    save(state)
    print('Reel published. Open your Instagram profile. Result saved in private R2.', flush=True)


def main():
    names = ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET',
             'INSTAGRAM_USER_ID', 'INSTAGRAM_ACCESS_TOKEN']
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise Stop('Missing GitHub Secrets: ' + ', '.join(missing))
    job = os.environ.get('JOB_ID', 'pilot-001')
    account = os.environ['R2_ACCOUNT_ID'].strip()
    user_id = os.environ['INSTAGRAM_USER_ID'].strip()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', job):
        raise Stop('Invalid JOB_ID.')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', account) or not re.fullmatch(r'\d+', user_id):
        raise Stop('Invalid R2_ACCOUNT_ID or INSTAGRAM_USER_ID.')
    client = boto3.client('s3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto', config=Config(retries={'max_attempts': 2, 'mode': 'standard'},
        signature_version='s3v4', request_checksum_calculation='when_required', response_checksum_validation='when_required'))
    publish(client, os.environ['R2_BUCKET'], f'content-factory/v1/{job}/',
            user_id, os.environ['INSTAGRAM_ACCESS_TOKEN'].strip())


if __name__ == '__main__':
    try:
        main()
    except Stop as exc:
        print(str(exc), flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f'Stopped ({type(exc).__name__}). Check R2 publication state before retrying.', flush=True)
        sys.exit(1)

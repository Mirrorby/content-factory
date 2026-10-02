"""Upload an existing R2 video privately, with a durable duplicate guard."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError


class Stop(Exception):
    pass


def checked(response):
    if not 200 <= response.status_code < 300:
        raise Stop(f'YouTube HTTP {response.status_code}. Check OAuth access and API settings; state remains in R2.')
    return response


def main():
    required = ['R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'YOUTUBE_OAUTH_JSON']
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise Stop('Missing GitHub Secrets: ' + ', '.join(missing))
    job = os.environ.get('JOB_ID', 'pilot-001')
    account = os.environ['R2_ACCOUNT_ID']
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', job) or not re.fullmatch(r'[a-fA-F0-9]{32}', account):
        raise Stop('Invalid JOB_ID or R2_ACCOUNT_ID')
    bucket = os.environ['R2_BUCKET']
    prefix = f'content-factory/v1/{job}/'
    key = prefix + 'youtube-publication.json'
    client = boto3.client('s3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto', config=Config(retries={'max_attempts': 2, 'mode': 'standard'},
        request_checksum_calculation='when_required', response_checksum_validation='when_required'))

    def save(state, **kwargs):
        client.put_object(Bucket=bucket, Key=key, Body=json.dumps(state).encode(), **kwargs)

    try:
        state = json.loads(client.get_object(Bucket=bucket, Key=key)['Body'].read())
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('NoSuchKey', '404'):
            raise
        state = None
    if state:
        if state.get('status') == 'uploaded':
            print('Already uploaded. Skipping duplicate. See youtube-publication.json in R2.')
            return
        raise Stop('Previous upload is unfinished or uncertain. Check youtube-publication.json in R2 and YouTube Studio before retrying. Do not delete state blindly.')

    secret = json.loads(os.environ['YOUTUBE_OAUTH_JSON'])
    token = checked(requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': secret['client_id'], 'client_secret': secret['client_secret'],
        'refresh_token': secret['refresh_token'], 'grant_type': 'refresh_token'}, timeout=60)).json()['access_token']
    headers = {'Authorization': 'Bearer ' + token}
    with tempfile.TemporaryDirectory() as directory:
        video = Path(directory) / 'final.mp4'
        client.download_file(bucket, prefix + 'final.mp4', str(video))
        size = video.stat().st_size
        if not size:
            raise Stop('R2 video is empty')
        # Persist before any YouTube side effect; conditional write prevents races.
        state = {'status': 'pending', 'run_id': os.environ.get('GITHUB_RUN_ID'), 'source': prefix + 'final.mp4'}
        save(state, IfNoneMatch='*')
        metadata = {
            'snippet': {'title': 'Первый шаг | Тест Content Factory',
                        'description': 'Тест автоматической сборки видео. Изображения созданы с помощью ИИ. #Shorts',
                        'categoryId': '22'},
            'status': {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': True}}
        response = checked(requests.post('https://www.googleapis.com/upload/youtube/v3/videos',
            params={'uploadType': 'resumable', 'part': 'snippet,status', 'notifySubscribers': 'false'},
            headers={**headers, 'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(size)},
            json=metadata, timeout=60, allow_redirects=False))
        session = response.headers['Location']
        parsed = urlparse(session)
        if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com':
            raise Stop('Unexpected upload URL')
        state.update(status='uploading', session_url=session)
        save(state)  # Keep private; never print session URL in public Actions logs.
        with video.open('rb') as media:
            result = checked(requests.put(session, headers={**headers, 'Content-Type': 'video/mp4',
                'Content-Length': str(size)}, data=media, timeout=(30, 600), allow_redirects=False)).json()
        if not result.get('id'):
            raise Stop('No video ID returned; inspect the saved upload session before retrying.')
        state.update(status='uploaded', video_id=result['id'], privacy=result.get('status', {}).get('privacyStatus', 'private'))
        state.pop('session_url', None)
        save(state)
    print('Uploaded privately. Open YouTube Studio; details saved in R2 youtube-publication.json.')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as output:
            output.write('YouTube upload completed (private). Open YouTube Studio. Details are in R2.\n')


if __name__ == '__main__':
    try:
        main()
    except Stop as exc:
        print(str(exc))
        sys.exit(1)
    except Exception as exc:
        # Request exceptions can contain a private upload URL or credentials.
        print(f'Upload stopped ({type(exc).__name__}). No automatic retry; inspect saved state in R2.')
        sys.exit(1)

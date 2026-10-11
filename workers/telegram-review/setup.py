"""Temporary private secret transfer; never print config or provider response bodies."""
import json
import os
import secrets
import sys
import requests
from run import R2Store

KEY = 'content-factory/production/v2/review-worker-bootstrap.json'
URL = 'https://leela-video-review.nikita-karpof.workers.dev'

def main():
    store = R2Store(require_gemini=False)
    if sys.argv[1] == 'bootstrap':
        mapping = json.loads(store.client.get_object(Bucket=store.bucket,
            Key='content-factory/production/v1/buffer-discovery-maya.json')['Body'].read())['mapping']
        c = dict(token=os.environ['TELEGRAM_BOT_TOKEN'],chat=os.environ['TELEGRAM_CHAT_ID'],
                 buffer_key=os.environ['BUFFER_MAYA_API_KEY'],organization_id=mapping['organization_id'],
                 channels=mapping['channels'],media_base=os.environ['BUFFER_MEDIA_BASE_URL'].rstrip('/'),
                 webhook_secret=secrets.token_urlsafe(32))
        store.client.put_object(Bucket=store.bucket,Key=KEY,Body=json.dumps(c).encode(),IfNoneMatch='*')
        print('Private one-use worker configuration staged.')
    else:
        c = json.loads(store.client.get_object(Bucket=store.bucket,Key=KEY)['Body'].read())
        r = requests.get(URL+'/health',timeout=20)
        if r.status_code != 200 or r.json() != {'ok':True}: raise RuntimeError('Worker unavailable')
        store.client.put_object(Bucket=store.bucket,Key='content-factory/production/v2/telegram-webhook.json',
            Body=json.dumps({'url':URL+'/telegram'}).encode())
        r = requests.post('https://api.telegram.org/bot'+c['token']+'/setWebhook',json={
            'url':URL+'/telegram','secret_token':c['webhook_secret'],'allowed_updates':['callback_query'],
            'drop_pending_updates':False,'max_connections':1},timeout=30)
        if r.status_code != 200 or not r.json().get('ok'): raise RuntimeError('Webhook rejected')
        r = requests.post('https://api.telegram.org/bot'+c['token']+'/getWebhookInfo',json={},timeout=30)
        info = r.json().get('result',{})
        if info.get('url') != URL+'/telegram': raise RuntimeError('Webhook not active')
        # Authenticated drain also exercises bindings and secret injection.
        r = requests.post(URL+'/drain',headers={'X-Telegram-Bot-Api-Secret-Token':c['webhook_secret']},timeout=180)
        if r.status_code != 200: raise RuntimeError('Worker drain failed')
        store.client.delete_object(Bucket=store.bucket,Key=KEY)
        print('Webhook active; authenticated drain passed; temporary configuration removed.')

if __name__ == '__main__':
    try: main()
    except Exception:
        print('Worker setup stopped; no secret details logged.')
        sys.exit(1)

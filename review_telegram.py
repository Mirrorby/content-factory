"""Poll owner's review buttons; publish only the exact approved video, never generate."""
import json
import os
import re
from datetime import timedelta
import requests
from botocore.exceptions import ClientError
from production import Ledger, load_config, load_private_channels, deliver, now
from factory.buffer_api import Buffer


def read_json(store, key):
    try:
        return json.loads(store.client.get_object(Bucket=store.bucket, Key=key)['Body'].read())
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('404', 'NoSuchKey'):
            return None
        raise


class Telegram:
    def __init__(self):
        self.token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
        self.chat = os.environ['TELEGRAM_CHAT_ID'].strip()

    def call(self, method, **data):
        try:
            response = requests.post('https://api.telegram.org/bot'+self.token+'/'+method,
                json=data, timeout=(15, 40), allow_redirects=False)
            result = response.json()
            if response.status_code != 200 or not result.get('ok'):
                raise RuntimeError('Telegram API rejected '+method)
            return result['result']
        except requests.RequestException:
            raise RuntimeError('Telegram request failed.') from None

    def acknowledge(self, query_id):
        try:
            self.call('answerCallbackQuery', callback_query_id=query_id, text='Решение обработано')
        except RuntimeError:
            # Telegram's short-lived callback notification may expire before cron runs.
            pass

    def label(self, message_id, job_id, text):
        self.call('editMessageReplyMarkup', chat_id=self.chat, message_id=message_id,
            reply_markup={'inline_keyboard': [[{'text':text, 'callback_data':'cf:done:'+job_id}]]})


def authorize_callback(query, chat_id, bot_id, state, read_receipt):
    """Require owner + original private message + confirmed receipt + current video hash."""
    match = re.fullmatch(r'cf:([ar]):([A-Za-z0-9][A-Za-z0-9_-]{0,54})', query.get('data',''))
    message = query.get('message', {})
    if (not match or str(query.get('from',{}).get('id')) != chat_id or
        str(message.get('chat',{}).get('id')) != chat_id or
        message.get('chat',{}).get('type') != 'private'):
        return None
    action, job_id = match.groups()
    matches = [(c, j) for c in state['characters'].values() for j in c['jobs'] if j['id']==job_id]
    if len(matches) != 1:
        return None
    character, job = matches[0]
    receipt = read_receipt(job_id)
    if (not receipt or receipt.get('status') != 'sent' or receipt.get('chat_id') != chat_id or
        receipt.get('bot_id') != bot_id or receipt.get('message_id') != message.get('message_id') or
        not job.get('sha256') or receipt.get('video_sha256') != job['sha256']):
        return None
    if job['status'] not in ('ready', 'rejected'):
        return None
    return job, receipt, action


def decide(job, receipt, action):
    # First valid decision wins. A repeat press cannot republish or reverse a decision.
    if job.get('review',{}).get('decision'):
        return False
    job['telegram_review'] = {'sha256':job['sha256'], 'sent_at':now().isoformat()}
    job['review'] = dict(decision='approved' if action=='a' else 'rejected',
        decided_at=now().isoformat(), message_id=receipt['message_id'])
    if action == 'a':
        job['review']['approved_sha256'] = job['sha256']
    else:
        job['status'] = 'rejected'
        for delivery in job['deliveries'].values():
            if delivery['status'] == 'waiting':
                delivery['status'] = 'rejected'
    return True


def status_label(job):
    if job['review']['decision'] == 'rejected':
        return '❌ Отклонено — не публикуется'
    statuses = [d['status'] for d in job['deliveries'].values()]
    if all(s in ('scheduled','sent') for s in statuses):
        return '✅ Отправлено в Buffer'
    if any(s in ('unknown','error') for s in statuses):
        return '⚠️ Нужна проверка отправки'
    if any(s == 'expired' for s in statuses):
        return '⚠️ Срок публикации прошёл — нужен перенос'
    if any(s == 'slot_conflict' for s in statuses):
        return '⚠️ Слот занят — нужна проверка'
    return '✅ Одобрено — ожидает отправки'


def main():
    tg = Telegram()
    if tg.call('getWebhookInfo').get('url'):
        raise RuntimeError('Bot has an active webhook; polling was not started.')
    ledger = Ledger()
    store = ledger.store
    try:
        store.put('lock.json', b'{}', IfNoneMatch='*')
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('PreconditionFailed','412'):
            print('Review postponed: production lock is held.')
            return
        raise
    try:
        state = ledger.read()
        save = lambda: ledger.save(state)
        cursor = read_json(store, store.prefix+'telegram-updates.json') or {'offset':0}
        updates = tg.call('getUpdates', offset=cursor['offset'], timeout=0,
                          allowed_updates=['callback_query'], limit=100)
        for update in updates:
            query = update.get('callback_query', {})
            checked = authorize_callback(query, tg.chat, tg.token.split(':')[0], state,
                lambda job_id: read_json(store, 'content-factory/v1/'+job_id+'/telegram-delivery.json'))
            if checked:
                job, receipt, action = checked
                if decide(job, receipt, action):
                    save()  # decision reaches R2 before offset advances or Buffer receives anything
            if query and str(query.get('from',{}).get('id')) == tg.chat:
                tg.acknowledge(query['id'])
            cursor['offset'] = update['update_id'] + 1
            store.put('telegram-updates.json', json.dumps(cursor).encode())
        config, _ = load_config()
        characters = [c for c in config['characters'] if c['enabled']]
        candidates = [j for c in state['characters'].values() for j in c['jobs']
                      if j.get('review',{}).get('decision')]
        needs_routing = any(j['review']['decision']=='approved' and
            any(d['status']=='waiting' for d in j['deliveries'].values()) for j in candidates)
        if needs_routing:
            load_private_channels(ledger, characters)
        for character in characters:
            char_state = state['characters'].get(character['id'], {'jobs':[]})
            api = None
            for job in char_state['jobs']:
                review = job.get('review',{})
                if not review.get('decision'):
                    continue
                eligible = (review['decision']=='approved' and job['status']=='ready' and
                    review.get('approved_sha256')==job.get('sha256') and
                    all(d['status'] not in ('unknown','error') for d in job['deliveries'].values()) and
                    any(d['status']=='waiting' for d in job['deliveries'].values()) and
                    review.get('next_delivery_check_at','') <= now().isoformat())
                if eligible:
                    # A full queue is retried every six hours, avoiding Free API rate exhaustion.
                    review['next_delivery_check_at'] = (now()+timedelta(hours=6)).isoformat()
                    save()
                    try:
                        if char_state.get('channels') != character['channels']:
                            raise RuntimeError('Channel mapping changed since generation.')
                        if not api:
                            api = Buffer(os.environ[character['buffer_secret']], character['organization_id'])
                            api.preflight(character, config)
                        deliver(api, ledger, config, character, {'jobs':[job]}, save)
                        review.pop('delivery_error',None)
                    except Exception as exc:
                        review['delivery_error'] = type(exc).__name__
                        print('Approved delivery paused; private state preserved.')
                    save()
                label = status_label(job)
                if review.get('delivery_error'):
                    label = '⚠️ Одобрено — ошибка отправки'
                if review.get('displayed_label') != label:
                    try:
                        tg.label(review['message_id'], job['id'], label)
                        review['displayed_label'] = label
                        save()
                    except RuntimeError:
                        print('Review label update postponed.')
        print('Review check completed. No video generation performed.')
    finally:
        store.client.delete_object(Bucket=store.bucket, Key=store.prefix+'lock.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Review stopped: '+type(exc).__name__)
        raise SystemExit(1)


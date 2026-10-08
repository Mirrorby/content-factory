"""Durable generation + Buffer queue refill. Default: offline preview, no side effects."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PLATFORMS = ('youtube', 'tiktok', 'instagram')


def now():
    return datetime.now(timezone.utc)


def needed(queue_counts, jobs, target=10):
    """Generate once for all networks, counting already planned/ready backlog first."""
    return max([0] + [target - queue_counts[p] - sum(
        j['deliveries'].get(p, {}).get('status', 'waiting') == 'waiting'
        for j in jobs) for p in PLATFORMS])


def due(last, current, hours=96):
    return not last or current >= datetime.fromisoformat(last) + timedelta(hours=hours)


def select_phrase(phrases, theme, usage):
    candidates = [p for p in phrases if p['theme'] == theme]
    return min(candidates, key=lambda p: (usage.get(p['id'], 0), p['id']))


def load_config():
    config = json.loads((ROOT / 'config/production.json').read_text())
    phrases = json.loads((ROOT / 'config/phrases-en.json').read_text())
    if config['caption'] != 'Find on Telegram: @leela_ru_bot':
        raise RuntimeError('Unexpected production caption.')
    if not 1 <= config['queue_target'] <= 10 or not 1 <= config['max_new_per_cycle'] <= 10:
        raise RuntimeError('Queue and batch caps must be 1..10.')
    if len({p['id'] for p in phrases}) != len(phrases):
        raise RuntimeError('Duplicate phrase IDs.')
    if len({c['id'] for c in config['characters']}) != len(config['characters']):
        raise RuntimeError('Duplicate character IDs.')
    for c in config['characters']:
        if not re.fullmatch('[a-z][a-z0-9-]{0,15}', c['id']):
            raise RuntimeError('Invalid character ID.')
        select_phrase(phrases, c['theme'], {})
    return config, phrases


class Ledger:
    def __init__(self):
        from run import R2Store
        self.store = R2Store(require_gemini=False)
        self.store.prefix = 'content-factory/production/v1/'

    def read(self):
        from render_gameplay import read_remote
        return read_remote(self.store, 'ledger.json') or {'version': 1, 'characters': {}}

    def save(self, state):
        self.store.put('ledger.json', json.dumps(state, sort_keys=True).encode())


def reconcile(api, character, state, save):
    remote = api.queue(list(character['channels'].values()))
    ids = {p['id']: p for p in remote}
    counts = {p: sum(item['channelId'] == character['channels'][p] for item in remote)
              for p in PLATFORMS}
    for job in state['jobs']:
        for platform, delivery in job['deliveries'].items():
            if delivery['status'] == 'unknown':
                raise RuntimeError('Unconfirmed Buffer submission; inspect saved job before resuming.')
            if delivery['status'] == 'error':
                raise RuntimeError('Buffer delivery failed; resolve the existing post in Buffer first.')
            if delivery['status'] != 'scheduled':
                continue
            post = ids.get(delivery['post_id']) or api.post(delivery['post_id'])
            if not post or post['channelId'] != character['channels'][platform]:
                raise RuntimeError('Saved Buffer post missing or channel changed; inspect before resuming.')
            if post['status'] == 'sent':
                delivery.update(status='sent', confirmed_at=now().isoformat())
                save()
            elif post['status'] not in ('scheduled', 'sending'):
                # Keep the existing receipt. On the next run re-read it after manual repair.
                raise RuntimeError('Existing post needs attention in Buffer: ' + post['status'])
    return counts


def plan(config, phrases, character, state, counts, current):
    if not due(state.get('last_cycle_at'), current, config['cycle_hours']):
        return 0
    count = min(config['max_new_per_cycle'], needed(counts, state['jobs'], config['queue_target']))
    # A stopped platform must not accumulate an unbounded backlog while others publish.
    backlog = max(sum(j['deliveries'][p]['status'] == 'waiting' for j in state['jobs'])
                  for p in PLATFORMS)
    count = min(count, max(0, config['queue_target'] - backlog))
    for _ in range(count):
        phrase = select_phrase(phrases, character['theme'], state['usage'])
        sequence = state.get('sequence', 0) + 1
        state['sequence'] = sequence
        job_id = f"prod-{character['id']}-{sequence:06d}"
        spec = dict(character={k: character[k] for k in ('id', 'appearance', 'voice')},
                    phrase=phrase, source=config['source'], speed=config['speed'], actor_asset=None)
        state['jobs'].append(dict(id=job_id, status='planned', spec=spec,
            created_at=current.isoformat(), deliveries={p: {'status': 'waiting'} for p in PLATFORMS}))
        state['usage'][phrase['id']] = state['usage'].get(phrase['id'], 0) + 1
    # Anchor to the UTC calendar day; a runner starting a minute earlier four
    # days later must not postpone the entire batch for another day.
    state['last_cycle_at'] = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return count


def render(ledger, state, job, save):
    store = ledger.store
    if job['status'] == 'planned':
        job['spec']['actor_asset'] = state.get('actor_asset')
        job['status'] = 'rendering'
        save()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'spec.json'
        path.write_text(json.dumps(job['spec']))
        subprocess.run([sys.executable, str(ROOT / 'render_thoughts.py')], cwd=ROOT,
            env={**os.environ, 'JOB_ID': job['id'], 'VIDEO_SPEC_PATH': str(path)},
            check=True, timeout=3600)
    prefix = f"content-factory/v1/{job['id']}/"
    result = json.loads(store.client.get_object(Bucket=store.bucket, Key=prefix+'state.json')['Body'].read())
    if result['status'] != 'ready':
        raise RuntimeError('Renderer did not save a ready result.')
    if not state.get('actor_asset'):
        state['actor_asset'] = {'key': prefix+'intro.mp4', 'sha256': result['clips'][0]['sha256']}
    job.update(status='ready', video_key=prefix+'final.mp4', sha256=result['video_sha256'])
    save()


def expose_video(ledger, config, job, save):
    """Only final videos copied to a SEPARATE public media bucket. No expiring URLs."""
    import requests
    store = ledger.store
    bucket = os.environ[config['public_bucket_env']]
    base = os.environ[config['public_base_url_env']].rstrip('/')
    if not job.get('public_key'):
        job['public_key'] = 'videos/' + uuid.uuid4().hex + '.mp4'
        save()
    if not job.get('media_ready'):
        store.client.copy_object(Bucket=bucket, Key=job['public_key'],
            CopySource={'Bucket': store.bucket, 'Key': job['video_key']},
            ContentType='video/mp4', MetadataDirective='REPLACE',
            Metadata={'sha256': job['sha256']}, CacheControl='public, max-age=31536000, immutable')
        # Check actual public access without downloading the movie or logging its URL.
        try:
            response = requests.head(base+'/'+job['public_key'], timeout=30, allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('Public video availability check failed.') from None
        if response.status_code != 200 or 'video/mp4' not in response.headers.get('Content-Type', ''):
            raise RuntimeError('Buffer media URL is not accessible as video/mp4.')
        job['media_ready'] = True
        save()
    return base+'/'+job['public_key']


def deliver(api, ledger, config, character, state, save):
    for job in state['jobs']:
        if job['status'] != 'ready':
            continue
        for platform in PLATFORMS:
            delivery = job['deliveries'][platform]
            if delivery['status'] != 'waiting':
                continue
            channel = character['channels'][platform]
            remote = api.queue([channel])  # refresh capacity immediately before each mutation
            if len(remote) >= config['queue_target']:
                continue
            url = expose_video(ledger, config, job, save)
            delivery.update(status='unknown', attempted_at=now().isoformat(), channel_id=channel)
            save()  # crash/timeout after POST cannot trigger a blind duplicate on retry
            receipt = api.create(channel, platform, url, job['spec']['phrase']['hook'], config['caption'])
            delivery.update(status='scheduled', post_id=receipt['id'], due_at=receipt.get('dueAt'))
            save()


def validate_live(config, characters):
    from zoneinfo import ZoneInfo
    if not config.get('posting_timezone'):
        raise RuntimeError('Set posting_timezone and the same morning/evening schedule in Buffer.')
    ZoneInfo(config['posting_timezone'])
    required = [config['public_bucket_env'], config['public_base_url_env'], 'GEMINI_API_KEY']
    for c in characters:
        required.append(c['buffer_secret'])
        if not c['organization_id'] or set(c['channels']) != set(PLATFORMS) or not all(c['channels'].values()):
            raise RuntimeError('Configure Buffer organization and three channel IDs for ' + c['id'])
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError('Missing settings: ' + ', '.join(missing))
    if os.environ[config['public_bucket_env']] == os.environ.get('R2_BUCKET'):
        raise RuntimeError('Use a separate bucket for public final videos.')
    url = urlparse(os.environ[config['public_base_url_env']])
    if url.scheme != 'https' or not url.netloc or url.query or url.fragment or url.username:
        raise RuntimeError('Media base URL must be a permanent HTTPS origin/path.')
    channel_ids = [ch for c in characters for ch in c['channels'].values()]
    if len(channel_ids) != len(set(channel_ids)):
        raise RuntimeError('Each character must have its own three channels.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config, phrases = load_config()
    if not args.execute:
        preview = {'mode': 'preview', 'cycle_hours': config['cycle_hours'],
                   'first_batch_per_character': config['queue_target'],
                   'normal_refill_after_four_days': 8, 'phrases': len(phrases),
                   'characters': [{k:c[k] for k in ('id', 'name', 'theme', 'enabled')}
                                  for c in config['characters']]}
        print(json.dumps(preview, indent=2))
        return
    characters = [c for c in config['characters'] if c['enabled']]
    if not characters:
        print('No enabled characters. No generation or publication performed.')
        return
    validate_live(config, characters)
    from factory.buffer_api import Buffer
    ledger = Ledger()
    ledger.store.put('lock.json', json.dumps({'run_id': os.environ.get('GITHUB_RUN_ID')}).encode(),
                     IfNoneMatch='*')
    try:
        all_state = ledger.read()
        save = lambda: ledger.save(all_state)
        failures = []
        for character in characters:
            try:
                state = all_state['characters'].setdefault(character['id'], {'jobs': [], 'usage': {}})
                if state.get('channels', character['channels']) != character['channels']:
                    raise RuntimeError('Channel mapping changed; migrate saved deliveries first.')
                state['channels'] = character['channels'].copy()
                api = Buffer(os.environ[character['buffer_secret']], character['organization_id'])
                api.preflight(character, config)
                counts = reconcile(api, character, state, save)
                new_count = plan(config, phrases, character, state, counts, now())
                save()  # reserve job IDs, phrase usage and immutable specs before rendering
                print(character['id'] + ': planned ' + str(new_count) + ' new videos.', flush=True)
                for job in state['jobs']:
                    if job['status'] in ('planned', 'rendering'):
                        render(ledger, state, job, save)
                deliver(api, ledger, config, character, state, save)
                print(character['id'] + ': generation and queue refill completed.', flush=True)
            except Exception as exc:
                # No exception strings: provider errors may contain URLs or credentials.
                reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                print(character['id'] + ': stopped: ' + reason + ' Existing state preserved.', flush=True)
                failures.append(character['id'])
        if failures:
            raise RuntimeError('Characters requiring inspection: ' + ', '.join(failures))
    finally:
        ledger.store.client.delete_object(Bucket=ledger.store.bucket, Key=ledger.store.prefix+'lock.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Production stopped: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        sys.exit(1)

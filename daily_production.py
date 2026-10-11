"""Two new videos for tomorrow in the owner's timezone, with private review first."""
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import json
import os
from production import (Ledger, PLATFORMS, load_private_channels, validate_live,
                        select_phrase, render, send_for_review, now)
from factory.buffer_api import Buffer
from factory.actor_scenes import select_scene


def tomorrow(current, config):
    return current.astimezone(ZoneInfo(config.get('planning_timezone', 'Europe/Minsk'))).date() + timedelta(days=1)


def slots(day, channel):
    weekday = ('mon','tue','wed','thu','fri','sat','sun')[day.weekday()]
    schedule = next(d for d in channel['postingSchedule'] if d['day'] == weekday)
    if schedule['paused'] or len(set(schedule['times'])) != 2:
        raise RuntimeError('Tomorrow requires two distinct Buffer slots.')
    result = []
    zone = ZoneInfo(channel['timezone'])
    for value in schedule['times']:
        local = datetime.combine(day, time.fromisoformat(value), zone)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise RuntimeError('Posting slot falls in a daylight-saving gap.')
        result.append(utc.isoformat().replace('+00:00','Z'))
    return sorted(result)


def plan_daily(config, phrases, character, state, channels, current):
    target = tomorrow(current, config)
    reserved = {p: slots(target, channels[ch]) for p, ch in character['channels'].items()}
    existing = [j for j in state['jobs'] if j.get('target_date') == target.isoformat()]
    if existing:
        if len(existing) != 2:
            raise RuntimeError('Incomplete daily reservation; inspect before generating.')
        return existing, 0
    jobs = []
    for index in range(2):
        phrase = select_phrase(phrases, character['theme'], state['usage'])
        state['usage'][phrase['id']] = state['usage'].get(phrase['id'], 0) + 1
        spec = dict(character={k: character[k] for k in
                    ('id','appearance','voice','reference_image_key')},
                    phrase=phrase, source=config['source'], speed=config['speed'], actor_asset=None)
        scene = select_scene(character, state)
        if scene:
            spec['scene'] = scene
        job = dict(id=f"daily-{character['id']}-{target:%Y%m%d}-{index+1:02d}",
                   status='planned', target_date=target.isoformat(), spec=spec,
                   created_at=current.isoformat(), deliveries={p: dict(status='waiting',
                   target_due_at=reserved[p][index]) for p in PLATFORMS})
        jobs.append(job)
    state['jobs'].extend(jobs)
    return jobs, 2


def check_assets(ledger, config, character):
    from botocore.exceptions import ClientError
    store = ledger.store
    asset = 'Maya reference PNG'
    try:
        response = store.client.get_object(Bucket=store.bucket,
            Key=character['reference_image_key'], Range='bytes=0-7')
        if response['Body'].read() != b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('Maya reference must be a valid PNG.')
        print('Maya reference PNG is available.', flush=True)
        asset = 'gameplay video'
        store.client.head_object(Bucket=store.bucket, Key=config['source'])
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('404','NoSuchKey','NotFound'):
            raise RuntimeError('Required private R2 asset missing: ' + asset + '. No generation started.') from None
        raise
    print('Private reference PNG and gameplay are available.', flush=True)


def recover_audio_failure(ledger, state, job, save):
    """One replacement only for Google's explicit uncharged audio failure."""
    if job.get('audio_recovery') or job['status'] != 'rendering':
        return None
    if any(d['status'] != 'waiting' for d in job['deliveries'].values()):
        return None
    from review_telegram import read_json
    prefix = 'content-factory/v1/' + job['id'] + '/'
    renderer = read_json(ledger.store, prefix + 'state.json') or {}
    response = read_json(ledger.store, prefix + 'intro.mp4.response.json') or {}
    if not retryable_audio_failure(renderer, response):
        return None
    replacement = json.loads(json.dumps(job))
    replacement.update(id=job['id']+'-a2', status='planned', audio_recovery=True,
                       replaces=job['id'])
    replacement.pop('generation_error', None)
    replacement['spec']['audio_recovery'] = True
    replacement['spec']['actor_asset'] = None
    job['status'] = 'generation_failed'
    state.setdefault('archive', []).append(job)
    state['jobs'][state['jobs'].index(job)] = replacement
    save()
    return replacement


def retryable_audio_failure(renderer, response):
    clips = renderer.get('clips', [])
    generated = response.get('response', {}).get('generateVideoResponse', {})
    reasons = generated.get('raiMediaFilteredReasons', [])
    return (len(clips) == 1 and clips[0].get('status') == 'blocked_or_empty'
            and response.get('done') is True and not response.get('error')
            and not generated.get('generatedSamples') and len(reasons) == 1
            and reasons[0].startswith('We encountered an issue with the audio for your prompt')
            and 'You have not been charged for this attempt.' in reasons[0])


def execute(config, phrases):
    characters = [c for c in config['characters'] if c['enabled']]
    ledger = Ledger()
    load_private_channels(ledger, characters)
    validate_live(config, characters)
    ledger.store.put('lock.json', json.dumps({'run_id': os.environ.get('GITHUB_RUN_ID')}).encode(), IfNoneMatch='*')
    try:
        all_state = ledger.read()
        save = lambda: ledger.save(all_state)
        for character in characters:
            check_assets(ledger, config, character)  # before any paid generation
            api = Buffer(os.environ[character['buffer_secret']], character['organization_id'])
            channels = api.preflight(character, config)
            state = all_state['characters'].setdefault(character['id'], {'jobs':[], 'usage':{}})
            if state.get('channels', character['channels']) != character['channels']:
                raise RuntimeError('Channel mapping changed; inspect saved deliveries.')
            state['channels'] = character['channels'].copy()
            jobs, count = plan_daily(config, phrases, character, state, channels, now())
            save()
            print(f"Reserved {count} new videos for {jobs[0]['target_date']} ({config['posting_timezone']}).", flush=True)
            failures = []
            for job in jobs:
                try:
                    if job['status'] in ('planned','rendering'):
                        # A separate actor performance for each video; no cross-job clip cache.
                        render(ledger, {}, job, save)
                    elif job['status'] == 'ready':
                        send_for_review(job, save)
                except Exception as exc:
                    replacement = recover_audio_failure(ledger, state, job, save)
                    if replacement:
                        print('Confirmed uncharged audio failure: trying one revised audio request.', flush=True)
                        try:
                            render(ledger, {}, replacement, save)
                            continue
                        except Exception as recovery_exc:
                            job = replacement
                            exc = recovery_exc
                    failures.append(job['id'])
                    job['generation_error'] = type(exc).__name__
                    save()
                    print('One daily video paused; continuing independent jobs.', flush=True)
            if failures:
                raise RuntimeError('Daily generation incomplete; saved failed jobs were not replaced or retried.')
            print('Daily pair delivered for Telegram review. Buffer requires approval.', flush=True)
    finally:
        ledger.store.client.delete_object(Bucket=ledger.store.bucket, Key=ledger.store.prefix+'lock.json')

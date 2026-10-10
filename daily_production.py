"""Two new videos for tomorrow in the owner's timezone, with private review first."""
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import json
import os
from production import (Ledger, PLATFORMS, load_private_channels, validate_live,
                        select_phrase, render, send_for_review, now)
from factory.buffer_api import Buffer


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
            for job in jobs:
                if job['status'] in ('planned','rendering'):
                    # A separate actor performance for each video; no cross-job clip cache.
                    render(ledger, {}, job, save)
                elif job['status'] == 'ready':
                    send_for_review(job, save)
            print('Daily pair delivered for Telegram review. Buffer requires approval.', flush=True)
    finally:
        ledger.store.client.delete_object(Bucket=ledger.store.bucket, Key=ledger.store.prefix+'lock.json')

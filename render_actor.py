"""One Leela demo with a speaking AI actor before and after real gameplay."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import textwrap
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from factory import core
from run import R2Store
from render_gameplay import read_remote

MODEL = 'veo-3.1-lite-generate-preview'
BASE = 'https://generativelanguage.googleapis.com/v1beta/'
GAMEPLAY = 'content-factory/v1/leela-full-001/final.mp4'
INTRO_LINE = "I keep overthinking everything. Let's try bringing that question to Leela."
OUTRO_LINE = 'Okay, that gives me something to reflect on. What would you ask?'
LOOK = ('Vertical 9:16 photorealistic casual smartphone video, one fictional adult woman aged 30, '
        'shoulder-length dark brown hair, brown eyes, beige sweater, seated at home beside a window. '
        'Warm natural daylight, eye-level medium closeup, natural skin texture, subtle handheld motion. '
        'A staged product demonstration with a fictional actor. No other people, no text or logos. '
        'Natural conversational American English, audible clear synchronized speech, quiet room, no music. ')
PROMPTS = [LOOK + 'She looks into the camera, curious and slightly amused, and says exactly: "' + INTRO_LINE +
           '". Pronounce Leela as LEE-lah. Start speaking at 0.5 seconds, finish by 7 seconds, then glance down at her phone off camera.',
           LOOK + 'Continue with exactly the same woman, face, clothing, room, lighting and voice as the supplied first frame. '
           'She glances up from her phone off camera, pauses thoughtfully, gives a small surprised smile, and says exactly: "' +
           OUTRO_LINE + '". Start at 0.5 seconds, finish by 7 seconds. Natural understated reflection, no exaggerated claims.']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(response):
    if response.status_code != 200:
        raise RuntimeError(f'Google API HTTP {response.status_code}; no automatic generation retry.')
    return response.json()


def download_video(uri, target, key):
    for _ in range(6):
        parsed = urlparse(uri)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host.endswith('.googleapis.com') or host.endswith('.googleusercontent.com')):
            raise RuntimeError('Unexpected video download host.')
        headers = {'x-goog-api-key': key} if host == 'generativelanguage.googleapis.com' else {}
        with requests.get(uri, headers=headers, stream=True, timeout=(30, 300), allow_redirects=False) as response:
            if response.is_redirect:
                uri = urljoin(uri, response.headers['Location'])
                continue
            if response.status_code != 200:
                raise RuntimeError(f'Video download HTTP {response.status_code}; generation will not repeat.')
            with target.open('wb') as output:
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
            return
    raise RuntimeError('Too many download redirects.')


def actor_clip(store, job, state, index, key, reference=None):
    item = state['clips'][index]
    name = ['intro.mp4', 'outro.mp4'][index]
    target = job / name
    def save():
        store.put('state.json', json.dumps(state).encode())
    if item['status'] == 'ready':
        store.client.download_file(store.bucket, store.prefix + name, str(target))
        if sha(target) != item['sha256']:
            raise RuntimeError('Saved actor clip checksum mismatch.')
        return target
    if item['status'] == 'new':
        instance = {'prompt': PROMPTS[index]}
        if reference:
            instance['image'] = {'bytesBase64Encoded': base64.b64encode(reference.read_bytes()).decode(), 'mimeType': 'image/png'}
        payload = {'instances': [instance], 'parameters': {'sampleCount': 1,
            'durationSeconds': 8, 'aspectRatio': '9:16', 'resolution': '720p',
            'personGeneration': 'allow_adult' if reference else 'allow_all'}}
        # Persist BEFORE POST: a timeout/unknown result must never spend again.
        item['status'] = 'pending'
        save()
        print(f'Actor clip {index + 1}/2: one Veo Lite request, 8 seconds.', flush=True)
        operation = checked(requests.post(BASE + f'models/{MODEL}:predictLongRunning',
            headers={'x-goog-api-key': key}, json=payload, timeout=(30, 180), allow_redirects=False))
        # Save the operation before polling so a later run can resume it.
        store.put(name + '.operation.json', json.dumps(operation).encode())
        item.update(status='processing', operation=operation.get('name'))
        save()
    elif item['status'] == 'pending':
        recovered = read_remote(store, name + '.operation.json')
        if not recovered or not recovered.get('name'):
            raise RuntimeError('Previous generation result unknown; no new paid request sent.')
        item.update(status='processing', operation=recovered['name'])
        save()
    if item['status'] != 'processing' or not re.fullmatch(r'[A-Za-z0-9_./-]+', item.get('operation') or ''):
        raise RuntimeError('Actor operation is not resumable; check private state.')
    for _ in range(120):
        operation = checked(requests.get(BASE + item['operation'], headers={'x-goog-api-key': key},
                            timeout=(30, 90), allow_redirects=False))
        if operation.get('done'):
            store.put(name + '.response.json', json.dumps(operation).encode())
            if operation.get('error'):
                item['status'] = 'failed'
                save()
                raise RuntimeError('Veo generation failed; details saved privately in R2. No retry.')
            samples = operation.get('response', {}).get('generateVideoResponse', {}).get('generatedSamples', [])
            if not samples or not samples[0].get('video', {}).get('uri'):
                item['status'] = 'blocked_or_empty'
                save()
                raise RuntimeError('Veo returned no video; response saved privately. No retry.')
            download_video(samples[0]['video']['uri'], target, key)
            info = media_info(target)
            if not info['audio'] or info['duration'] < 7.5:
                raise RuntimeError('Generated actor clip has missing audio or unexpected duration.')
            store.upload_file(target)
            item.update(status='ready', sha256=sha(target))
            save()
            return target
        print(f'Actor clip {index + 1}: waiting for Google generation.', flush=True)
        time.sleep(10)
    raise RuntimeError('Generation still processing; rerun SAME job to poll without paying again.')


def media_info(path):
    data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
    return {'duration': float(data['format']['duration']),
            'audio': any(s['codec_type'] == 'audio' for s in data['streams'])}


def assemble(job, sources):
    font = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    outputs = []
    for index, source in enumerate(sources):
        filters = ('scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                   'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0xf5eedf,setsar=1,fps=30')
        if index != 1:
            line = INTRO_LINE if index == 0 else OUTRO_LINE
            (job / 'line.txt').write_text('\n'.join(textwrap.wrap(line, width=36)), encoding='utf-8')
            filters += (f",drawtext=fontfile={font}:textfile=line.txt:fontcolor=white:fontsize=38:"
                        'borderw=2:bordercolor=black:x=(w-text_w)/2:y=h-260:line_spacing=10,'
                        f'drawtext=fontfile={font}:text=AI actor - demonstration:fontcolor=white:'
                        'fontsize=24:box=1:boxcolor=black@0.5:x=40:y=70')
        target = job / f'part-{index}.mp4'
        with (job / 'assembly.log').open('a') as log:
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', str(source.resolve()),
                '-vf', filters, '-af', 'aresample=48000,apad', '-shortest', '-c:v', 'libx264',
                '-preset', 'fast', '-crf', '23', '-maxrate', '5M', '-bufsize', '10M',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
                '-video_track_timescale', '15360', str(target.resolve())], cwd=job,
                stdout=log, stderr=log, check=True, timeout=1200)
        outputs.append(target)
    (job / 'concat.txt').write_text(''.join(f"file '{p.name}'\n" for p in outputs))
    final = job / 'final.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
        '-i', str(job / 'concat.txt'), '-c', 'copy', '-movflags', '+faststart', str(final)], check=True, timeout=120)
    core.probe(final)
    expected = sum(media_info(p)['duration'] for p in sources)
    if abs(media_info(final)['duration'] - expected) > 0.5 or final.stat().st_size > 50_000_000:
        raise RuntimeError('Final duration or size failed validation.')
    return final


def main():
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', core.JOB_ID):
        raise RuntimeError('Invalid job ID.')
    store = R2Store()
    job = (Path('actor-jobs') / core.JOB_ID).resolve()
    job.mkdir(parents=True, exist_ok=True)
    key = os.environ['GEMINI_API_KEY'].strip()
    spec = dict(model=MODEL, prompts=PROMPTS, source=GAMEPLAY, seconds_per_clip=8, max_requests=2,
                caption='Leela: what question would you bring?\n\nStaged demonstration with an AI-generated actor; real gameplay recording. #Leela #SelfReflection')
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    store.put('lock.json', b'{}', IfNoneMatch='*')
    try:
        state = read_remote(store, 'state.json')
        if state and state.get('fingerprint') != fingerprint:
            raise RuntimeError('Job configuration changed; existing generated assets preserved.')
        if state and state.get('status') == 'ready':
            print('Already assembled. Delivery can reuse saved result.')
            return
        gameplay = job / 'gameplay.mp4'
        store.client.download_file(store.bucket, GAMEPLAY, str(gameplay))
        core.probe(gameplay)
        if state and state.get('gameplay_sha256') != sha(gameplay):
            raise RuntimeError('Gameplay changed; existing job preserved.')
        if not state:
            # Read-only access check before the first charge.
            checked(requests.get(BASE + 'models/' + MODEL, headers={'x-goog-api-key': key}, timeout=30, allow_redirects=False))
            state = dict(fingerprint=fingerprint, status='generating', gameplay_sha256=sha(gameplay),
                         clips=[{'status': 'new'}, {'status': 'new'}])
            store.put('spec.json', json.dumps(spec).encode())
            store.put('state.json', json.dumps(state).encode())
        intro = actor_clip(store, job, state, 0, key)
        reference = job / 'actor.png'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-ss', '1', '-i', str(intro),
                        '-frames:v', '1', str(reference)], check=True, timeout=30)
        outro = actor_clip(store, job, state, 1, key, reference)
        final = assemble(job, [intro, gameplay, outro])
        store.upload_file(final)
        state.update(status='ready', video_sha256=sha(final), video_info=core.probe(final))
        store.put('state.json', json.dumps(state).encode())
        print(f'Actor + complete gameplay + actor: {state["video_info"]["duration"]:.2f} seconds. Ready for Telegram.')
    finally:
        if (job / 'assembly.log').exists():
            store.upload_file(job / 'assembly.log')
        store.client.delete_object(Bucket=store.bucket, Key=store.prefix + 'lock.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Stopped: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), flush=True)
        sys.exit(1)

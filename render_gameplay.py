"""Render a private R2 gameplay recording without image-generation requests."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from botocore.exceptions import ClientError
from factory import core
from run import R2Store

SCRIPT = (
    'What question would you bring to Leela? '
    'Start with something that has been on your mind. '
    'Roll the dice and follow your move across the board. '
    'Read the reflection for the space you land on. '
    'Take a moment to notice what resonates with you. '
    'Try Leela with your own question.'
)
CAPTION = 'Find on Telegram: @leela_ru_bot'


def number(name, default, low, high):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or not low <= value <= high:
        raise RuntimeError(f'Invalid {name}; expected {low} to {high}.')
    return value


def configuration():
    key = os.environ.get('GAMEPLAY_KEY', 'content-factory/assets/leela/gameplay-en-001.mp4')
    if not key.startswith(('assets/leela/', 'content-factory/assets/leela/')) or not key.lower().endswith(('.mp4', '.mov')):
        raise RuntimeError('GAMEPLAY_KEY must be an MP4 or MOV inside assets/leela/ or content-factory/assets/leela/.')
    return dict(profile='leela-gameplay-v2-full', source_key=key,
                start=number('CLIP_START', '0', 0, 36000),
                seconds=number('CLIP_SECONDS', '0', 0, 36000),
                speed=number('GAMEPLAY_SPEED', '2.5', 2, 3),
                script=SCRIPT, caption=CAPTION, voice='en-US-JennyNeural',
                engine_commit=core.EXPECTED_COMMIT)


def read_remote(store, name):
    try:
        return json.loads(store.client.get_object(Bucket=store.bucket,
                           Key=store.prefix + name)['Body'].read())
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('NoSuchKey', '404'):
            return None
        raise


def prepare_clip(source, target, config):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', str(source)], capture_output=True,
        text=True, check=True, timeout=60)
    data = json.loads(result.stdout)
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or not any(s.get('codec_type') == 'video' for s in data['streams']):
        raise RuntimeError('Source has no readable video.')
    available = duration - config['start']
    length = min(config['seconds'], available) if config['seconds'] else available
    if length < 5:
        raise RuntimeError('Selected source fragment is shorter than 5 seconds. Adjust CLIP_START.')
    # Preserve the complete phone screen. Never crop game text to fill 9:16.
    filters = (f"setpts=(PTS-STARTPTS)/{config['speed']},"
        'scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,'
        'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0xf5eedf,setsar=1,fps=30,'
        'tpad=stop_mode=clone:stop_duration=40')
    with target.with_suffix('.log').open('w') as log:
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-ss', str(config['start']),
            '-i', str(source), '-t', str(length / config['speed'] + 40),
            '-vf', f'trim=duration={length},' + filters, '-an', '-c:v', 'libx264',
            '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(target)], stdout=log, stderr=log,
            check=True, timeout=900)
    return length


def encode_full(clip, narration, final, subtitle, duration):
    # Reserve overhead below Telegram's 50 MB cap.
    bitrate = min(6_000_000, int(44_000_000 * 8 / duration) - 128_000)
    if bitrate < 100_000:
        raise RuntimeError('Recording is too long for the Telegram video limit.')
    # Use a fixed local filename as the filter argument, never user-supplied paths.
    shutil.copyfile(subtitle, final.parent / 'burn.srt')
    filters = ("subtitles=burn.srt:force_style='FontName=DejaVu Sans,"
               "FontSize=20,Outline=2,MarginV=35',"
               "tpad=stop_mode=clone:stop_duration=" + str(duration))
    with (final.parent / 'encode.log').open('w') as log:
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error',
            '-i', str(clip.resolve()), '-i', str(narration.resolve()),
            '-map', '0:v:0', '-map', '1:a:0', '-vf', filters, '-af', 'apad',
            '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast',
            '-crf', '23', '-maxrate', str(bitrate), '-bufsize', str(bitrate * 2),
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart', str(final.resolve())],
            cwd=final.parent, stdout=log, stderr=log, check=True, timeout=3600)


def main():
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', core.JOB_ID):
        raise RuntimeError('Invalid JOB_ID.')
    config = configuration()
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=core.REPO, text=True).strip() != core.EXPECTED_COMMIT:
        raise RuntimeError('Unexpected MoneyPrinterTurbo version.')
    store = R2Store(require_gemini=False)
    job = core.REPO / 'storage/content_factory' / core.JOB_ID
    job.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        store.put(name, json.dumps(value, ensure_ascii=False).encode())
    # Shared generator lock. No takeover of a potentially running job.
    store.put('lock.json', json.dumps({'run_id': os.environ.get('GITHUB_RUN_ID')}).encode(), IfNoneMatch='*')
    try:
        state = read_remote(store, 'state.json')
        if state and state.get('fingerprint') != fingerprint:
            raise RuntimeError('This job ID belongs to different settings. Use a new job ID.')
        if state and state.get('status') == 'ready':
            final = job / 'final.mp4'
            store.client.download_file(store.bucket, store.prefix + 'final.mp4', str(final))
            if core.digest(final) != state['video_sha256']:
                raise RuntimeError('Saved result checksum mismatch.')
            core.probe(final)
            print('Already rendered; using saved video.', flush=True)
            return
        source = job / 'source.mp4'
        try:
            head = store.client.head_object(Bucket=store.bucket, Key=config['source_key'])
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey'):
                raise RuntimeError('Source not found in R2: ' + config['source_key']) from None
            raise
        if not 0 < head['ContentLength'] <= 1_000_000_000:
            raise RuntimeError('Source must be nonempty and at most 1 GB.')
        print('Downloading private gameplay recording from R2.', flush=True)
        response = store.client.get_object(Bucket=store.bucket, Key=config['source_key'], IfMatch=head['ETag'])
        with source.open('wb') as output:
            shutil.copyfileobj(response['Body'], output)
        response['Body'].close()
        source_hash = core.digest(source)
        if state and state.get('source_sha256') != source_hash:
            raise RuntimeError('Source recording changed. Use a new job ID.')
        state = dict(status='rendering', fingerprint=fingerprint, source_sha256=source_hash)
        save('state.json', state)
        save('spec.json', dict(config, source_sha256=source_hash))
        print('Preparing gameplay at requested speed; no image API calls.', flush=True)
        clip = job / 'gameplay.mp4'
        length = prepare_clip(source, clip, config)
        settings = core.get_settings([clip])
        settings.update(video_subject='Leela gameplay', video_script=config['script'],
            voice_name=config['voice'], video_clip_duration=math.ceil(length / config['speed'] + 40),
            font_size=48)
        core.save_json(job / 'params.json', settings)
        store.upload_file(job / 'params.json')
        core.run_worker(job, 'check', 'preflight')
        core.run_worker(job, 'render', str(uuid.uuid4()))
        result = core.read_json(job / 'result.json')
        rendered = Path(result['videos'][0])
        final = job / 'final.mp4'
        subtitle = Path(result.get('subtitle_path') or '')
        if not subtitle.is_file() or not subtitle.stat().st_size:
            raise RuntimeError('Missing generated subtitles.')
        # MPT renders only as long as narration. Use its audio/subtitles but
        # assemble from the COMPLETE accelerated gameplay instead of that cut.
        narration = core.probe(rendered)['duration']
        gameplay_duration = length / config['speed']
        output_duration = max(gameplay_duration, narration)
        core_path = job / 'subtitle.srt'
        shutil.copyfile(subtitle, core_path)
        encode_full(clip, rendered, final, core_path, output_duration)
        info = core.probe(final)
        if abs(info['duration'] - output_duration) > 0.25:
            raise RuntimeError('Output duration does not cover the complete selected recording.')
        subtitle = core_path
        state.update(source_seconds=length, gameplay_seconds=gameplay_duration)
        print(f'Complete source: {length:.2f}s; accelerated: {gameplay_duration:.2f}s; output: {info["duration"]:.2f}s.', flush=True)
        if final.stat().st_size > 50_000_000:
            raise RuntimeError('Result exceeds Telegram delivery size.')
        store.upload_file(final)
        store.upload_file(subtitle, 'subtitle.srt')
        state.update(status='ready', video_sha256=core.digest(final), video_info=info)
        save('state.json', state)
        print('Gameplay video saved to private R2; ready for Telegram.', flush=True)
    finally:
        for name in ('preflight.log', 'render.log', 'gameplay.log', 'encode.log'):
            if (job / name).is_file():
                try:
                    store.upload_file(job / name)
                except Exception:
                    print('Could not save diagnostic log.', flush=True)
        store.client.delete_object(Bucket=store.bucket, Key=store.prefix + 'lock.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid exposing signed URLs or credentials through SDK exception text.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('Stopped: ' + message, flush=True)
        sys.exit(1)

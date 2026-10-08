"""Thought cards, complete silent gameplay, one clean spoken reaction."""
import array
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import edge_tts
import render_actor as actor
from factory import core
from run import R2Store
from render_gameplay import read_remote

SOURCE = 'content-factory/assets/leela/gameplay-en-001.mp4'
SPEED = 2.5
PROMPT = (
    'One continuous 8-second vertical 9:16 photorealistic smartphone shot. '
    'One fictional adult woman aged 30, shoulder-length dark brown hair, beige sweater, '
    'seated at home by a window, warm natural daylight, medium close-up. '
    'A staged fictional product demo. No text, logos, captions or music. '
    'Seconds 0 to 4: she silently reads her phone held just below the frame, thoughtful expression, mouth closed. '
    'Second 4: she looks up, raises her eyebrows, a small surprised smile. '
    'From second 5 to second 6.2 she says ONLY "No way!" with natural surprised American English delivery. '
    'Seconds 6.2 to 8: she smiles at the camera, silent, lips closed. '
    'No other words, no narration, no singing, no background music. '
    'Subtle natural motion, no camera cuts, consistent face and clothing throughout.'
)


def ffmpeg(job, args):
    with (job / 'edit.log').open('a') as log:
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error'] + args,
                       cwd=job, stdout=log, stderr=log, check=True, timeout=1200)


def card(filename, y, start, end, size=52):
    font = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    enabled = f"between(t,{start},{end})"
    return (f",drawbox=x=55:y={y}:w=970:h=190:color=0xfffaf0@0.96:t=fill:enable='{enabled}',"
            f"drawtext=fontfile={font}:textfile={filename}:fontsize={size}:fontcolor=0x33281a:"
            f"line_spacing=12:x=(w-text_w)/2:y={y}+35:enable='{enabled}'")


def music(path, duration, reaction_at):
    """Original synthesized ambient score, one continuous file across all cuts."""
    rate = 44100
    chords = [(220, 261.63, 329.63), (174.61, 220, 261.63),
              (130.81, 164.81, 196), (196, 246.94, 293.66)]
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        for block in range(math.ceil(duration)):
            data = array.array('h')
            for i in range(min(rate, round(duration * rate) - block * rate)):
                t = block + i / rate
                bar = int(t // 4)
                phase = t % 4
                # Cross-fade chord boundaries instead of restarting at edit cuts.
                blend = min(1, phase / .4)
                current = sum(math.sin(2 * math.pi * f * t) for f in chords[bar % 4]) / 3
                previous = sum(math.sin(2 * math.pi * f * t) for f in chords[(bar - 1) % 4]) / 3
                sample = blend * current + (1 - blend) * previous
                fade = min(1, t / 1.2, max(0, duration - t) / 1.5)
                duck = 1 - .65 * max(0, min(1, (t - reaction_at + .3) / .3,
                                                    (reaction_at + 1.8 - t) / .3))
                data.append(int(32767 * .07 * sample * fade * duck))
            if sys.byteorder != 'little':
                data.byteswap()
            out.writeframes(data.tobytes())


def edit(job, person, gameplay, voice):
    duration = actor.media_info(gameplay)['duration']
    if not math.isfinite(duration) or duration < 5:
        raise RuntimeError('Invalid gameplay duration.')
    game_length = duration / SPEED + 2  # all source frames plus readable final hold
    total = 3 + game_length + 4
    (job / 'hook.txt').write_text('Why do I\noverthink everything?')
    (job / 'game.txt').write_text("Let's ask Leela.")
    (job / 'reaction.txt').write_text('Why is this\nso accurate?')
    (job / 'cta.txt').write_text('What would\nyou ask?')
    normal = ('scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,'
              'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0xf5eedf,setsar=1,fps=30')
    label = ''
    parts = [
        (person, 0, 3, normal + label + card('hook.txt', 1200, 0, 3)),
        (gameplay, 0, game_length, f'setpts=(PTS-STARTPTS)/{SPEED},' + normal +
         ',tpad=stop_mode=clone:stop_duration=2' + card('game.txt', 200, 0, 2.2)),
        (person, 4, 4, normal + label + card('reaction.txt', 1200, 0, 2.1) +
         card('cta.txt', 1200, 2.1, 4)),
    ]
    for index, (source, start, length, filters) in enumerate(parts):
        ffmpeg(job, ['-ss', str(start), '-i', str(source), '-t', str(length),
            '-vf', filters, '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
            '-maxrate', '5M', '-bufsize', '10M', '-pix_fmt', 'yuv420p',
            '-video_track_timescale', '15360', str(job / f'part-{index}.mp4')])
    (job / 'concat.txt').write_text("file 'part-0.mp4'\nfile 'part-1.mp4'\nfile 'part-2.mp4'\n")
    ffmpeg(job, ['-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy', 'silent.mp4'])
    actual = actor.media_info(job / 'silent.mp4')['duration']
    reaction_at = 3 + game_length + 1
    music(job / 'music.wav', actual, reaction_at)
    delay = round(reaction_at * 1000)
    ffmpeg(job, ['-i', 'silent.mp4', '-i', 'music.wav', '-i', str(voice),
        '-filter_complex', f'[2:a]adelay={delay}:all=1,volume=1.0[v];'
        '[1:a][v]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]',
        '-map', '0:v:0', '-map', '[a]', '-t', str(actual), '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
        '-movflags', '+faststart', 'final.mp4'])
    final = job / 'final.mp4'
    info = core.probe(final)
    if abs(info['duration'] - total) > .25 or final.stat().st_size > 50_000_000:
        raise RuntimeError('Final duration or Telegram size check failed.')
    return final, info


def main():
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', core.JOB_ID):
        raise RuntimeError('Invalid job ID.')
    store = R2Store()
    job = (Path('thought-jobs') / core.JOB_ID).resolve()
    job.mkdir(parents=True, exist_ok=True)
    spec = dict(version=1, model=actor.MODEL, prompt=PROMPT, source=SOURCE, speed=SPEED,
        max_requests=1, speech='No way!', voice='en-US-JennyNeural',
        caption='Find us on Telegram: @leela-ru-bot')
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    store.put('lock.json', b'{}', IfNoneMatch='*')
    try:
        state = read_remote(store, 'state.json')
        if state and state.get('fingerprint') != fingerprint:
            raise RuntimeError('Settings changed. Existing generated clip preserved.')
        if state and state.get('status') == 'ready':
            print('Already ready; reuse saved result.')
            return
        source = job / 'source.mp4'
        store.client.download_file(store.bucket, SOURCE, str(source))
        source_sha = actor.sha(source)
        if state and state.get('source_sha256') != source_sha:
            raise RuntimeError('Source changed; existing job preserved.')
        if not state:
            state = dict(status='generating', fingerprint=fingerprint, source_sha256=source_sha,
                         clips=[{'status': 'new'}])
            store.put('spec.json', json.dumps(spec).encode())
            store.put('state.json', json.dumps(state).encode())
        # All dialogue comes from a separate clean speech track. Discard Veo audio.
        voice = job / 'reaction.mp3'
        if not read_remote(store, 'voice.json'):
            asyncio.run(edge_tts.Communicate('No way!', spec['voice'], rate='+10%').save(str(voice)))
            store.upload_file(voice)
            store.put('voice.json', json.dumps({'sha256': actor.sha(voice)}).encode())
        else:
            store.client.download_file(store.bucket, store.prefix + voice.name, str(voice))
        actor.PROMPTS = [PROMPT]
        person = actor.actor_clip(store, job, state, 0, os.environ['GEMINI_API_KEY'].strip())
        final, info = edit(job, person, source, voice)
        store.upload_file(final)
        state.update(status='ready', video_sha256=actor.sha(final), video_info=info)
        store.put('state.json', json.dumps(state).encode())
        print(f'Thought cards, full gameplay, one spoken reaction: {info["duration"]:.2f}s. Ready for Telegram.')
    finally:
        if (job / 'edit.log').exists():
            store.upload_file(job / 'edit.log')
        store.client.delete_object(Bucket=store.bucket, Key=store.prefix + 'lock.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Stopped: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), flush=True)
        sys.exit(1)

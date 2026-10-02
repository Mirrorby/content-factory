"""Run locally once; never run OAuth consent inside GitHub Actions."""
import argparse
import json
import os
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('client_json', type=Path)
    parser.add_argument('--output', type=Path, default=Path.home() / 'content-factory-youtube-secret.json')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output already exists. Choose another --output path.')
    config = json.loads(args.client_json.read_text())
    if 'installed' not in config:
        raise SystemExit('Use an OAuth client of type Desktop app.')
    flow = InstalledAppFlow.from_client_config(config, scopes=['https://www.googleapis.com/auth/youtube.upload'])
    credentials = flow.run_local_server(
        host='localhost', port=0, access_type='offline', prompt='consent',
        authorization_prompt_message='Open this address in your browser: {url}',
        success_message='Authorization complete. You may close this window.',
        timeout_seconds=300,
    )
    if not credentials.refresh_token:
        raise SystemExit('No refresh token returned. Repeat authorization with consent.')
    secret = {'client_id': credentials.client_id, 'client_secret': credentials.client_secret,
              'refresh_token': credentials.refresh_token}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(secret, output)
    print(f'Saved: {args.output}')
    print('Copy its contents to GitHub repository secret YOUTUBE_OAUTH_JSON. Do not commit it.')


if __name__ == '__main__':
    main()

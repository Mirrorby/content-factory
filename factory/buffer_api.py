"""Buffer GraphQL adapter. Never retry mutations or print response bodies/URLs."""
import requests


class Buffer:
    def __init__(self, key, organization_id):
        self.key, self.organization_id = key, organization_id
        self.calls = 0

    def query(self, query, variables):
        self.calls += 1
        # Leave space for manual use and reconciliation on the Free plan.
        if self.calls > 90:
            raise RuntimeError('Buffer request budget reached; resume later.')
        try:
            response = requests.post('https://api.buffer.com', headers={
                'Authorization': 'Bearer ' + self.key},
                json={'query': query, 'variables': variables}, timeout=(15, 60),
                allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError('Buffer HTTP ' + str(response.status_code))
            result = response.json()
            if result.get('errors') or not result.get('data'):
                raise RuntimeError('Buffer GraphQL error; check account/schema configuration.')
            return result['data']
        except requests.RequestException:
            raise RuntimeError('Buffer request outcome unavailable.') from None

    def queue(self, channels):
        query = '''query($input: PostsInput!, $after: String) {
          posts(first:100, after:$after, input:$input) {
            edges { node { id channelId status dueAt } }
            pageInfo { hasNextPage endCursor }
          }
        }'''
        result, after = [], None
        while True:
            page = self.query(query, {'input': {'organizationId': self.organization_id,
                'filter': {'channelIds': channels,
                           'status': ['scheduled', 'sending', 'error', 'needs_approval', 'draft']}},
                'after': after})['posts']
            result.extend(edge['node'] for edge in page['edges'])
            if not page['pageInfo']['hasNextPage']:
                return result
            cursor = page['pageInfo']['endCursor']
            if not cursor or cursor == after:
                raise RuntimeError('Incomplete Buffer pagination; queue is unknown.')
            after = cursor

    def post(self, post_id):
        return self.query('''query($input: PostInput!) {
          post(input:$input) { id status channelId }
        }''', {'input': {'id': post_id}})['post']

    def preflight(self, character, config):
        channels = self.query('''query($input: ChannelsInput!) {
          channels(input:$input) { id service organizationId timezone
            isDisconnected isLocked isQueuePaused
            postingSchedule { day paused times }
          }
        }''', {'input': {'organizationId': self.organization_id}})['channels']
        by_id = {c['id']: c for c in channels}
        for platform, channel_id in character['channels'].items():
            channel = by_id.get(channel_id)
            if not channel or channel['service'] != platform:
                raise RuntimeError('Buffer channel mapping does not match its platform.')
            if any(channel[k] for k in ('isDisconnected', 'isLocked', 'isQueuePaused')):
                raise RuntimeError('Buffer channel disconnected, locked or paused.')
            if channel['timezone'] != config['posting_timezone']:
                raise RuntimeError('Buffer channel timezone differs from production config.')
            days = channel['postingSchedule']
            if (len(days) != 7 or
                {d['day'] for d in days} != {'mon','tue','wed','thu','fri','sat','sun'} or
                any(d['paused'] or len(d['times']) != config.get('posts_per_day', 2) or
                    len(set(d['times'])) != config.get('posts_per_day', 2) for d in days)):
                raise RuntimeError('Set the two daily posting slots in every Buffer channel.')

        return by_id

    def create(self, channel, platform, url, title, caption, due_at=None):
        metadata = {
            'youtube': {'title': title[:100], 'categoryId': '22', 'privacy': 'public',
                        'madeForKids': False, 'isAiGenerated': True},
            'instagram': {'type': 'reel', 'shouldShareToFeed': True, 'isAiGenerated': True},
            'tiktok': {'isAiGenerated': True},
        }
        payload = dict(text=caption, channelId=channel, schedulingType='automatic',
                       mode='addToQueue', needsApproval=False, aiAssisted=True,
                       assets=[{'video': {'url': url}}], metadata={platform: metadata[platform]})
        if due_at:
            payload.update(mode='customScheduled', dueAt=due_at)
        data = self.query('''mutation($input: CreatePostInput!) {
          createPost(input:$input) {
            ... on PostActionSuccess { post { id dueAt } }
            ... on MutationError { message }
          }
        }''', {'input': payload})['createPost']
        if not data.get('post', {}).get('id'):
            raise RuntimeError('Buffer did not confirm creation. Inspect queue before retrying.')
        return data['post']


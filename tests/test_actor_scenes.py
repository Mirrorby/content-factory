import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from factory.actor_scenes import scene_reference, video_prompt


class SceneImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name)
        self.reference = self.job / 'reference.png'
        Image.new('RGB', (300, 300)).save(self.reference)
        output = io.BytesIO()
        Image.new('RGB', (576, 1024)).save(output, format='PNG')
        self.response = {'outputs': [{'type':'image', 'data':base64.b64encode(output.getvalue()).decode()}]}
        self.scene = dict(setting='Quiet cafe.',outfit='Blue shirt.',framing='Chest-up.',reaction='Raises eyebrows.')
        self.store = Mock(bucket='private', prefix='job/')

    def test_one_request_and_ready_checkpoint_reuses_image(self):
        state = {}
        with patch('factory.actor_scenes.read_remote',return_value=None), patch('factory.actor_scenes.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = self.response
            def respond(*args, **kwargs):
                self.assertEqual(state['scene_image']['status'], 'pending')
                return post.return_value
            post.side_effect = respond
            target = scene_reference(self.store,self.job,state,self.reference,self.scene,'test')
            self.assertEqual(state['scene_image']['status'],'ready')
            scene_reference(self.store,self.job,state,self.reference,self.scene,'test')
            self.assertEqual(post.call_count,1)
            self.assertNotEqual(target.read_bytes(),self.reference.read_bytes())
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['response_format']['aspect_ratio'],'9:16')
            self.assertIn('identity only',payload['input'][0]['text'])

    def test_unknown_image_request_never_repeats(self):
        state = {'scene_image':{'status':'pending'}}
        with patch('factory.actor_scenes.read_remote',return_value=None), patch('factory.actor_scenes.requests.post') as post:
            with self.assertRaisesRegex(RuntimeError,'unresolved'):
                scene_reference(self.store,self.job,state,self.reference,self.scene,'test')
            post.assert_not_called()

    def test_saved_response_restores_without_new_request(self):
        state = {'scene_image':{'status':'pending'}}
        with patch('factory.actor_scenes.read_remote',return_value=self.response), patch('factory.actor_scenes.requests.post') as post:
            scene_reference(self.store,self.job,state,self.reference,self.scene,'test')
            post.assert_not_called()
            self.assertEqual(state['scene_image']['status'],'ready')

    def test_scene_video_keeps_native_complete_reaction(self):
        prompt = video_prompt(self.scene)
        self.assertIn('Blue shirt.',prompt)
        self.assertIn('Quiet cafe.',prompt)
        self.assertIn('synchronized to her lips',prompt)
        self.assertIn('No way!',prompt)


if __name__ == '__main__':
    unittest.main()

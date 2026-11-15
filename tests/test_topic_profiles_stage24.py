import tempfile
import unittest
from pathlib import Path
from plugins.newsroom.store import Store
from plugins.newsroom.desk import Desk

class ProfilesTest(unittest.TestCase):
    def test_inheritance_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            desk = Desk(Store(Path(tmp) / 'news.db'))
            self.assertEqual(desk.topic_profile('energy_blackout')['tone'], '')
            desk.save_topic_profile('energy', tone='нейтральний', sources='офіційні')
            self.assertEqual(desk.topic_profile('energy_blackout')['tone'], 'нейтральний')
            desk.save_topic_profile('energy_blackout', tone='терміновий', outcome='коротка новина')
            self.assertEqual(desk.topic_profile('energy_blackout')['tone'], 'терміновий')
            self.assertEqual(desk.topic_profile('energy_blackout')['sources'], 'офіційні')
            desk.save_topic_profile('energy_blackout', tone='')
            self.assertEqual(desk.topic_profile('energy_blackout')['tone'], 'нейтральний')
            self.assertIn('Пріоритетні джерела', desk.editorial_instructions('energy_blackout'))
            with self.assertRaises(ValueError):
                desk.save_topic_profile('energy', forbidden='x')

if __name__ == '__main__': unittest.main()

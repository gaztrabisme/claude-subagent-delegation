import unittest

import scribe


class Smoke(unittest.TestCase):
    def test_package_imports(self):
        self.assertIsNotNone(scribe.__name__)


if __name__ == "__main__":
    unittest.main()

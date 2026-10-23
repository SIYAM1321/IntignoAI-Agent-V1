import unittest
from tools.web_search import _DDGParser


class WebSearchParserTests(unittest.TestCase):
    def test_ddg_result_parser(self):
        html = '''<div class="result"><h2><a class="result__a" href="https://example.com/?uddg=https%3A%2F%2Fexample.org%2Fpage">Example title</a></h2><a class="result__snippet">Example snippet text.</a></div>'''
        parser = _DDGParser(); parser.feed(html)
        self.assertEqual(len(parser.results), 1)
        self.assertEqual(parser.results[0]["title"], "Example title")
        self.assertIn("Example snippet", parser.results[0]["snippet"])


if __name__ == "__main__":
    unittest.main()

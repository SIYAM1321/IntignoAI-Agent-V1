import unittest

from agent.engine import _dispatch_sse_event, _parse_function_arguments


class ParserShapeTests(unittest.TestCase):
    def test_sse_lines_can_be_reassembled(self):
        raw = 'event: step.delta\ndata: {"index":0,"delta":{"type":"text","text":"Hi"}}\n\n'
        lines = raw.splitlines()
        self.assertEqual(lines[0], 'event: step.delta')
        self.assertTrue(lines[1].startswith('data:'))
        self.assertEqual(lines[2], '')

    def test_done_sentinel_is_ignored(self):
        received = []
        keep = _dispatch_sse_event("done", ["[DONE]"], lambda event, data: received.append((event, data)))
        self.assertFalse(keep)
        self.assertEqual(received, [])

    def test_done_sentinel_without_event_name_is_ignored(self):
        received = []
        keep = _dispatch_sse_event(None, ["[DONE]"], lambda event, data: received.append((event, data)))
        self.assertFalse(keep)
        self.assertEqual(received, [])


    def test_empty_step_start_arguments_do_not_break_streamed_json(self):
        self.assertEqual(_parse_function_arguments('{}{"query":"Dewan Sakibul Islam"}'), {"query": "Dewan Sakibul Islam"})
        self.assertEqual(_parse_function_arguments('{"path":"C:/Users/hp"}'), {"path": "C:/Users/hp"})

    def test_json_event_is_dispatched(self):
        received = []
        keep = _dispatch_sse_event("step.delta", ['{"event_type":"step.delta","index":0}'], lambda event, data: received.append((event, data)))
        self.assertTrue(keep)
        self.assertEqual(received, [("step.delta", {"event_type": "step.delta", "index": 0})])


if __name__ == '__main__':
    unittest.main()

"""The assistant backends: each CLI adapter keeps one session per chat and streams the reply's text."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tana.assistant import BACKENDS, Unavailable, make_backend

# Stand-ins for the CLIs: they print each tool's documented output and log their arguments
FAKES = {
    "claude": r'''import json, sys
args = sys.argv[1:]; open(sys.argv[0] + ".log", "a").write(json.dumps(args) + "\n"); text = sys.stdin.read()
for w in ["You ", "said: ", text]:
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": w}}}))
''',
    "codex": r'''import json, sys
args = sys.argv[1:]; open(sys.argv[0] + ".log", "a").write(json.dumps(args) + "\n")
if args[1] != "resume": print(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "You said: " + args[-1].split("\n\n")[-1]}}))
''',
    "pi": r'''import json, sys
args = sys.argv[1:]; open(sys.argv[0] + ".log", "a").write(json.dumps(args) + "\n")
print(json.dumps({"type": "session", "id": args[args.index("--session-id") + 1]}))
for w in ["You said: ", args[-1]]:
    print(json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": w}}))
''',
    "dsh": r'''import json, sys
args = sys.argv[1:]; open(sys.argv[0] + ".log", "a").write(json.dumps(args) + "\n")
print("You said: " + args[-1].split("User: ")[-1])
''',
}


class CliBackends(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def fake(self, name: str) -> Path:
        path = self.tmp / name
        path.write_text(f"#!{os.sys.executable}\n" + FAKES[name])
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def talk(self, kind: str, binary: str, messages: list[str]) -> tuple[list[str], list[list[str]]]:
        b = make_backend({"backend": kind, "bin": str(self.fake(binary)), "state_dir": str(self.tmp / "state")})
        replies = []

        async def run():
            for m in messages:
                pieces = []

                async def on_text(p):
                    pieces.append(p)

                async def run_tool(n, a):
                    return ""
                await b.turn("chat-1", m, [], "Be brief.", on_text, run_tool)
                replies.append("".join(pieces).strip())
        asyncio.run(run())
        calls = [json.loads(line) for line in (self.tmp / f"{binary}.log").read_text().splitlines()]
        return replies, calls

    def test_claude_code_sets_a_session_then_resumes_it(self):
        replies, calls = self.talk("claude-code", "claude", ["hello", "again"])
        self.assertEqual(replies, ["You said: hello", "You said: again"])
        sid = calls[0][calls[0].index("--session-id") + 1]
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], sid)
        self.assertIn("--append-system-prompt", calls[0])

    def test_codex_resumes_the_thread_it_started(self):
        replies, calls = self.talk("codex", "codex", ["hello", "again"])
        self.assertEqual(replies, ["You said: hello", "You said: again"])
        self.assertEqual(calls[1][:3], ["exec", "resume", "thread-1"])
        self.assertTrue(calls[0][-1].startswith("Be brief."))         # instructions go with the first message

    def test_pi_keeps_one_session_per_chat(self):
        replies, calls = self.talk("pi", "pi", ["hello", "again"])
        self.assertEqual(replies, ["You said: hello", "You said: again"])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], calls[1][calls[1].index("--session-id") + 1])

    def test_dsh_gets_the_recent_exchanges_with_each_message(self):
        replies, calls = self.talk("dsh", "dsh", ["hello", "again"])
        self.assertEqual(replies, ["You said: hello", "You said: again"])
        self.assertIn("User: hello\nAssistant: You said: hello", calls[1][-1])

    def test_a_missing_program_is_reported_as_unavailable(self):
        b = make_backend({"backend": "codex", "bin": str(self.tmp / "nope"), "state_dir": str(self.tmp / "s")})

        async def run():
            async def noop(*a):
                return ""
            await b.turn("c", "hi", [], "", noop, noop)
        with self.assertRaises(Unavailable):
            asyncio.run(run())

    def test_every_backend_is_known_and_unknown_ones_are_refused(self):
        self.assertEqual(set(BACKENDS), {"rameness", "claude-code", "codex", "pi", "dsh"})
        self.assertIsNone(make_backend({"backend": "rameness"}))           # no url: not configured
        with self.assertRaises(ValueError):
            make_backend({"backend": "nope"})


if __name__ == "__main__":
    unittest.main()

"""Scripted OpenAI-compatible model server for reproducing hermes-rescue end to end (demo/run_demo.sh).

Behaviour is picked by the requested model name:
  missing-model    -> 404 model not found (like Ollama without the model pulled)
  dead-key         -> 401 invalid api key
  wrecker          -> first call: a terminal tool call that deletes HERMES memories;
                      after the tool result: a short final answer
  editor           -> first call: write_file into config.yaml; then a final answer
  pruner           -> first call: a raw `sqlite3 state.db 'DELETE FROM messages'`; then a final answer
  everyday         -> first call: ordinary build cleanup in a project folder (E2E_PROJECT); then an answer
  anything else    -> a plain answer
Non-streaming only (the test config sets model.streaming: false).
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ["E2E_HERMES_HOME"]
LOG = os.environ.get("E2E_LOG", "")


def completion(model, message, finish="stop"):
    return {"id": f"chatcmpl-{time.time_ns()}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}


def tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call_{time.time_ns()}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            ids = ["wrecker", "editor", "pruner", "everyday", "plain", "dead-key"]
            return self._send(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        model = body.get("model", "")
        msgs = body.get("messages", [])
        if LOG:
            with open(LOG, "a") as fh:
                fh.write(json.dumps({"model": model, "n": len(msgs), "last": msgs[-1] if msgs else None})[:2000] + "\n")
        had_tool_result = any(m.get("role") == "tool" for m in msgs)
        if model == "missing-model":
            return self._send(404, {"error": {"message": f"model '{model}' not found, try pulling it first",
                                              "type": "not_found_error"}})
        if model == "dead-key":
            return self._send(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error",
                                              "code": "invalid_api_key"}})
        if model == "wrecker" and not had_tool_result:
            return self._send(200, completion(model, tool_call(
                "terminal", {"command": f"rm -rf {HOME}/memories && echo cleaned"}), "tool_calls"))
        if model == "editor" and not had_tool_result:
            return self._send(200, completion(model, tool_call(
                "write_file", {"path": f"{HOME}/config.yaml", "content": "broken: [\n"}), "tool_calls"))
        if model == "pruner" and not had_tool_result:
            return self._send(200, completion(model, tool_call(
                "terminal", {"command": f"sqlite3 {HOME}/state.db 'DELETE FROM messages'"}), "tool_calls"))
        if model == "everyday" and not had_tool_result:
            return self._send(200, completion(model, tool_call(
                "terminal", {"command": "mkdir -p build && echo x > build/out.txt && rm -rf build && echo built",
                             "workdir": os.environ.get("E2E_PROJECT", "/tmp")}), "tool_calls"))
        text = "I tried to tidy up Hermes for you." if had_tool_result else "Hello! All good here."
        return self._send(200, completion(model, {"role": "assistant", "content": text}))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

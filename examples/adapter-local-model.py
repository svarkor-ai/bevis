#!/usr/bin/env python3
"""A bevis adapter that calls a local OpenAI-compatible model server.

The shape: an HTTP client. Works against llama.cpp's server, vLLM, Ollama's
compatible endpoint, LM Studio, or a hosted API — bevis cannot tell, because
bevis never sees any of it:

    export MODEL_URL=http://127.0.0.1:8080/v1/chat/completions   # yours
    export MODEL_NAME=my-local-model                             # yours
    export MODEL_API_KEY=...                # yours, if it needs one, in YOUR env
    bevis adapter add localmodel --cmd "python3 $PWD/examples/adapter-local-model.py"
    bevis doctor --adapter localmodel
    bevis run --adapter localmodel

Every line of network code in this workflow is in THIS file, which is yours.
The bevis package imports no HTTP library at all and could not make this call
if it wanted to (`tests/test_no_dependencies.py` asserts that). The registry
holds one name and one command line.
"""
import json, os, sys, urllib.request                                  # noqa: E401

URL = os.environ.get("MODEL_URL", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = os.environ.get("MODEL_NAME", "local-model")
KEY = os.environ.get("MODEL_API_KEY", "")
PROBE = os.environ.get("BEVIS_DOCTOR_PROBE") == "1"

prompt = "reply with the single word OK" if PROBE else (
    "%s\n\n%s\n\nThis is done when: %s"
    % (os.environ["BEVIS_JOB_TITLE"], os.environ.get("BEVIS_JOB_DESCRIPTION", ""),
       os.environ["BEVIS_JOB_ACCEPTANCE"]))
headers = {"Content-Type": "application/json"}
if KEY:
    headers["Authorization"] = "Bearer " + KEY
request = urllib.request.Request(URL, headers=headers, data=json.dumps(
    {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}).encode())
with urllib.request.urlopen(request, timeout=600) as response:
    reply = json.load(response)["choices"][0]["message"]["content"]

if PROBE:   # `bevis doctor --adapter <name>` — prove the server answers, cheaply
    sys.exit(print("%s answered as %s: %s" % (URL, MODEL, reply.strip()[:40])))
path = "bevis-job-%s.md" % os.environ["BEVIS_JOB_DISPLAY_ID"]
with open(path, "w", encoding="utf-8") as handle:
    handle.write(reply)
print("wrote %s (%d characters)" % (path, len(reply)))

# Exiting 0 says the model answered and the file was written. It does not say
# the job is done: bevis runs the job's checks next, and only they can close it.

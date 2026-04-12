import json
from pathlib import Path

p = Path(r"C:\Users\sumit\.codex\sessions\2026\04\11\rollout-2026-04-11T06-09-19-019d79fa-995b-7af3-b8c1-27ea9c39741a.jsonl")
with open(p, encoding="utf-8", errors="replace") as f:
    for i, line in enumerate(f):
        if i >= 5:
            break
        d = json.loads(line)
        etype = d.get("type", "?")
        payload = d.get("payload", {})
        print(f"--- Event {i}: type={etype} ---")
        print(f"  top keys: {list(d.keys())}")
        print(f"  payload keys: {list(payload.keys())[:10]}")
        if etype == "session_meta":
            print(f"  payload preview: {json.dumps(payload, default=str)[:300]}")
        if etype == "event_msg":
            msg = payload.get("message", payload)
            if isinstance(msg, dict):
                print(f"  msg keys: {list(msg.keys())[:8]}")
                print(f"  role: {msg.get('role')}")
                content = msg.get("content", "")
                if isinstance(content, list) and content:
                    print(f"  content[0] type: {type(content[0])}")
                    if isinstance(content[0], dict):
                        print(f"  content[0] keys: {list(content[0].keys())}")
                        print(f"  content[0] text[:80]: {content[0].get('text','')[:80]}")
                else:
                    print(f"  content[:80]: {str(content)[:80]}")

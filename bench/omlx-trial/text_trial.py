"""L3a text trial: extract four fields per ticket with schema-enforced output. No sampling params."""
import json
import sys
import time
import urllib.request
from pathlib import Path

trial = Path(sys.argv[1])
model = sys.argv[2]
out_path = Path(sys.argv[3])
key = json.load(open(Path.home() / ".omlx/settings.json"))["auth"]["api_key"]
schema = json.load(open(trial / "text/schema.json"))
items = [json.loads(l) for l in open(trial / "text/items.jsonl") if l.strip()]

SYSTEM = (
    "Extract fields from a customer-support ticket. product is one of router, camera, thermostat, "
    "doorbell, speaker. issue is one of setup, connectivity, billing, hardware, other. order_number "
    "is the order number exactly as written (format ORD-12345) or null if none is given; if the "
    "customer corrects it, use the corrected one. refund_requested is true only if the customer asks "
    "for their money back. Answer with the JSON object only."
)

rows, stats = [], []
for it in items:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": it["ticket"]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "ticket", "schema": schema, "strict": True}},
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.load(r)
        text = resp["choices"][0]["message"]["content"]
        pred = json.loads(text)
        ok_schema = True
        usage = resp.get("usage", {})
    except Exception as e:  # noqa: BLE001
        pred, ok_schema, usage = {}, False, {"error": type(e).__name__}
    dt = time.time() - t0
    pred["id"] = it["id"]
    rows.append(pred)
    stats.append({"id": it["id"], "seconds": round(dt, 1), "schema_ok": ok_schema, "usage": usage})
    print(it["id"], round(dt, 1), ok_schema, flush=True)

out_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
(out_path.with_suffix(".stats.json")).write_text(json.dumps(stats, indent=1))
print("schema_valid", sum(s["schema_ok"] for s in stats), "/", len(stats))
print("total_seconds", round(sum(s["seconds"] for s in stats), 1))

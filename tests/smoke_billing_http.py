"""Billing-on smoke test: full MCP flow over the ASGI app.

Verifies, with FILEFORGE_BILLING_MODE=on, in one process:
- anonymous call is charged against the free quota,
- a bogus API key is rejected,
- a keyed call spends a paid credit and returns a working download_url,
- a failing tool call refunds its credit,
- the Creem webhook route verifies signatures and grants credits,
- /v1/balance reports the result.

Run from the project root:
    PYTHONPATH=src .venv/bin/python tests/smoke_billing_http.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="fileforge-billing-smoke-"))

os.environ["FILEFORGE_BILLING_MODE"] = "on"
os.environ["FILEFORGE_DB_PATH"] = str(_TMP / "test.db")
os.environ["FILEFORGE_OUTPUT_DIR"] = str(_TMP / "output")
os.environ["FILEFORGE_INCOMING_DIR"] = str(_TMP / "incoming")
os.environ["FILEFORGE_DOWNLOAD_SECRET"] = "test-secret"
os.environ["FILEFORGE_PUBLIC_BASE_URL"] = "http://127.0.0.1:8765"
os.environ["CREEM_PRODUCT_STARTER_ID"] = "prod_starter_test"
os.environ["CREEM_WEBHOOK_SECRET"] = "whsec_test"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import httpx  # noqa: E402
from run_selftest import make_image, make_text_pdf  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    RESULTS.append(condition)


async def run() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from fileforge import billing
    from fileforge.server import mcp

    samples = _TMP / "samples"
    samples.mkdir()
    img = samples / "one.png"
    make_image(img, "steelblue", "IMG")
    img_b64 = base64.b64encode(img.read_bytes()).decode()
    text_pdf = samples / "text.pdf"
    make_text_pdf(text_pdf, "NO-TABLES-HERE")
    text_b64 = base64.b64encode(text_pdf.read_bytes()).decode()

    app = mcp.streamable_http_app()

    def factory(headers=None, **kwargs) -> httpx.AsyncClient:
        kwargs.pop("follow_redirects", None)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            follow_redirects=True,
            headers=headers,
            timeout=kwargs.get("timeout"),
        )

    async def call(headers, tool, args):
        async with streamablehttp_client(
            "http://127.0.0.1:8765/mcp", headers=headers, httpx_client_factory=factory
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args)

    async with app.router.lifespan_context(app):
        # 1. anonymous call -> free quota
        res = await call(None, "images_to_pdf", {"images_base64": [img_b64]})
        check("anon call succeeds", not res.isError, res.content[0].text[:100])
        info = json.loads(res.content[0].text)
        check(
            "anon charged 1 free credit",
            info["credits"]["free_used_this_month"] == 1,
            str(info["credits"]),
        )
        download_url = info["download_url"]
        check("download_url returned, local path hidden", "file_path" not in info and "/download/" in download_url)

        # 2. download route serves the PDF
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            r = await client.get(download_url.replace("http://127.0.0.1:8765", ""))
            check("download route returns the PDF", r.status_code == 200 and r.content.startswith(b"%PDF"), str(r.status_code))

        # 3. bogus key rejected
        res = await call({"Authorization": "Bearer ff_bogus"}, "images_to_pdf", {"images_base64": [img_b64]})
        check("bogus API key rejected", res.isError and "Invalid API key" in res.content[0].text, res.content[0].text[:100])

        # 4. keyed account: paid credit spent, failure refunded
        account_id = billing.create_account(email="smoke@example.com")
        key = billing.issue_api_key(account_id)
        billing.grant_credits(account_id, 10, "smoke_grant")
        headers = {"Authorization": f"Bearer {key}"}

        res = await call(headers, "images_to_pdf", {"images_base64": [img_b64]})
        check("keyed call succeeds", not res.isError, res.content[0].text[:100])
        check("paid credit spent (10 -> 9)", billing.balance(account_id)["paid_credits"] == 9)

        res = await call(headers, "pdf_tables_to_excel", {"pdf_base64": text_b64})
        check("table-less PDF errors", res.isError, res.content[0].text[:100])
        check("failed call refunded (still 9)", billing.balance(account_id)["paid_credits"] == 9)

        # 5. webhook route: bad signature 401, good signature credits the buyer
        payload = {
            "id": "evt_smoke_1",
            "eventType": "checkout.completed",
            "object": {
                "id": "ch_smoke",
                "order": {"id": "ord_smoke_1"},
                "product": {"id": "prod_starter_test", "name": "Starter"},
                "customer": {"email": "smoke@example.com"},
                "metadata": {},
            },
        }
        raw = json.dumps(payload).encode()
        secret = os.environ["CREEM_WEBHOOK_SECRET"]
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            r = await client.post("/webhooks/creem", content=raw, headers={"creem-signature": "0" * 64})
            check("webhook bad signature -> 401", r.status_code == 401, str(r.status_code))
            r = await client.post("/webhooks/creem", content=raw, headers={"creem-signature": sig})
            body = r.json()
            check("webhook credits buyer (+100 -> 109)", r.status_code == 200 and body.get("status") == "credited" and billing.balance(account_id)["paid_credits"] == 109, str(body))
            r = await client.get("/v1/balance", headers=headers)
            check("/v1/balance returns 109", r.status_code == 200 and r.json()["paid_credits"] == 109, r.text[:100])


def main() -> int:
    asyncio.run(run())
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

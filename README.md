# FileForge

Deterministic file processing for chat users, exposed as a remote MCP
server (Streamable HTTP). Built for directory distribution (Claude
Connectors Directory, Smithery, official MCP Registry, ChatGPT Apps):
the host platforms bring the users; FileForge sells credit packs.

Positioning: "File tools for your AI chat" - see `landing/index.html`.

## Architecture

Two layers, deliberately separated:

| Layer | File | Responsibility |
|---|---|---|
| Tools | `src/fileforge/tools.py` | Pure functions. Take local file paths, write results to `output/`, return a dict describing the produced file. No MCP imports - unit-testable, reusable behind a future web API. Page/pixel caps live here too (defence in depth). |
| Inbound | `src/fileforge/storage.py` + `limits.py` | Turns base64 payloads from remote clients into validated, short-lived local files: strict base64, size caps (PDF 25 MB, image 15 MB), magic-byte type sniffing, PDF page cap (500), image pixel cap (40 MP), password-protected PDFs rejected. |
| Transport | `src/fileforge/server.py` | FastMCP (streamable-http) wrapper. Tool signatures take base64 payloads; owns billing wrapper, rate limiting, per-call timeout, and the plain HTTP routes (`/health`, `/download/<token>`, `/webhooks/creem`, `/v1/balance`, `/v1/keys`). |
| Downloads | `src/fileforge/downloads.py` + `janitor.py` | Signed (HMAC-SHA256), expiring download tokens; expiry = file deletion time. The janitor deletes outputs after 24h (the landing page's privacy promise) and stray inputs after 1h; runnable standalone (`python -m fileforge.janitor`), as an in-server thread, or from a systemd timer. |
| Billing | `src/fileforge/billing.py` + `admin.py` | SQLite ledger. API keys (`Authorization: Bearer ff_...`, only hashes stored), 1 credit per call, paid credits spent first then 5 free calls/month (UTC), automatic refund when a call fails, Creem `checkout.completed` webhook (HMAC signature verified, idempotent by event id and order id) grants 100/300-credit packs; `refund.created` claws back. Admin CLI: `python -m fileforge.admin create-key/balance/grant/list-accounts`. Billing is OFF unless `FILEFORGE_BILLING_MODE=on`. |

Dependencies (mature OSS only): `mcp` (official Python SDK, pinned
`>=1.2,<2` - 2.x renamed FastMCP), `pymupdf`, `pdfplumber`, `openpyxl`,
`Pillow`.

## Tools

| Tool | Status | Notes |
|---|---|---|
| `pdf_tables_to_excel` | ✅ implemented + self-tested | pdfplumber table extraction → one xlsx sheet per table. Fails with a clear message when no ruled/aligned tables are detectable. |
| `merge_pdfs` | ✅ implemented + self-tested | pymupdf `insert_pdf`, order-preserving, requires ≥ 2 inputs. |
| `images_to_pdf` | ✅ implemented + self-tested | One image per page, page sized to image aspect at 200 DPI; converts non-RGB inputs. |
| `pdf_form_fill` | 🚧 interface only | Stub raising `NotImplementedError`, with an implementation sketch in its docstring. **Not registered on the server** - a permanently erroring tool would fail directory review. |

All tool names are ≤ 64 characters (Claude directory requirement) and
carry a human `title` plus a plain-language description.

## Run

```bash
cd fileforge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# boot the server (endpoint: http://127.0.0.1:8765/mcp)
PYTHONPATH=src .venv/bin/python -m fileforge.server
# key env: FILEFORGE_HOST, FILEFORGE_PORT, FILEFORGE_OUTPUT_DIR,
# FILEFORGE_PUBLIC_BASE_URL (enables download_url in results),
# FILEFORGE_DOWNLOAD_SECRET, FILEFORGE_BILLING_MODE=on,
# CREEM_WEBHOOK_SECRET, CREEM_PRODUCT_STARTER_ID, CREEM_PRODUCT_PRO_ID,
# FILEFORGE_ADMIN_TOKEN, FILEFORGE_JANITOR_ENABLE=1
# full list in src/fileforge/server.py's docstring.
```

## Self tests

```bash
# 1) tool-level tests: generate fixtures in samples/, call the tools,
#    re-open and verify the outputs
PYTHONPATH=src .venv/bin/python tests/run_selftest.py

# 2) transport smoke test: official MCP client against the real
#    Streamable HTTP app (initialize, tools/list, tools/call)
PYTHONPATH=src .venv/bin/python tests/smoke_http.py

# 3) remote-layer tests: storage validation, signed downloads, janitor,
#    billing ledger, Creem webhook (no network, throwaway DB)
PYTHONPATH=src .venv/bin/python tests/run_remote_tests.py

# 4) billing-on end-to-end: anon free quota, key auth, paid charge,
#    failure refund, signed download over HTTP, webhook credit grant
PYTHONPATH=src .venv/bin/python tests/smoke_billing_http.py
```

### Results (2026-11-02, v0.2 remote layer)

- `tests/run_selftest.py` - 5/5 passed (unchanged tool layer).
- `tests/run_remote_tests.py` - **26/26 passed**: base64 ingest
  (happy path + wrong-type/garbage/invalid/empty rejected), signed
  download tokens (round-trip, tamper, expiry, path traversal), janitor
  24h sweep, billing (key auth, free quota exhaustion, paid-first
  spending, exactly-once refund, anon buckets), Creem webhook
  (signature verify, +100 credits, duplicate event ignored, unknown
  product reported not guessed).
- `tests/smoke_http.py` - passed (new base64 tool signature).
- `tests/smoke_billing_http.py` - **12/12 passed** with billing on.

### Results (2026-11-02, this machine)

`tests/run_selftest.py` - 5/5 passed:

```
fixtures written to .../fileforge/samples
[PASS] pdf_tables_to_excel: xlsx exists and opens - table_tables-ac3cc14d.xlsx
[PASS] pdf_tables_to_excel: content matches sample table - A1='Item' B2='3' tables=1
[PASS] merge_pdfs: 2 pages, both markers present, in order - pages=2 size=1256B
[PASS] images_to_pdf: 2 pages, each page embeds an image - pages=2 size=366912B
[PASS] pdf_form_fill: stub raises NotImplementedError

5/5 checks passed
```

`tests/smoke_http.py` - passed:

```
tools listed over Streamable HTTP: ['images_to_pdf', 'merge_pdfs', 'pdf_tables_to_excel']
remote images_to_pdf call OK: {"file_name": "images-a66a082f.pdf", ...}
[PASS] streamable-http smoke test (ASGI transport, official MCP client)
```

Notes on the smoke test: this dev sandbox intercepts loopback TCP
between processes, so the smoke test drives the server's real ASGI app
(the same Starlette app uvicorn serves) via httpx's ASGI transport -
the full MCP protocol path is exercised, only raw TCP is bypassed.
Standalone boot was verified separately: `python -m fileforge.server`
starts uvicorn with "StreamableHTTP session manager started /
Application startup complete".

## Landing page

`landing/index.html` - static draft, no framework: one-line pitch, tool
list, credit-pack pricing (Free 5/mo; $6 = 100 credits; $15 = 300
credits; credits never expire), contact email, and full Privacy Policy /
Terms of Service sections (payment-processor review, e.g. Creem, requires
a visible contact email and legal pages on the site).

## TODO (next steps, in order)

1. **pdf_form_fill implementation** - pymupdf widgets; decide unknown
   field policy, checkbox/radio value conventions, flattening; add a
   self test with a real fillable form; only then register the tool.
2. **Deployment** - public HTTPS endpoint, isolated from other
   workloads on the target host (dedicated user/systemd service/port/
   subdomain, CPU/memory limits). Plan first, deploy after owner
   confirmation.
3. **Directory submission assets** - reviewer test account, listing
   copy, icon, `server.json`. Claude directory additionally requires:
   HTTPS endpoint, tool annotations (done), and a paid Claude plan on
   the submitting account. Free registries first (Smithery, official
   MCP Registry, Glama).
4. **Known MVP trade-offs to revisit** - (a) buyers claim their API
   key by email after purchase; add email verification before real
   volume. (b) Anonymous free quota is bucketed by client-IP hash;
   shared NATs share a bucket. (c) Claude/ChatGPT connector UIs may
   not let users set a Bearer header - paid users on those surfaces
   need OAuth later; the free tier works there today.

Done in v0.2: file ingress (base64 + limits), signed expiring download
links + 24h janitor, credit/billing layer with Creem webhook, and the
launch hardening set (payload caps, per-account rate limit 30/min,
120s per-call timeout, sanitised file names, page/pixel caps).

## Layout

```
fileforge/
├── pyproject.toml / requirements.txt
├── src/fileforge/
│   ├── __init__.py
│   ├── tools.py        # pure tool functions
│   ├── storage.py      # base64 ingest + validation
│   ├── limits.py       # central safety limits (env-tunable)
│   ├── downloads.py    # signed expiring download tokens
│   ├── janitor.py      # 24h expiry cleanup
│   ├── billing.py      # credit ledger, API keys, Creem webhook logic
│   ├── admin.py        # admin CLI (keys, balances, manual grants)
│   └── server.py       # FastMCP streamable-http transport + HTTP routes
├── tests/
│   ├── run_selftest.py      # tool-level tests (generates fixtures)
│   ├── run_remote_tests.py  # storage/downloads/billing/webhook tests
│   ├── smoke_http.py        # MCP-over-HTTP smoke test (billing off)
│   └── smoke_billing_http.py# MCP-over-HTTP smoke test (billing on)
├── landing/index.html  # static landing page draft
├── samples/            # generated test fixtures (gitignored)
├── incoming/           # materialised uploads, deleted after each call
├── data/               # billing sqlite DB (gitignored)
└── output/             # produced files, deleted after 24h (gitignored)
```

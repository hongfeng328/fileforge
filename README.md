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
| Tools | `src/fileforge/tools.py` | Pure functions. Take local file paths, write results to `output/`, return a dict describing the produced file. No MCP imports - unit-testable, reusable behind a future web API. |
| Transport | `src/fileforge/server.py` | Thin FastMCP (streamable-http) wrapper. Registers tools with titles, descriptions and annotations; owns host/port config. |

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
# env overrides: FILEFORGE_HOST, FILEFORGE_PORT, FILEFORGE_OUTPUT_DIR
```

## Self tests

```bash
# 1) tool-level tests: generate fixtures in samples/, call the tools,
#    re-open and verify the outputs
PYTHONPATH=src .venv/bin/python tests/run_selftest.py

# 2) transport smoke test: official MCP client against the real
#    Streamable HTTP app (initialize, tools/list, tools/call)
PYTHONPATH=src .venv/bin/python tests/smoke_http.py
```

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
2. **File ingress** - tools currently take local paths. For a remote
   server, clients must be able to get files in: accept base64 payloads
   or pre-signed upload URLs, with size/type limits.
3. **Download links & expiry cleanup** - return signed, expiring URLs
   for outputs instead of local paths; a janitor job deletes output
   files after the expiry window (privacy promise on the landing page
   depends on this).
4. **Credit/billing layer** - API-key auth per user, credit balance,
   one credit per tool call, free-tier monthly quota (5), Creem checkout
   for packs ($6/100, $15/300). Keep billing OUT of `tools.py`; enforce
   in the transport layer or a middleware.
5. **Directory submission assets** - reviewer test account, listing
   copy, icon. Claude directory additionally requires: HTTPS endpoint,
   tool annotations (done), and a paid Claude plan on the submitting
   account.
6. **Deployment isolation** - the target server also runs other,
   unrelated workloads. Deploy FileForge as its own systemd service
   under a dedicated user, bound
   to its own port, behind its own (sub)domain + HTTPS (Caddy/nginx);
   never share the output directory or env files with other services;
   set CPU/memory limits so a malformed PDF cannot starve neighbours.
7. **Hardening before public launch** - request size caps, per-key rate
   limits, timeouts per tool call, sanitize file names (done in
   `_output_path`), scan for zip-bomb-style PDFs (page/pixel caps).

## Layout

```
fileforge/
├── pyproject.toml / requirements.txt
├── src/fileforge/
│   ├── __init__.py
│   ├── tools.py        # pure tool functions
│   └── server.py       # FastMCP streamable-http transport
├── tests/
│   ├── run_selftest.py # tool-level tests (generates fixtures)
│   └── smoke_http.py   # MCP-over-HTTP smoke test
├── landing/index.html  # static landing page draft
├── samples/            # generated test fixtures (gitignored)
└── output/             # produced files (gitignored)
```

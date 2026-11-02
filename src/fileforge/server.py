"""FileForge MCP server - transport layer over fileforge.tools.

Runs a remote MCP server over Streamable HTTP (the transport the Claude
Connectors Directory and other registries require; SSE is deprecated).

Remote design (v0.2, 2026-11-02):
- Inputs arrive as base64 file payloads, never server-local paths.
  ``fileforge.storage`` validates size/type/structure before any tool
  runs; materialised inputs are deleted right after the call.
- Results are returned with a signed, expiring download URL
  (``fileforge.downloads``); files are deleted after 24h by the janitor,
  exactly as the privacy/landing pages promise.
- Billing lives here, not in the tools (``fileforge.billing``):
  FILEFORGE_BILLING_MODE=on charges 1 credit per call (paid credits
  first, then 5 free calls/month), refunds automatically on failure,
  and authenticates with ``Authorization: Bearer ff_...`` API keys.
  Anonymous callers get the free quota in an IP-hash bucket.
  Creem's checkout.completed webhook (POST /webhooks/creem) grants
  purchased credits; the signature is verified against
  CREEM_WEBHOOK_SECRET before anything is applied.

Run:
    PYTHONPATH=src .venv/bin/python -m fileforge.server
Environment:
    FILEFORGE_HOST / FILEFORGE_PORT   bind (default 127.0.0.1:8765)
    FILEFORGE_OUTPUT_DIR              result files
    FILEFORGE_INCOMING_DIR            materialised uploads
    FILEFORGE_PUBLIC_BASE_URL         e.g. https://files.example.com -
                                      enables download_url in results
    FILEFORGE_DOWNLOAD_SECRET         HMAC secret for download links
    FILEFORGE_BILLING_MODE            off (default, local dev) | on
    FILEFORGE_DB_PATH                 billing sqlite file
    CREEM_WEBHOOK_SECRET              enables POST /webhooks/creem
    CREEM_PRODUCT_STARTER_ID / CREEM_PRODUCT_PRO_ID
    FILEFORGE_ADMIN_TOKEN             enables POST /v1/keys (X-Admin-Token)
    FILEFORGE_JANITOR_ENABLE=1        hourly in-process cleanup thread

Endpoints: /mcp (MCP), /health, /download/<token>, /webhooks/creem,
/v1/balance (Bearer key), /v1/keys (admin).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from fileforge import billing, downloads, limits, storage, tools

BILLING_ON = os.environ.get("FILEFORGE_BILLING_MODE", "off").lower() == "on"

mcp = FastMCP(
    "FileForge",
    instructions=(
        "FileForge performs deterministic file processing: extract tables "
        "from a PDF into Excel, merge PDFs, combine images into a PDF, and "
        "fill fillable PDF forms. Send file contents as base64-encoded "
        "strings. Each call returns a download_url for the produced file; "
        "download links expire after 24 hours and files are then deleted. "
        "Each call costs 1 credit; the free plan includes 5 calls per month."
    ),
)

_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


# ---------------------------------------------------------------------------
# Identity, rate limiting, billing wrapper
# ---------------------------------------------------------------------------

_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request | None) -> str:
    if request is None:
        return "unknown"
    if os.environ.get("FILEFORGE_TRUST_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _resolve_account(ctx: Context) -> str:
    """Map the caller's credentials to a billing account id."""
    request = None
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
    except Exception:
        request = None
    auth = request.headers.get("authorization", "") if request is not None else ""
    if auth.lower().startswith("bearer "):
        account_id = billing.account_for_api_key(auth[7:].strip())
        if account_id is None:
            raise ValueError("Invalid API key. Check the key or buy a credit pack first.")
        return account_id
    return billing.anonymous_account_id(_client_ip(request))


def _check_rate_limit(identity: str) -> None:
    now = time.monotonic()
    with _RATE_LOCK:
        window = _RATE_WINDOWS[identity]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limits.RATE_LIMIT_PER_MINUTE:
            raise ValueError(
                f"Rate limit reached ({limits.RATE_LIMIT_PER_MINUTE} calls/minute). "
                "Please wait a moment and try again."
            )
        window.append(now)


async def _run_tool(ctx: Context, tool_name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Charge -> run with timeout -> refund on failure -> attach download URL."""
    account_id: str | None = None
    charge_ref: str | None = None
    if BILLING_ON:
        account_id = _resolve_account(ctx)
        _check_rate_limit(account_id)
        try:
            charge_ref = billing.charge(account_id, tool_name)
        except billing.BillingError as exc:
            raise ValueError(str(exc)) from None
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn), timeout=limits.TOOL_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        if BILLING_ON and account_id and charge_ref:
            billing.refund(account_id, charge_ref, tool_name)
        raise ValueError(
            f"Processing timed out after {limits.TOOL_TIMEOUT_SECONDS} seconds; "
            "no credit was charged."
        ) from None
    except BaseException:
        if BILLING_ON and account_id and charge_ref:
            billing.refund(account_id, charge_ref, tool_name)
        raise

    info = dict(result)
    url, expiry = downloads.download_url(info["file_name"])
    if url:
        info["download_url"] = url
        # Do not leak the server's filesystem layout to remote clients.
        info.pop("file_path", None)
    info["download_expires_at"] = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
    if BILLING_ON and account_id:
        info["credits"] = billing.balance(account_id)
    return info


# ---------------------------------------------------------------------------
# MCP tools (base64 in, download URL out)
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Extract PDF tables to Excel",
    description=(
        "Extract all tables detected in a PDF into an Excel (.xlsx) workbook, "
        "one worksheet per table. Input: the PDF file's contents as a "
        "base64-encoded string (max 25 MB, 500 pages; tables need visible "
        "ruling lines or clearly aligned columns). Returns a download_url "
        "for the .xlsx file (link valid 24 hours) plus the table count. "
        "Costs 1 credit; 5 free calls per month."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
async def pdf_tables_to_excel(ctx: Context, pdf_base64: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        path = storage.materialize(pdf_base64, "pdf")
        try:
            return tools.pdf_tables_to_excel(str(path))
        finally:
            storage.discard([path])

    return await _run_tool(ctx, "pdf_tables_to_excel", work)


@mcp.tool(
    title="Merge PDFs",
    description=(
        "Merge two or more PDF files into a single PDF, in the order given. "
        "Input: a list of base64-encoded PDF file contents (each max 25 MB / "
        "500 pages). Returns a download_url for the merged PDF (link valid "
        "24 hours), its size and page count. Costs 1 credit; 5 free calls "
        "per month."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
async def merge_pdfs(ctx: Context, pdfs_base64: list[str]) -> dict[str, Any]:
    if not pdfs_base64 or len(pdfs_base64) < 2:
        raise ValueError("merge_pdfs needs at least two PDF files.")

    def work() -> dict[str, Any]:
        paths = storage.materialize_many(pdfs_base64, "pdf")
        try:
            return tools.merge_pdfs([str(p) for p in paths])
        finally:
            storage.discard(paths)

    return await _run_tool(ctx, "merge_pdfs", work)


@mcp.tool(
    title="Combine images into a PDF",
    description=(
        "Combine one or more images (PNG/JPEG/WebP) into a single PDF, one "
        "image per page, pages sized to each image. Input: a list of "
        "base64-encoded image contents (each max 15 MB / 40 MP). Returns a "
        "download_url for the PDF (link valid 24 hours), its size and page "
        "count. Costs 1 credit; 5 free calls per month."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
async def images_to_pdf(ctx: Context, images_base64: list[str], dpi: int = 200) -> dict[str, Any]:
    dpi = max(72, min(600, int(dpi)))

    def work() -> dict[str, Any]:
        paths = storage.materialize_many(images_base64, "image")
        try:
            return tools.images_to_pdf([str(p) for p in paths], dpi=dpi)
        finally:
            storage.discard(paths)

    return await _run_tool(ctx, "images_to_pdf", work)


@mcp.tool(
    title="Fill a PDF form",
    description=(
        "Fill the fields of a fillable PDF form and return the completed "
        "form. Input: the PDF's contents as a base64-encoded string, plus "
        "a fields object mapping field names to values. Unknown field "
        "names are rejected with a list of the form's real fields. "
        "Checkboxes accept true/false; radio groups take the chosen "
        "option's name; dropdowns take one of their defined choices. "
        "Set flatten=true to bake values in so they can no longer be "
        "edited. Returns a download_url for the filled PDF (link valid "
        "24 hours). Costs 1 credit; 5 free calls per month."
    ),
    annotations=_WRITE_ANNOTATIONS,
)
async def pdf_form_fill(
    ctx: Context, pdf_base64: str, fields: dict[str, str], flatten: bool = False
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        path = storage.materialize(pdf_base64, "pdf")
        try:
            return tools.pdf_form_fill(str(path), fields, flatten=flatten)
        finally:
            storage.discard([path])

    return await _run_tool(ctx, "pdf_form_fill", work)


# ---------------------------------------------------------------------------
# Plain HTTP routes
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "fileforge", "billing": "on" if BILLING_ON else "off"}
    )


@mcp.custom_route("/download/{token}", methods=["GET"])
async def download(request: Request) -> Any:
    try:
        path = downloads.verify_token(request.path_params["token"])
    except ValueError as exc:
        message = str(exc)
        status = 410 if "expired" in message or "no longer" in message else 404
        return PlainTextResponse(message, status_code=status)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@mcp.custom_route("/webhooks/creem", methods=["POST"])
async def creem_webhook(request: Request) -> JSONResponse:
    secret = os.environ.get("CREEM_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"error": "webhook not configured"}, status_code=503)
    raw = await request.body()
    if not billing.verify_creem_signature(raw, request.headers.get("creem-signature"), secret):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    result = billing.process_creem_event(payload)
    return JSONResponse(result)


@mcp.custom_route("/v1/balance", methods=["GET"])
async def balance_route(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "missing API key"}, status_code=401)
    result = billing.dumps_balance_for_key(auth[7:].strip())
    if result is None:
        return JSONResponse({"error": "invalid API key"}, status_code=401)
    return JSONResponse(result)


@mcp.custom_route("/v1/keys", methods=["POST"])
async def issue_key_route(request: Request) -> JSONResponse:
    admin_token = os.environ.get("FILEFORGE_ADMIN_TOKEN", "")
    if not admin_token:
        return JSONResponse({"error": "key issuing not configured"}, status_code=503)
    provided = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(admin_token, provided):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        email = str(body["email"]).strip().lower()
    except Exception:
        return JSONResponse({"error": "body must be JSON with an email"}, status_code=400)
    account_id = billing.find_or_create_by_email(email)
    key = billing.issue_api_key(account_id, label=str(body.get("label", "")))
    return JSONResponse(
        {"account_id": account_id, "api_key": key, "balance": billing.balance(account_id)}
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _janitor_loop() -> None:
    from fileforge import janitor

    while True:
        time.sleep(3600)
        try:
            janitor.cleanup_expired()
        except Exception:
            pass  # the systemd timer is the backstop; never kill the server


def main() -> None:
    host = os.environ.get("FILEFORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("FILEFORGE_PORT", "8765"))
    mcp.settings.host = host
    mcp.settings.port = port
    # DNS-rebinding protection: behind the reverse proxy the public Host
    # header is our domain, which the framework's localhost-only default
    # would reject ("Invalid Host header"). Allow the public host from
    # FILEFORGE_PUBLIC_BASE_URL alongside loopback for local health use.
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
    public_host = urlparse(os.environ.get("FILEFORGE_PUBLIC_BASE_URL", "")).netloc
    if public_host:
        allowed_hosts.append(public_host)
        allowed_origins += [f"https://{public_host}", f"http://{public_host}"]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    if downloads.EPHEMERAL_SECRET:
        print(
            "WARNING: FILEFORGE_DOWNLOAD_SECRET is not set; download links "
            "will stop working on restart."
        )
    if os.environ.get("FILEFORGE_JANITOR_ENABLE") == "1":
        threading.Thread(target=_janitor_loop, daemon=True, name="fileforge-janitor").start()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

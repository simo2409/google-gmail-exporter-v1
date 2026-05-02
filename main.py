#!/usr/bin/env python3
"""Fetch emails from Gmail based on searches defined in config.json.

Output structure per email (when both save_json and save_html are enabled):
  {output_path}/{stem}.json        — full metadata + body
  {output_path}/{stem}/
      {stem}.html                  — HTML with src rewritten to local filenames
      {image_hash}.{ext}           — downloaded images (external URLs, data URIs, CID)
"""

import base64
import hashlib
import json
import mimetypes
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).parent
_LLMWIKI_CONFIG_DIR = Path.home() / ".config" / "llmwiki" / "obs-llmwiki-simone-personal-v1"
CREDENTIALS_FILE = _LLMWIKI_CONFIG_DIR / "credentials.json"
TOKEN_FILE = _LLMWIKI_CONFIG_DIR / "token-gmail.json"
CONFIG_FILE = BASE_DIR / "config.json"

_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


def _execute_with_retry(request, max_retries: int = 5):
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status in _RETRYABLE_STATUSES and attempt < max_retries - 1:
                print(f"  API error {exc.resp.status}, retrying in {delay:.1f}s…")
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise


MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def mime_to_ext(mime: str) -> str:
    mime = mime.split(";")[0].strip().lower()
    return MIME_EXT.get(mime, mimetypes.guess_extension(mime) or ".bin")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> list[dict]:
    if not CONFIG_FILE.exists():
        print(f"ERROR: config.json not found at {CONFIG_FILE}")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text())
    searches = cfg.get("searches", [])
    if not searches:
        print("ERROR: no searches defined in config.json")
        sys.exit(1)
    return searches


def resolve_output_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(
                    "ERROR: credentials.json not found.\n"
                    "Download it from Google Cloud Console:\n"
                    "  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON\n"
                    f"Place it at: {CREDENTIALS_FILE}\n"
                    f"(shared across all llmwiki utils — create the directory if needed)"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds


# ---------------------------------------------------------------------------
# Gmail message parsing
# ---------------------------------------------------------------------------

def decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


def extract_body(payload: dict) -> dict[str, str]:
    mime = payload.get("mimeType", "")
    parts = payload.get("parts", [])
    if mime == "text/plain":
        return {"text": decode_body(payload), "html": ""}
    if mime == "text/html":
        return {"text": "", "html": decode_body(payload)}
    text, html = "", ""
    for part in parts:
        sub = extract_body(part)
        text = text or sub["text"]
        html = html or sub["html"]
    return {"text": text, "html": html}


def extract_cid_parts(payload: dict, service, msg_id: str) -> dict[str, tuple[bytes, str]]:
    """Return {content-id: (bytes, mime_type)} for all inline image attachments."""
    cid_map: dict[str, tuple[bytes, str]] = {}

    def walk(part: dict) -> None:
        hdrs = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
        content_id = hdrs.get("content-id", "").strip("<>")
        attachment_id = part.get("body", {}).get("attachmentId")
        if content_id and attachment_id:
            att = _execute_with_retry(
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=attachment_id)
            )
            cid_map[content_id] = (
                base64.urlsafe_b64decode(att["data"]),
                part.get("mimeType", "image/png"),
            )
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return cid_map


def header_value(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def safe_filename(value: str, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", value).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:max_len] if cleaned else "no_subject"


# ---------------------------------------------------------------------------
# Image downloading
# ---------------------------------------------------------------------------

def save_image(data: bytes, mime: str, folder: Path) -> str:
    ext = mime_to_ext(mime)
    name = hashlib.md5(data).hexdigest() + ext
    (folder / name).write_bytes(data)
    return name


def download_and_localize_images(
    html: str,
    folder: Path,
    cid_parts: dict[str, tuple[bytes, str]],
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; email-fetcher/1.0)"})

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue

        local_name: str | None = None

        if src.startswith("data:"):
            try:
                header, b64data = src.split(",", 1)
                mime = header.split(":")[1].split(";")[0]
                local_name = save_image(base64.b64decode(b64data), mime, folder)
            except Exception as exc:
                print(f"  Warning: could not save data-URI image: {exc}")

        elif src.startswith("cid:"):
            cid = src[4:].strip()
            if cid in cid_parts:
                img_bytes, mime = cid_parts[cid]
                local_name = save_image(img_bytes, mime, folder)

        elif src.startswith(("http://", "https://")):
            try:
                resp = session.get(src, timeout=10, allow_redirects=True)
                if resp.ok:
                    mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    local_name = save_image(resp.content, mime, folder)
            except Exception as exc:
                print(f"  Warning: could not download image {src[:80]!r}: {exc}")

        if local_name:
            img["src"] = local_name

    return str(soup)


# ---------------------------------------------------------------------------
# Save logic
# ---------------------------------------------------------------------------

def save_email(
    msg: dict,
    service,
    output_dir: Path,
    label: str,
    save_json: bool,
    save_html: bool,
) -> None:
    msg_id = msg["id"]
    headers = msg.get("payload", {}).get("headers", [])
    subject = header_value(headers, "Subject") or "no_subject"
    stem = f"{msg_id}_{safe_filename(subject)}"
    body = extract_body(msg.get("payload", {}))

    if save_json:
        email_data = {
            "id": msg_id,
            "threadId": msg.get("threadId"),
            "date": header_value(headers, "Date"),
            "from": header_value(headers, "From"),
            "subject": subject,
            "snippet": msg.get("snippet", ""),
            "body_text": body["text"],
            "body_html": body["html"],
            "labels": msg.get("labelIds", []),
        }
        (output_dir / f"{stem}.json").write_text(
            json.dumps(email_data, indent=2, ensure_ascii=False)
        )

    if save_html and body["html"]:
        folder = output_dir / stem
        folder.mkdir(exist_ok=True)
        cid_parts = extract_cid_parts(msg.get("payload", {}), service, msg_id)
        local_html = download_and_localize_images(body["html"], folder, cid_parts)
        (folder / f"{stem}.html").write_text(local_html, encoding="utf-8")

    print(f"  {label} Saved: {stem}")


# ---------------------------------------------------------------------------
# Skip / completion index
# ---------------------------------------------------------------------------

def build_saved_index(output_dir: Path) -> dict[str, dict]:
    """
    Return {msg_id: {'json': Path, 'folder': Path}} for already-saved emails.
    A folder counts only if it contains an HTML file (guards against partial writes).
    """
    saved: dict[str, dict] = {}
    if not output_dir.exists():
        return saved
    for item in output_dir.iterdir():
        msg_id = item.name.split("_")[0]
        if item.is_file() and item.suffix == ".json":
            saved.setdefault(msg_id, {})["json"] = item
        elif item.is_dir() and list(item.glob("*.html")):
            saved.setdefault(msg_id, {})["folder"] = item
    return saved


def needs_fetch(entry: dict, save_json: bool, save_html: bool, msg_id: str) -> bool:
    """Return True if the email still needs to be fetched from the API."""
    has_json = "json" in entry
    has_folder = "folder" in entry

    # Both outputs present → fully done
    if (not save_json or has_json) and (not save_html or has_folder):
        return False

    # JSON present, HTML folder missing: check whether body_html is actually empty
    if save_html and not has_folder and has_json:
        data = json.loads(entry["json"].read_text())
        if not data.get("body_html"):
            return False  # email is text-only, nothing to fetch for HTML

    return True


# ---------------------------------------------------------------------------
# Message listing
# ---------------------------------------------------------------------------

def list_messages(service, query: str, max_results: int | None) -> list[dict]:
    messages: list[dict] = []
    page_token = None
    while True:
        remaining = None if max_results is None else max_results - len(messages)
        if remaining is not None and remaining <= 0:
            break
        page_size = min(500, remaining) if remaining is not None else 500
        kwargs: dict = {"userId": "me", "q": query, "maxResults": page_size}
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute_with_retry(service.users().messages().list(**kwargs))
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages if max_results is None else messages[:max_results]


# ---------------------------------------------------------------------------
# Per-search runner
# ---------------------------------------------------------------------------

def run_search(search: dict, service) -> None:
    sender: str = search["sender"]
    max_results: int | None = search.get("max_results") or None
    output_dir = resolve_output_path(search.get("output_path", "emails"))
    save_json: bool = search.get("save_json", True)
    save_html: bool = search.get("save_html", True)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'─' * 60}")
    print(f"Sender      : {sender}")
    print(f"Max results : {max_results or 'unlimited'}")
    print(f"Output      : {output_dir}")
    print(f"Save        : {'json ' if save_json else ''}{'html' if save_html else ''}")
    print(f"{'─' * 60}")

    messages = list_messages(service, f"from:{sender}", max_results)
    total = len(messages)
    if total == 0:
        print("No emails found.")
        return

    print(f"Found {total} email(s). Checking existing files...")
    saved = build_saved_index(output_dir)
    skipped = fetched = 0

    for i, msg_ref in enumerate(messages, 1):
        msg_id = msg_ref["id"]
        entry = saved.get(msg_id, {})
        label = f"[{i}/{total}]"

        if not needs_fetch(entry, save_json, save_html, msg_id):
            print(f"  {label} Skipped: {msg_id}")
            skipped += 1
            continue

        msg = _execute_with_retry(
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
        )
        save_email(msg, service, output_dir, label, save_json, save_html)
        fetched += 1

    print(f"\nDone. {fetched} processed, {skipped} skipped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    searches = load_config()
    creds = authenticate()
    service = build("gmail", "v1", credentials=creds)

    for search in searches:
        run_search(search, service)


if __name__ == "__main__":
    main()

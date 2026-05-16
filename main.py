#!/usr/bin/env python3
"""Fetch emails from Gmail based on searches defined in config.json.

Output structure per email (when both save_json and save_html are enabled):
  {output_path}/{stem}.json        — full metadata + body
  {output_path}/{stem}/
      {stem}.html                  — HTML with src rewritten to local filenames
      {image_hash}.{ext}           — downloaded images (external URLs, data URIs, CID)
      links/
          {url_hash}_{slug}.md     — markdown for each link in the email body
"""

import base64
import hashlib
import json
import mimetypes
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).parent
_LLMWIKI_CONFIG_DIR = Path.home() / ".config" / "llmwiki" / "obs-llmwiki-simone-personal-v1"

_LOCAL_CREDENTIALS_FILE = BASE_DIR / "credentials.json"
_LOCAL_TOKEN_FILE = BASE_DIR / "token.json"
_DEFAULT_CREDENTIALS_FILE = _LLMWIKI_CONFIG_DIR / "credentials.json"
_DEFAULT_TOKEN_FILE = _LLMWIKI_CONFIG_DIR / "token-gmail.json"

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

def _resolve_auth_files() -> tuple[Path, Path]:
    """Return (credentials_file, token_file).

    Uses the local credentials.json (next to this script) when present;
    otherwise falls back to the shared llmwiki config directory.
    The token file always lives alongside the credentials file it was derived from.
    """
    if _LOCAL_CREDENTIALS_FILE.exists():
        return _LOCAL_CREDENTIALS_FILE, _LOCAL_TOKEN_FILE
    return _DEFAULT_CREDENTIALS_FILE, _DEFAULT_TOKEN_FILE


def authenticate() -> Credentials:
    credentials_file, token_file = _resolve_auth_files()
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_file.exists():
                print(
                    "ERROR: credentials.json not found.\n"
                    "Download it from Google Cloud Console:\n"
                    "  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON\n"
                    f"Place it locally at: {_LOCAL_CREDENTIALS_FILE}\n"
                    f"  or at the shared location: {_DEFAULT_CREDENTIALS_FILE}\n"
                    f"(create the directory if needed)"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
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
# Link fetching
# ---------------------------------------------------------------------------

def extract_links_from_html(html: str) -> list[str]:
    """Return unique http/https hrefs from <a> tags, preserving order."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def _normalize_url(url: str) -> str:
    """Strip query string and fragment — same article, different tracking params = same content."""
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def _url_to_slug(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"[^\w/-]", "", parsed.path).strip("/")
    slug = re.sub(r"[/_-]+", "-", path)
    return slug[:60] if slug else parsed.netloc[:60]


def fetch_url_as_markdown(url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        if not resp.ok:
            print(f"  Warning: HTTP {resp.status_code} fetching {url[:80]!r}")
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type:
            print(f"  Skipping non-HTML link ({content_type}): {url[:80]!r}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else url
        body = soup.find("article") or soup.find("main") or soup.body
        if body is None:
            return None
        markdown_body = md(str(body), heading_style="ATX", bullets="-")
        return f"# {title}\n\nSource: {url}\n\n{markdown_body}"
    except Exception as exc:
        print(f"  Warning: could not fetch {url[:80]!r}: {exc}")
        return None


def save_links_as_markdown(html: str, folder: Path) -> None:
    links = extract_links_from_html(html)
    if not links:
        return
    links_dir = folder / "links"
    links_dir.mkdir(exist_ok=True)
    # cache lives at output_dir/_url_cache so the same URL is never fetched twice
    # across all emails in this search, even across multiple runs
    cache_dir = folder.parent / "_url_cache"
    cache_dir.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; email-fetcher/1.0)"})
    for url in links:
        normalized = _normalize_url(url)
        url_hash = hashlib.md5(normalized.encode()).hexdigest()[:8]
        slug = _url_to_slug(normalized)
        filename = f"{url_hash}_{slug}.md" if slug else f"{url_hash}.md"
        dest = links_dir / filename
        if dest.exists():
            continue
        cached = cache_dir / filename
        if cached.exists():
            dest.write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"    From cache: {filename}")
            continue
        content = fetch_url_as_markdown(url, session)
        if content is not None:
            cached.write_text(content, encoding="utf-8")
            dest.write_text(content, encoding="utf-8")
            print(f"    Saved link: {filename}")


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
    fetch_links: bool,
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

    if (save_html or fetch_links) and body["html"]:
        folder = output_dir / stem
        folder.mkdir(exist_ok=True)
        if save_html:
            cid_parts = extract_cid_parts(msg.get("payload", {}), service, msg_id)
            local_html = download_and_localize_images(body["html"], folder, cid_parts)
            (folder / f"{stem}.html").write_text(local_html, encoding="utf-8")
        if fetch_links:
            save_links_as_markdown(body["html"], folder)

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
    fetch_links: bool = search.get("fetch_links", False)

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'─' * 60}")
    print(f"Sender      : {sender}")
    print(f"Max results : {max_results or 'unlimited'}")
    print(f"Output      : {output_dir}")
    print(f"Save        : {'json ' if save_json else ''}{'html ' if save_html else ''}{'links' if fetch_links else ''}")
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
            skipped += 1
            continue

        msg = _execute_with_retry(
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
        )
        save_email(msg, service, output_dir, label, save_json, save_html, fetch_links)
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

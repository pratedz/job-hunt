"""Company logos, downloaded once and served locally (M9).

The browser never asks the internet for a logo: the backend fetches it with the same
polite, cookie-less fetcher as everything else, keeps it under ``data/logos/`` and the
board serves it from 127.0.0.1.

Only raster images are stored, and the type is decided by **magic bytes** - an SVG is
never written, whatever its name says, because an SVG can carry script. Size cap 200 KB,
one file per normalised company name, and a failure is retried at most once a week.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unicodedata
import urllib.parse
import zlib
from pathlib import Path

from . import store
from .http import HostStopped

MAX_BYTES = 200 * 1024
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"\x00\x00\x01\x00", "ico"),
)
#: The only names ``/logos/`` serves: what ``file_name`` writes, nothing else.
SERVED_NAME = re.compile(r"^[0-9a-f]{16}\.(png|jpg|gif|webp|ico)$")
CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
                 "webp": "image/webp", "ico": "image/x-icon"}


def image_kind(data: bytes) -> str | None:
    """The raster type from the first bytes, or None (an SVG, HTML or anything else)."""
    if not data:
        return None
    for signature, extension in SIGNATURES:
        if data.startswith(signature):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def logos_dir(cfg) -> Path:
    return cfg.data_dir / "logos"


def logo_storage_key(company: str | None, company_norm: str | None = None) -> str | None:
    """Key for the logos table only. Job dedupe still uses ``company_norm``.

    A name ``normalise_text`` can fold keeps that folded key, so existing rows stay put.
    A name it drops, such as a Thai-only legal name, gets ``raw:<sha1>`` of the NFC
    display string. That key is stable and cannot collide with a folded Latin name.
    """
    if company_norm:
        return company_norm
    folded = store.normalise_text(company)
    if folded:
        return folded
    text = unicodedata.normalize("NFC", (company or "").strip())
    if not text:
        return None
    digest = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()[:16]
    return f"raw:{digest}"


def file_name(company_norm: str, extension: str) -> str:
    digest = hashlib.sha1((company_norm or "").encode("utf-8")).hexdigest()[:16]
    return f"{digest}.{extension}"


def stored_path(cfg, name: str) -> Path:
    """Resolve a served logo name inside data/logos/ and nowhere else."""
    if not SERVED_NAME.match(name or ""):
        raise ValueError("that is not a logo file name")
    candidate = (logos_dir(cfg) / name).resolve()
    root = logos_dir(cfg).resolve()
    if candidate.parent != root:
        raise ValueError("logo path escapes data/logos/")
    return candidate


def download(cfg, conn, fetcher, company_norm: str, company: str | None, url: str,
             log=None, remember_failure: bool = True) -> str | None:
    """Fetch one logo once. Returns the stored file name, or None on any refusal.

    A refusal is recorded (retried after 7 days) unless ``remember_failure`` is false,
    which a caller uses when it still has another official URL to try.
    ``HostStopped`` is raised unrecorded, so a capped or stopped host costs the company nothing.
    """
    log = log or (lambda *a, **k: None)

    def refuse(error: str) -> None:
        if remember_failure:
            store.record_logo_failure(conn, company_norm, url, error=error)
        log(f"logo {company}: {error}")

    if urllib.parse.urlsplit(url or "").scheme != "https":
        # data from a search page decides this URL: never let it point curl at file://,
        # a plain-http host or anything else
        refuse("not an https URL")
        return None
    try:
        response = fetcher.get(url, accept="image/*")
    except HostStopped:
        raise       # the host's budget for this run is spent: not this logo's failure
    except Exception as exc:
        refuse(f"{type(exc).__name__}: {exc}")
        return None
    if not response.ok:
        refuse(f"HTTP {response.status}")
        return None

    body = response.raw if isinstance(response.raw, bytes) else b""
    body = _within_cap(body)
    if body is None:
        refuse(f"too large ({len(response.raw) if isinstance(response.raw, bytes) else 0} bytes)")
        return None
    kind = image_kind(body)
    if kind is None:
        refuse("not a raster image")
        return None

    directory = logos_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    name = file_name(company_norm, kind)
    target = directory / name
    handle = tempfile.NamedTemporaryFile("wb", dir=str(directory), prefix=".logo-",
                                         suffix=".tmp", delete=False)
    try:
        handle.write(body)
        handle.close()
        os.replace(handle.name, target)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    store.set_logo(conn, company_norm, name, url)
    log(f"logo {company}: stored {name} ({len(body)} bytes)")
    return name


def fetch_missing(cfg, conn, fetcher, limit: int = 30, log=None) -> dict:
    """Download logos for companies that have a URL and no cached file yet."""
    log = log or (lambda *a, **k: None)
    rows = store.logo_candidates(conn, limit=limit)
    report = {"considered": len(rows), "stored": 0, "refused": 0, "deferred": 0}
    for row in rows:
        try:
            name = download(cfg, conn, fetcher, row["company_norm"], row["company"],
                            row["url"], log=log)
        except HostStopped as exc:
            log(f"logo {row['company']}: {exc}")
            report["deferred"] += 1         # retried on the next run
            continue
        if name:
            report["stored"] += 1
        else:
            report["refused"] += 1
    return report


def url_for(conn, company_norm: str | None) -> str | None:
    """`/logos/<file>` for a company with a cached logo, else None."""
    if not company_norm:
        return None
    row = store.get_logo(conn, company_norm)
    return f"/logos/{row['file']}" if row is not None and row["file"] else None


# --------------------------------------------------------------------------- #
# image size (to prefer a logo of at least 64 px)
# --------------------------------------------------------------------------- #

GOOD_SIZE = 64


def image_size(data: bytes) -> int | None:
    """The larger side in pixels for PNG, GIF and ICO (the largest entry), else None."""
    kind = image_kind(data)
    if kind == "png" and len(data) >= 24:
        return max(int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if kind == "gif" and len(data) >= 10:
        return max(int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
    if kind == "ico" and len(data) >= 6:
        count = int.from_bytes(data[4:6], "little")
        sides = []
        for index in range(min(count, 64)):
            entry = data[6 + 16 * index: 6 + 16 * index + 2]
            if len(entry) < 2:
                break
            sides.extend(256 if value == 0 else value for value in entry)
        return max(sides) if sides else None
    return None


# --------------------------------------------------------------------------- #
# configured companies: name matching and their own site icon
# --------------------------------------------------------------------------- #

#: Words that say nothing about which company it is: legal forms, places, group words.
NOISE_WORDS = {
    "co", "company", "ltd", "limited", "public", "pcl", "plc", "inc", "incorporated", "corp",
    "corporation", "llc", "pte", "sdn", "bhd", "the", "thailand", "singapore", "asia", "apac",
    "th", "sg", "hk", "international", "group", "holdings",
}


def core_tokens(name: str | None) -> tuple[str, ...]:
    """A company name as whole normalised tokens, without brackets and noise words.

    "Shopee (Thailand) Co., Ltd." -> ("shopee",); "Marriott International" -> ("marriott",).
    """
    text = re.sub(r"\([^)]*\)", " ", name or "")
    return tuple(word for word in (store.normalise_text(text) or "").split()
                 if word not in NOISE_WORDS)


def _name_parts(name: str | None) -> list[str]:
    """"TikTok / TikTok Shop / ByteDance" and "Funding Societies | Modalku" are several names."""
    return [part for part in re.split(r"[/|]", name or "") if part.strip()]


def logo_sources(cfg) -> list[dict]:
    """Every company -> domain the lead configured: ``[[companies]].domain`` first, then the
    exact names of ``[logo_domains]`` (marked ``exact``)."""
    sources = [dict(entry) for entry in (cfg.get("companies", []) or []) if entry.get("domain")]
    for name, domain in (cfg.get("logo_domains", {}) or {}).items():
        if isinstance(domain, str) and domain.strip():
            sources.append({"name": name, "domain": domain, "exact": True})
    return sources


def configured_domains(cfg) -> list[str]:
    return list(dict.fromkeys(str(entry["domain"]).strip().casefold()
                              for entry in logo_sources(cfg)))


def logo_assets(cfg) -> dict[str, str]:
    """Optional ``[logo_assets]`` map: domain → one https URL of that company's own logo.

    Used when the home page is a script shell or refuses the fetcher, and a public
    raster on the company's own site or its official CDN is already known. Values
    that are not https are ignored.
    """
    raw = cfg.get("logo_assets", {}) or {}
    assets = {}
    if not isinstance(raw, dict):
        return assets
    for domain, url in raw.items():
        if isinstance(url, str) and urllib.parse.urlsplit(url.strip()).scheme == "https":
            assets[str(domain).strip().casefold()] = url.strip()
    return assets


class CompanyMatcher:
    """Matches a company name to a configured domain, on **whole tokens only**.

    1. ``[logo_domains]`` names, exactly (after normalisation).
    2. The core tokens of the name, or of any ``/``-separated part of it, **equal** to the core
       tokens of a configured name or one of its parts. "Unilever Thailand", "Shopee (Thailand)
       Co., Ltd." and "Marriott International" match; "Sea Logistics", "Central Retail" and
       "Seagate" do not match Sea or Central Group.
    """

    def __init__(self, sources):
        self.exact: dict[str, str] = {}
        self.cores: dict[tuple[str, ...], str] = {}
        for entry in sources or []:
            domain = str(entry.get("domain") or "").strip().casefold()
            name = str(entry.get("name") or "")
            if not domain:
                continue
            if entry.get("exact"):
                key = store.normalise_text(name)
                if key:
                    self.exact.setdefault(key, domain)
            for part in _name_parts(name):
                core = core_tokens(part)
                if core:
                    self.cores.setdefault(core, domain)    # [[companies]] come first

    def domain_for(self, company: str | None) -> str | None:
        key = store.normalise_text(company)
        if not key:
            return None
        if key in self.exact:
            return self.exact[key]
        for part in [company] + _name_parts(company):
            core = core_tokens(part)
            if core and core in self.cores:
                return self.cores[core]
        return None


def domain_key(domain: str) -> str:
    """The ``logos`` row key of a company site's own icon."""
    return f"@{domain}"


# Public suffixes long enough that the brand label is the third label from the right.
_MULTI_LABEL_SUFFIXES = {
    "co.th", "ac.th", "or.th", "go.th", "in.th", "co.uk", "org.uk", "gov.uk",
    "com.au", "net.au", "org.au", "com.sg", "com.hk", "com.my", "co.id", "co.jp",
    "com.br", "co.nz", "com.tw", "gov.sg", "com.ph", "co.kr", "com.vn",
}


def registrable_site(host: str | None) -> str:
    """The site a host belongs to: ``www.lazada.co.th`` and ``lazada.co.th`` are one site."""
    host = (host or "").casefold().split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def stayed_on_site(domain: str, url: str | None) -> bool:
    """True when ``url`` is still on ``domain``'s site, including its subdomains."""
    host = urllib.parse.urlsplit(url or "").hostname
    return bool(host) and registrable_site(host) == registrable_site(domain)


def _within_cap(body: bytes) -> bytes | None:
    """The bytes to store, or None when they are over 200 KB and cannot be scaled down.

    A genuine posting logo sometimes arrives larger than the cap. Pillow scales it to
    256 px; macOS ``sips`` is the fallback when Pillow is not installed. Anything that
    is not a raster, or that is still too big, is refused.
    """
    if len(body) <= MAX_BYTES:
        return body
    if image_kind(body) is None:
        return None
    fitted = _shrink_raster(body)
    if fitted is None or len(fitted) > MAX_BYTES or image_kind(fitted) is None:
        return None
    return fitted


def _shrink_raster(body: bytes, max_side: int = 256) -> bytes | None:
    """Scale a raster to ``max_side``. Pillow first, then macOS ``sips``.

    Pillow is the portable path (Windows and WSL included): ``pip install pillow``.
    ``sips`` is only a fallback on a Mac where Pillow is not installed. If neither
    can decode the file, the caller refuses it. Nothing is stored from magic bytes alone.
    """
    kind = image_kind(body)
    if kind is None:
        return None
    fitted = _shrink_with_pillow(body, max_side) or _shrink_with_sips(body, kind, max_side)
    if fitted is None or image_kind(fitted) != "png" or len(fitted) >= len(body):
        return None
    return fitted


def _shrink_with_pillow(body: bytes, max_side: int) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io_bytes(body)) as image:
            image.load()
            image.thumbnail((max_side, max_side))
            frame = image.convert("RGBA") if "A" in image.getbands() else image.convert("RGB")
            handle = io_bytes()
            frame.save(handle, format="PNG")
            return handle.getvalue()
    except Exception:
        return None


def io_bytes(body: bytes | None = None):
    import io
    return io.BytesIO(body) if body is not None else io.BytesIO()


def _shrink_with_sips(body: bytes, kind: str, max_side: int) -> bytes | None:
    suffix = ".jpg" if kind == "jpg" else f".{kind}"
    try:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / f"logo{suffix}"
            target = Path(directory) / "logo.png"
            source.write_bytes(body)
            proc = subprocess.run(
                ["sips", "-s", "format", "png", "-Z", str(max_side),
                 str(source), "--out", str(target)],
                capture_output=True, timeout=20)
            if proc.returncode != 0 or not target.is_file():
                return None
            return target.read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return None


def raster_decodes(data: bytes) -> bool:
    """True when the bytes are a raster Pillow or a strict PNG/JPEG parse can open.

    Magic bytes alone are not enough: a PNG header plus padding does not decode.
    """
    kind = image_kind(data)
    if kind is None:
        return False
    if _pillow_decodes(data):
        return True
    if kind == "png":
        return _png_payload_decodes(data)
    if kind == "jpg":
        return _jpeg_has_frame(data)
    if kind == "ico":
        # Some favicons are a tight 32-bit DIB. Pillow rejects the mask, browsers still draw them.
        return _ico_directory_decodes(data)
    return False


def _ico_directory_decodes(data: bytes) -> bool:
    if len(data) < 22 or data[:4] != b"\x00\x00\x01\x00":
        return False
    count = int.from_bytes(data[4:6], "little")
    if count < 1 or 6 + 16 * count > len(data):
        return False
    for index in range(count):
        entry = data[6 + 16 * index:22 + 16 * index]
        size = int.from_bytes(entry[8:12], "little")
        offset = int.from_bytes(entry[12:16], "little")
        if size < 16 or offset < 6 or offset + size > len(data):
            continue
        payload = data[offset:offset + 4]
        if payload.startswith(b"\x89PNG") or payload == b"\x28\x00\x00\x00":
            return True
    return False


def _pillow_decodes(data: bytes) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(io_bytes(data)) as image:
            image.load()
        return True
    except Exception:
        return False


def _png_payload_decodes(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    idat = bytearray()
    width = height = None
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        tag = data[pos + 4:pos + 8]
        start = pos + 8
        end = start + length
        if length < 0 or end + 4 > len(data):
            return False
        chunk = data[start:end]
        pos = end + 4
        if tag == b"IHDR" and len(chunk) >= 13:
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            if chunk[8] != 8 or chunk[12] != 0 or not width or not height:
                return False
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break
    if not idat or not width or not height:
        return False
    try:
        zlib.decompress(bytes(idat))
    except zlib.error:
        return False
    return True


def _jpeg_has_frame(data: bytes) -> bool:
    if len(data) < 4 or data[:3] != b"\xff\xd8\xff":
        return False
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if index + 9 > len(data):
                return False
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width > 0 and height > 0
        if marker == 0xD9:
            return False
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(data):
            return False
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return False
        index += 2 + length
    return False


def clear_undecodable_logos(cfg, conn) -> list[str]:
    """Drop logo rows whose file is missing or does not decode. Returns those keys.

    A ``/logos/`` URL with no decodable file is not coverage. The source URL is kept
    so a later fetch can try again; the file pointer is cleared.
    """
    cleared = []
    rows = conn.execute(
        "SELECT company_norm, file FROM logos WHERE file IS NOT NULL AND file != ''"
    ).fetchall()
    for row in rows:
        try:
            path = stored_path(cfg, row["file"])
        except ValueError:
            path = None
        body = path.read_bytes() if path is not None and path.is_file() else b""
        if body and raster_decodes(body):
            continue
        conn.execute(
            "UPDATE logos SET file = NULL, error = ? WHERE company_norm = ?",
            ("cached file missing or undecodable", row["company_norm"]))
        cleared.append(row["company_norm"])
    conn.commit()
    return cleared


def icon_candidates(html: str, base_url: str) -> list[str]:
    """Raster icon URLs from a home page, best first; ``/favicon.ico`` is always there.

    Order: apple-touch-icon (largest first), then ``rel=icon`` with sizes >= 64 (largest
    first), then ``/favicon.ico``, then smaller or unsized icons. SVG is skipped.
    """
    from . import htmldom

    touch, large, small = [], [], []
    for link in htmldom.parse(html or "").find_all("link"):
        rel = (link.get("rel") or "").casefold().split()
        href = (link.get("href") or "").strip()
        if not href or href.startswith("data:"):
            continue
        if "svg" in (link.get("type") or "").casefold() or \
                urllib.parse.urlsplit(href).path.casefold().endswith(".svg"):
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", link.get("sizes") or "")]
        side = max(sizes) if sizes else 0
        url = urllib.parse.urljoin(base_url, href)
        if any(r.startswith("apple-touch-icon") for r in rel):
            touch.append((side, url))
        elif "icon" in rel:
            (large if side >= GOOD_SIZE else small).append((side, url))
    ordered = [url for _, url in sorted(touch, key=lambda p: -p[0])]
    ordered += [url for _, url in sorted(large, key=lambda p: -p[0])]
    ordered.append(urllib.parse.urljoin(base_url, "/favicon.ico"))
    ordered += [url for _, url in sorted(small, key=lambda p: -p[0])]
    # A script-only home page often still serves these. They stay after linked icons
    # so a real apple-touch-icon in the HTML is preferred. The fetcher tries four.
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        ordered.append(urllib.parse.urljoin(base_url, path))
    seen, out = set(), []
    for url in ordered:
        if url not in seen and urllib.parse.urlsplit(url).scheme == "https":
            seen.add(url)
            out.append(url)
    return out


def _store_bytes(cfg, conn, key: str, body: bytes, kind: str, url: str) -> str:
    directory = logos_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    name = file_name(key, kind)
    handle = tempfile.NamedTemporaryFile("wb", dir=str(directory), prefix=".logo-",
                                         suffix=".tmp", delete=False)
    try:
        handle.write(body)
        handle.close()
        os.replace(handle.name, directory / name)
    except BaseException:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    store.set_logo(conn, key, name, url)
    return name


def _left_its_site(domain: str, requested: str, final: str | None) -> bool:
    """True when a URL on this site redirects to a different site.

    A CDN address linked from the company's own page is already off-site when requested,
    so it is kept. Only a same-site URL that lands elsewhere is refused.
    """
    if not stayed_on_site(domain, requested):
        return False
    return not stayed_on_site(domain, final or requested)


def _candidates_after_redirect(domain: str, home: str, page, log) -> list[str]:
    """Icon URLs from a home page that stayed on this site.

    A redirect to another brand (Moët Hennessy → LVMH, Renaissance → Marriott) must not
    donate that brand's favicon. Same-site ``/favicon.ico`` is still tried.
    """
    final = page.url or home
    if not stayed_on_site(domain, final):
        log(f"logo {domain}: home page left the site for {final}; not using that brand's icon")
        return icon_candidates("", home)
    return icon_candidates(page.text if page.ok else "", final)


def _best_icon(fetcher, domain: str, candidates: list[str], log) -> tuple | None:
    """First raster of >= 64 px among up to four URLs, else the largest. None if none."""
    best: tuple[int, bytes, str, str] | None = None
    for url in candidates[:4]:
        try:
            response = fetcher.get(url, accept="image/*")
        except HostStopped:
            if best is None:
                raise
            break
        except Exception as exc:
            log(f"logo {domain}: {url} failed ({exc})")
            continue
        body = response.raw if isinstance(response.raw, bytes) else b""
        if response.ok and _left_its_site(domain, url, response.url):
            log(f"logo {domain}: {url} redirected off the site to {response.url}")
            continue
        kind = image_kind(body) if response.ok else None
        if kind is not None and len(body) > MAX_BYTES:
            body = _within_cap(body) or b""
            kind = image_kind(body)
        if kind is None or len(body) > MAX_BYTES:
            continue                        # SVG, HTML, too big: try the next one
        side = image_size(body)
        if side is not None and side < 16:
            continue                        # a 1 px stub is not a logo
        side = side or GOOD_SIZE            # JPEG/WebP: size unknown, accept
        if best is None or side > best[0]:
            best = (side, body, kind, url)
        if side >= GOOD_SIZE:
            break
    return best


def fetch_domain_icon(cfg, conn, fetcher, domain: str, log=None, assets=None) -> str | None:
    """The company site's own icon: a known asset, then home page links, then /favicon.ico.

    Each candidate is checked by magic bytes and size; the first one of >= 64 px wins,
    else the largest raster of at least 16 px. When the apex host refuses, ``www.`` is
    tried (a different host, not a captcha bypass). ``HostStopped`` is raised unrecorded
    when nothing was stored. At most four icon requests per host, plus one configured asset.
    """
    log = log or (lambda *a, **k: None)
    key = domain_key(domain)
    asset = (assets or {}).get(domain.casefold())
    if asset:
        # A known official file. A miss is not remembered yet: the home page may still work.
        if download(cfg, conn, fetcher, key, domain, asset, log=log, remember_failure=False):
            return store.get_logo(conn, key)["file"]

    home = f"https://{domain}/"
    stopped: HostStopped | None = None
    try:
        page = fetcher.get(home)
        candidates = _candidates_after_redirect(domain, home, page, log)
    except HostStopped as exc:
        stopped = exc
        candidates = []
    except Exception as exc:
        log(f"logo {domain}: home page failed ({exc}); trying /favicon.ico")
        candidates = icon_candidates("", home)

    try:
        best = _best_icon(fetcher, domain, candidates, log) if candidates else None
    except HostStopped as exc:
        stopped = stopped or exc
        best = None

    if best is None and not domain.casefold().startswith("www."):
        www = "www." + domain
        www_home = f"https://{www}/"
        try:
            page = fetcher.get(www_home)
            www_candidates = _candidates_after_redirect(domain, www_home, page, log)
        except HostStopped:
            www_candidates = []
        except Exception as exc:
            log(f"logo {domain}: www home page failed ({exc})")
            www_candidates = [urllib.parse.urljoin(www_home, "/favicon.ico")]
        try:
            best = _best_icon(fetcher, domain, www_candidates, log) if www_candidates else None
        except HostStopped:
            best = None

    if best is None:
        if stopped is not None:
            raise stopped
        store.set_logo(conn, key, None, home, error="no raster icon on the site")
        log(f"logo {domain}: no raster icon found")
        return None
    side, body, kind, url = best
    name = _store_bytes(cfg, conn, key, body, kind, url)
    log(f"logo {domain}: stored {name} ({len(body)} bytes, {side} px) from {url}")
    return name


def _recently_failed(row) -> bool:
    if row is None or row["file"] or not row["failed_at"]:
        return False
    from datetime import datetime, timedelta, timezone
    try:
        failed = datetime.fromisoformat(str(row["failed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - failed < timedelta(days=7)


def fetch_domains(cfg, conn, fetcher, domains, limit: int = 30, log=None,
                  assets=None, retry_failed: bool = False) -> dict:
    """Site icons for configured domains that have none yet (failures wait 7 days).

    ``retry_failed`` tries again inside that week. A configured ``[logo_assets]`` URL
    is always worth a try, because the earlier failure was the home page, not that file.
    """
    report = {"considered": 0, "stored": 0, "refused": 0, "deferred": 0}
    assets = assets if assets is not None else {}
    for domain in list(dict.fromkeys(domains))[:max(limit, 0)]:
        row = store.get_logo(conn, domain_key(domain))
        if row is not None and row["file"]:
            continue
        if _recently_failed(row) and not retry_failed and domain.casefold() not in assets:
            continue
        report["considered"] += 1
        try:
            name = fetch_domain_icon(cfg, conn, fetcher, domain, log=log, assets=assets)
        except HostStopped as exc:
            if (exc.reason or "").startswith("request cap"):
                report["deferred"] += 1         # our budget, not the site's answer
            else:
                # a 403/429/wall is the site's policy: ask again in a week, not every run
                store.set_logo(conn, domain_key(domain), None, f"https://{domain}/",
                               error=f"site refused: {exc.reason}")
                report["refused"] += 1
            continue
        report["stored" if name else "refused"] += 1
    return report


# --------------------------------------------------------------------------- #
# lookup for the board
# --------------------------------------------------------------------------- #

class LogoIndex:
    """One read of the ``logos`` table; resolves a company to `/logos/<file>`.

    Order: the company's own logo (LinkedIn / JobsDB / profile ``logo_url``), then the
    icon of the configured company it matches.
    """

    def __init__(self, conn, companies=None):
        self.files = store.logo_map(conn)
        self.matcher = CompanyMatcher(companies)

    def file(self, company: str | None, company_norm: str | None = None) -> str | None:
        norm = logo_storage_key(company, company_norm)
        if norm and self.files.get(norm):
            return self.files[norm]
        domain = self.matcher.domain_for(company or norm)
        return self.files.get(domain_key(domain)) if domain else None

    def url(self, company: str | None, company_norm: str | None = None) -> str | None:
        name = self.file(company, company_norm)
        return f"/logos/{name}" if name else None


# --------------------------------------------------------------------------- #
# backfill: every visible company, cheapest path first
# --------------------------------------------------------------------------- #

def _posting_logo(fetcher, job) -> str | None:
    """One posting fetch for a job without a logo URL: LinkedIn or JobsDB only."""
    from .sources import jobsdb, linkedin

    if job["source"] == linkedin.SOURCE and job["source_job_id"]:
        response = fetcher.get(linkedin.posting_url(job["source_job_id"]))
        return linkedin.parse_posting(response.text).get("logo_source_url") if response.ok else None
    if job["source"] == jobsdb.SOURCE and job["source_job_id"]:
        response = fetcher.get(jobsdb.job_url(job["source_job_id"]))
        return jobsdb.parse_job_page(response.text).get("logo_source_url") if response.ok else None
    return None


def backfill(cfg, conn, fetcher, companies: list[dict], log=None,
             linkedin_budget: int = 45) -> dict:
    """Give every visible company a real logo by the cheapest path.

    ``companies``: ``[{"company", "company_norm", "jobs": [job rows]}]``, most important
    first. Paths, in order: a stored logo URL, the configured company's site icon, one
    posting fetch (LinkedIn within ``linkedin_budget`` requests, JobsDB within its cap).
    Writes only the ``logos`` table, files in ``data/logos/`` and ``jobs.logo_source_url``.
    """
    from .sources import linkedin

    log = log or (lambda *a, **k: None)
    profile_companies = logo_sources(cfg)
    matcher = CompanyMatcher(profile_companies)
    assets = logo_assets(cfg)
    report = {"visible": len(companies), "had_logo": 0, "stored": {"url": 0, "domain": 0,
              "posting": 0}, "deferred": 0, "missing": []}
    linkedin_used = 0

    def covered(item):
        return LogoIndex(conn, profile_companies).file(item["company"], item["company_norm"])

    for item in companies:
        norm, name = item["company_norm"], item["company"]
        if covered(item):
            report["had_logo"] += 1
            continue
        try:
            own = store.get_logo(conn, norm) if norm else None
            urls = [job["logo_source_url"] for job in item["jobs"] if job["logo_source_url"]]
            if norm and urls and not _recently_failed(own):
                if download(cfg, conn, fetcher, norm, name, urls[0], log=log):
                    report["stored"]["url"] += 1
                    continue
            domain = matcher.domain_for(name)
            if domain:
                fetch_domains(cfg, conn, fetcher, [domain], log=log, assets=assets,
                              retry_failed=bool(assets.get(domain.casefold())))
                if covered(item):
                    report["stored"]["domain"] += 1
                    continue
            if norm and not urls and not _recently_failed(own):
                for job in item["jobs"]:
                    if job["source"] == linkedin.SOURCE:
                        if linkedin_used >= linkedin_budget:
                            continue
                        linkedin_used += 1
                    url = _posting_logo(fetcher, job)
                    if url:
                        store.set_logo_source_url(conn, job["id"], url)
                        if download(cfg, conn, fetcher, norm, name, url, log=log):
                            report["stored"]["posting"] += 1
                        break
                    if job["source"] in (linkedin.SOURCE, "jobsdb"):
                        break               # one posting fetch per company
        except HostStopped as exc:
            log(f"logo {name}: {exc}")
            report["deferred"] += 1
            continue
        except Exception as exc:            # one company must not stop the backfill
            log(f"logo {name}: {type(exc).__name__}: {exc}")
    report["relink"] = relink(conn, profile_companies,
                              [(item["company"], item["company_norm"]) for item in companies])
    index = LogoIndex(conn, profile_companies)
    report["missing"] = [item["company"] for item in companies
                         if not index.file(item["company"], item["company_norm"])]
    report["with_logo"] = report["visible"] - len(report["missing"])
    return report


# --------------------------------------------------------------------------- #
# relink: matched site icons written under each company's own key (no fetch)
# --------------------------------------------------------------------------- #

def relink(conn, sources, names) -> dict:
    """For every ``(company, company_norm)`` without its own logo whose name matches a
    configured domain with a stored icon, write a link row under ``company_norm``.

    No request is made. Lookups by the company's own key then work in every board
    version, and the company's own LinkedIn/JobsDB logo still replaces the link later.
    """
    matcher = CompanyMatcher(sources)
    report = {"checked": 0, "linked": 0, "updated": 0, "own": 0, "unmatched": 0}
    seen = set()
    for company, norm in names:
        norm = logo_storage_key(company, norm)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        report["checked"] += 1
        row = store.get_logo(conn, norm)
        linked = row is not None and str(row["source_url"] or "").startswith(store.LINK_PREFIX)
        if row is not None and row["file"] and not linked:
            report["own"] += 1
            continue
        domain = matcher.domain_for(company)
        target = store.get_logo(conn, domain_key(domain)) if domain else None
        if target is None or not target["file"]:
            report["unmatched"] += 1
            continue
        if linked and row["file"] == target["file"]:
            continue
        store.set_logo_link(conn, norm, target["file"], domain_key(domain))
        report["updated" if linked else "linked"] += 1
    return report


def all_company_names(conn, tracker=None) -> list[tuple[str, str]]:
    """Every company on a stored job, plus every tracker record's company."""
    names = [(row["company"], row["company_norm"]) for row in conn.execute(
        "SELECT DISTINCT company, company_norm FROM jobs WHERE company IS NOT NULL")]
    if tracker is not None:
        for status in ("to_apply", "applied"):
            names += [(record.get("company"), None) for record in tracker.applications(status)
                      if record.get("company")]
    return names


def check(conn, tracker, sources) -> list[dict]:
    """Companies with a stored logo file whose jobs or records still show no logo_url.

    Uses the board's own row builders, so it checks what ``/api/jobs`` and
    ``/api/applications`` return, not a re-implementation.
    """
    from . import board_api

    index = LogoIndex(conn, sources)
    gaps: dict[str, dict] = {}

    def expected(company, norm):
        own = store.get_logo(conn, logo_storage_key(company, norm) or "")
        if own is not None and own["file"]:
            return own["file"]
        domain = index.matcher.domain_for(company)
        target = store.get_logo(conn, domain_key(domain)) if domain else None
        return target["file"] if target is not None and target["file"] else None

    logos = board_api.logo_urls(conn, sources)
    for row in store.board_jobs(conn):
        file = expected(row["company"], row["company_norm"])
        if file and not board_api.job_row(row, tracker, logos)["logo_url"]:
            gaps.setdefault(row["company"], {"company": row["company"], "file": file,
                                             "jobs": 0, "records": 0})["jobs"] += 1
    for status in ("to_apply", "applied"):
        for record in board_api.applications(tracker, status, conn=conn,
                                             companies=sources)["applications"]:
            file = expected(record.get("company"), None)
            if file and not record.get("logo_url"):
                gaps.setdefault(record.get("company"), {"company": record.get("company"),
                                                        "file": file, "jobs": 0,
                                                        "records": 0})["records"] += 1
    return list(gaps.values())

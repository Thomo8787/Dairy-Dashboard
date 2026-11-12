"""Download the current month's Müller collection CSVs and apply weighbridge litres.

One portal login covers ALH, BNK, COF, and SFR. The Parkes uses a different
login and is skipped. Credentials come from MULLER_USERNAME and MULLER_PASSWORD.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from services.muller_collections_import import import_muller_uploads
from services.nml_pdf import FARM_PRODUCER_REF

logger = logging.getLogger(__name__)

_UK = ZoneInfo("Europe/London")
DEFAULT_BASE = "https://farmerappprd.mulleragroscloud.com/ords/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# One login. PRK is a separate portal account and is not included.
PORTAL_FARMS: tuple[str, ...] = ("ALH", "BNK", "COF", "SFR")

_APEX_URL = re.compile(r"f\?p=[0-9]+:[0-9]+:[0-9]*:[^'\"\s)<]*", re.I)
_LOCKED = re.compile(r"account (has been|is) locked", re.I)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


class MullerPortalError(Exception):
    """The portal could not be read. The account was not retried."""


@dataclass
class ApexForm:
    flow_id: str
    step_id: str
    instance: str
    submission_id: str
    reload_on_submit: str
    salt: str
    protected: str
    row_version: str
    checksums: list[Any] = field(default_factory=list)


@dataclass
class Link:
    href: str
    text: str
    css: str
    title: str
    aria: str
    row_text: str


@dataclass
class SelectControl:
    name: str
    options: list[tuple[str, str, bool]]


def muller_is_configured() -> bool:
    return bool(_username() and _password())


def _username() -> str:
    return (os.environ.get("MULLER_USERNAME") or "").strip()


def _password() -> str:
    return os.environ.get("MULLER_PASSWORD") or ""


def _base_url() -> str:
    raw = (os.environ.get("MULLER_PORTAL_BASE") or DEFAULT_BASE).strip()
    return raw if raw.endswith("/") else raw + "/"


def collection_filename(farm: str, month: dt.date) -> str:
    return f"{farm}_MULLER_collection_details_for_{month:%Y-%m}.csv"


def month_needles(day: dt.date) -> list[str]:
    """Labels the portal may use for this calendar month."""
    full = _MONTHS[day.month - 1]
    short = full[:3]
    yy = f"{day.year % 100:02d}"
    return [
        f"{full} {day.year}",
        f"{short} {day.year}",
        f"{short}-{day.year}",
        f"{short}-{yy}",
        f"{day.year}-{day.month:02d}",
        f"{day.month:02d}/{day.year}",
        f"{day.month:02d}-{day.year}",
        f"{day.month}/{day.year}",
    ]


def score_month(text: str, day: dt.date) -> int:
    folded = " ".join((text or "").lower().replace(",", " ").split())
    if not folded:
        return 0
    best = 0
    for needle in month_needles(day):
        if needle in folded:
            best = max(best, 3 if str(day.year) in needle else 2)
    return best


def looks_like_collection_csv(text: str) -> bool:
    sample = (text or "").lstrip("\ufeff").lstrip()
    if not sample or sample.startswith("<") or sample.startswith("{"):
        return False
    header = sample.splitlines()[0].lower()
    return "litre" in header and (
        "sample" in header or "date" in header or "collection" in header
    )


def decode_csv_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "replace")


def split_csv_by_producer(text: str) -> dict[str, str] | None:
    """Split a combined export when a column carries the producer number."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None
    field_name = ""
    for name in reader.fieldnames:
        folded = (name or "").strip().lower()
        if "producer" in folded or folded in {
            "supplier",
            "supplier number",
            "membership",
            "member",
            "account",
            "account number",
        }:
            field_name = name
            break
    if not field_name:
        return None
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in reader:
        ref = re.sub(r"\D", "", str(row.get(field_name) or ""))
        farm = next(
            (code for code, producer in FARM_PRODUCER_REF.items() if producer == ref),
            "",
        )
        if farm in PORTAL_FARMS:
            grouped.setdefault(farm, []).append(row)
    if not grouped:
        return None
    split: dict[str, str] = {}
    for farm, rows in grouped.items():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(reader.fieldnames))
        writer.writeheader()
        writer.writerows(rows)
        split[farm] = buffer.getvalue()
    return split


def with_apex_request(url: str, request_name: str) -> str | None:
    match = re.search(r"(f\?p=\d+:\d+:\d*:)([^:]*)", url, re.I)
    if not match:
        return None
    return url[: match.start()] + match.group(1) + request_name + url[match.end() :]


def extract_apex_urls(raw: str) -> list[str]:
    found: list[str] = []
    for match in _APEX_URL.finditer(raw or ""):
        url = match.group(0).replace("&amp;", "&")
        if url not in found:
            found.append(url)
    return found


def ir_configs(html: str) -> list[dict[str, str]]:
    configs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'ajaxIdentifier"\s*:\s*"([^"]+)"', html):
        window = html[max(0, match.start() - 600) : match.end() + 600]
        worksheet = re.search(r'worksheetId"\s*:\s*"?(\d+)', window)
        report = re.search(r'reportId"\s*:\s*"?(\d+)', window)
        if not worksheet or not report:
            continue
        key = match.group(1)
        if key in seen:
            continue
        seen.add(key)
        configs.append(
            {
                "ajax_id": key,
                "worksheet_id": worksheet.group(1),
                "report_id": report.group(1),
            }
        )
    return configs


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}
        self.links: list[Link] = []
        self.selects: list[SelectControl] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._row_depth = 0
        self._row_parts: list[str] = []
        self._row_links: list[Link] = []
        self._link: dict[str, Any] | None = None
        self._link_parts: list[str] = []
        self._select: SelectControl | None = None
        self._option: list[Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
            self._title_parts = []
        if tag == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_parts = []
                self._row_links = []
        if tag == "input":
            value = attr.get("value", "")
            for key in (attr.get("id", ""), attr.get("name", "")):
                if key:
                    self.inputs[key] = value
        if tag == "a":
            self._link = {
                "href": attr.get("href", "") or attr.get("onclick", ""),
                "css": attr.get("class", ""),
                "title": attr.get("title", ""),
                "aria": attr.get("aria-label", ""),
            }
            self._link_parts = []
        if tag == "select":
            self._select = SelectControl(name=attr.get("id") or attr.get("name") or "", options=[])
        if tag == "option" and self._select is not None:
            self._option = [attr.get("value", ""), "", "selected" in attr]
        if tag == "img" and self._row_depth:
            self._row_parts.append(attr.get("alt", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split())
        if tag == "a" and self._link is not None:
            link = Link(
                href=self._link["href"],
                text=" ".join("".join(self._link_parts).split()),
                css=self._link["css"],
                title=self._link["title"],
                aria=self._link["aria"],
                row_text="",
            )
            if self._row_depth:
                self._row_links.append(link)
            else:
                self.links.append(link)
            self._link = None
        if tag == "option" and self._option is not None and self._select is not None:
            self._select.options.append(
                (self._option[0], " ".join(self._option[1].split()), bool(self._option[2]))
            )
            self._option = None
        if tag == "select" and self._select is not None and self._select.name:
            self.selects.append(self._select)
            self._select = None
        if tag == "tr" and self._row_depth:
            self._row_depth -= 1
            if self._row_depth == 0:
                row_text = " ".join(" ".join(self._row_parts).split())
                for link in self._row_links:
                    self.links.append(
                        Link(
                            href=link.href,
                            text=link.text,
                            css=link.css,
                            title=link.title,
                            aria=link.aria,
                            row_text=row_text,
                        )
                    )

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._link is not None:
            self._link_parts.append(data)
        if self._option is not None:
            self._option[1] += data
        if self._row_depth:
            self._row_parts.append(data)


def parse_page(html: str) -> tuple[ApexForm | None, _PageParser]:
    parser = _PageParser()
    parser.feed(html)
    inputs = parser.inputs
    flow_id = inputs.get("pFlowId") or inputs.get("p_flow_id") or ""
    step_id = inputs.get("pFlowStepId") or inputs.get("p_flow_step_id") or ""
    instance = inputs.get("pInstance") or inputs.get("p_instance") or ""
    if not (flow_id and step_id and instance):
        return None, parser
    raw_sums = inputs.get("pPageFormRegionChecksums") or "[]"
    try:
        checksums = json.loads(raw_sums)
    except json.JSONDecodeError:
        checksums = []
    form = ApexForm(
        flow_id=flow_id,
        step_id=step_id,
        instance=instance,
        submission_id=inputs.get("pPageSubmissionId") or inputs.get("p_page_submission_id") or "",
        reload_on_submit=inputs.get("pReloadOnSubmit") or inputs.get("p_reload_on_submit") or "S",
        salt=inputs.get("pSalt") or inputs.get("pPageSubmissionId") or "",
        protected=inputs.get("pPageItemsProtected") or "",
        row_version=inputs.get("pPageItemsRowVersion") or "",
        checksums=checksums if isinstance(checksums, list) else [],
    )
    return form, parser


def _page_json(form: ApexForm, items: dict[str, str]) -> str:
    payload = {
        "salt": form.salt,
        "pageItems": {
            "itemsToSubmit": [{"n": name, "v": value} for name, value in items.items()],
            "protected": form.protected,
            "rowVersion": form.row_version,
            "formRegionChecksums": form.checksums,
        },
    }
    return json.dumps(payload, separators=(",", ":"))


def _is_view_link(link: Link) -> bool:
    blob = " ".join((link.css, link.title, link.aria, link.text)).lower()
    return (
        "fa-search" in link.css.lower()
        or "magnif" in blob
        or "view" in blob
        or "detail" in blob
    )


def _is_collections_link(link: Link) -> bool:
    blob = " ".join((link.text, link.title, link.aria)).lower()
    return "collection" in blob and "statement" not in blob


def _selection_state(html: str, producer_ref: str) -> str:
    """match, other, or unknown when no option is marked selected."""
    _form, parser = parse_page(html)
    selected = [
        (value, label)
        for select in parser.selects
        for value, label, is_selected in select.options
        if is_selected
    ]
    if not selected:
        return "unknown"
    if any(producer_ref in label or producer_ref == value for value, label in selected):
        return "match"
    return "other"


class MullerPortal:
    def __init__(self) -> None:
        if not muller_is_configured():
            raise MullerPortalError(
                "Müller login is not set. Add MULLER_USERNAME and MULLER_PASSWORD "
                "on the dairy-dashboard service in Render."
            )
        self.base = _base_url()
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
            timeout=60,
            follow_redirects=True,
        )
        self.home_url = ""

    def close(self) -> None:
        self.client.close()

    def _absolute(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/ords/"):
            return "https://farmerappprd.mulleragroscloud.com" + url
        if url.startswith("/"):
            return "https://farmerappprd.mulleragroscloud.com" + url
        return self.base + url.lstrip("/")

    def _get(self, url: str) -> tuple[str, str]:
        response = self.client.get(self._absolute(url))
        response.raise_for_status()
        content = response.content
        preview = decode_csv_bytes(content[:800])
        text = decode_csv_bytes(content) if looks_like_collection_csv(preview) else response.text
        self._dump("get", text[:200000])
        return str(response.url), text

    def _dump(self, label: str, html: str) -> None:
        if (os.environ.get("MULLER_DEBUG") or "").strip() not in {"1", "true", "yes"}:
            return
        from pathlib import Path

        folder = Path(__file__).resolve().parent.parent / "data" / "muller_portal_debug"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "page"
        redacted = re.sub(
            r'(name="P211_PASSWORD"[^>]*value=")[^"]*',
            r"\1",
            html,
            flags=re.I,
        )
        (folder / f"{safe}.html").write_text(redacted, encoding="utf-8")

    def open_login_form(self) -> tuple[str, str]:
        url, html = self._get("f?p=200:LOGIN")
        match = re.search(r"window\.location\.href='([^']+)'", html)
        if not match:
            return url, html
        href = match.group(1).encode("utf-8").decode("unicode_escape")
        offset = dt.datetime.now(_UK).utcoffset() or dt.timedelta(0)
        hours, remainder = divmod(int(offset.total_seconds()), 3600)
        href = re.sub(r"tz=.*", f"tz={hours}:{abs(remainder) // 60:02d}", href)
        return self._get(href)

    def accept(self, form: ApexForm, request_name: str, items: dict[str, str]) -> tuple[str, str]:
        payload = {
            "p_flow_id": form.flow_id,
            "p_flow_step_id": form.step_id,
            "p_instance": form.instance,
            "p_debug": "",
            "p_request": request_name,
            "p_reload_on_submit": form.reload_on_submit or "S",
            "p_page_submission_id": form.submission_id,
            "p_json": _page_json(form, items),
        }
        response = self.client.post(self.base + "wwv_flow.accept", data=payload)
        response.raise_for_status()
        body = response.text
        self._dump(f"accept-{request_name or 'page'}", body)
        stripped = body.lstrip()
        if stripped.startswith("{"):
            try:
                message = response.json()
            except json.JSONDecodeError:
                message = {}
            errors = message.get("errors") if isinstance(message, dict) else None
            if errors:
                detail = "; ".join(
                    str(item.get("message") or item) for item in errors if isinstance(item, dict)
                ) or str(errors)
                if _LOCKED.search(detail):
                    raise MullerPortalError(
                        "The Müller portal has locked this login. Email "
                        "farm.services@muller.co.uk and do not try again until it is unlocked."
                    )
                raise MullerPortalError(detail)
            target = ""
            if isinstance(message, dict):
                target = str(message.get("redirectURL") or "")
            if not target:
                raise MullerPortalError("The portal did not open the next page.")
            return self._get(target)
        if _LOCKED.search(body) and "P211_PASSWORD" in body:
            raise MullerPortalError(
                "The Müller portal has locked this login. Email "
                "farm.services@muller.co.uk and do not try again until it is unlocked."
            )
        return str(response.url), body

    def login(self) -> str:
        _url, html = self.open_login_form()
        form, _parser = parse_page(html)
        if form is None:
            raise MullerPortalError("The Müller sign-in page did not load.")
        url, html = self.accept(
            form,
            "LOGIN",
            {"P211_USERNAME": _username(), "P211_PASSWORD": _password()},
        )
        if "P211_PASSWORD" in html and "Welcome to Müller Farmer Portal" in html:
            raise MullerPortalError("The Müller portal rejected the username or password.")
        self.home_url = url
        logger.info("Signed in to the Müller portal")
        return html

    def _switch_producer(self, html: str, producer_ref: str) -> str | None:
        _form, parser = parse_page(html)
        form = _form
        for select in parser.selects:
            match = next(
                (
                    (value, label)
                    for value, label, _selected in select.options
                    if producer_ref in label or producer_ref == value
                ),
                None,
            )
            if match is None or form is None:
                continue
            value, _label = match
            request_name = select.name
            found = re.search(
                rf"{re.escape(select.name)}.{{0,300}}?apex\.submit\(\{{request:'([^']+)'",
                html,
            )
            if found:
                request_name = found.group(1)
            _url, switched = self.accept(form, request_name, {select.name: value})
            state = _selection_state(switched, producer_ref)
            if state == "other":
                labels = ", ".join(
                    label or value
                    for control in parse_page(switched)[1].selects
                    if control.name == select.name
                    for value, label, _selected in control.options[:8]
                )
                raise MullerPortalError(
                    f"Could not switch the portal to producer {producer_ref}. "
                    f"Choices: {labels or 'none'}"
                )
            return switched
        for link in parser.links:
            blob = f"{link.text} {link.title} {link.aria} {link.row_text}"
            if producer_ref not in blob:
                continue
            urls = extract_apex_urls(link.href)
            if not urls:
                continue
            _url, page = self._get(urls[0])
            return page
        return None

    def _open_named(self, html: str, predicate) -> tuple[str, str] | None:
        _form, parser = parse_page(html)
        matches = [link for link in parser.links if predicate(link) and extract_apex_urls(link.href)]
        if not matches:
            return None
        matches.sort(key=lambda link: 0 if link.text.lower() == "collections" else 1)
        return self._get(extract_apex_urls(matches[0].href)[0])

    def _open_current_month(self, html: str, day: dt.date) -> tuple[str, str]:
        _form, parser = parse_page(html)
        rows: dict[str, list[Link]] = {}
        for link in parser.links:
            if not link.row_text:
                continue
            rows.setdefault(link.row_text, []).append(link)
        ranked = sorted(
            ((score_month(text, day), text, links) for text, links in rows.items()),
            key=lambda item: (-item[0], len(item[1])),
        )
        if not ranked or ranked[0][0] == 0:
            loose = [
                (score_month(f"{link.text} {link.title} {link.aria}", day), link)
                for link in parser.links
            ]
            loose = [item for item in loose if item[0] > 0 and extract_apex_urls(item[1].href)]
            if loose:
                loose.sort(key=lambda item: -item[0])
                return self._get(extract_apex_urls(loose[0][1].href)[0])
            labels = [text for text in rows if text][:12]
            shown = "; ".join(labels) if labels else parser.title or "no rows"
            raise MullerPortalError(
                f"Could not find {day:%B %Y} on the collections page. Rows seen: {shown}"
            )
        _score, _text, links = ranked[0]
        view = next((link for link in links if _is_view_link(link)), None)
        target = view or next((link for link in links if extract_apex_urls(link.href)), None)
        if target is None:
            raise MullerPortalError(f"Found {day:%B %Y} but no magnifying glass beside it.")
        urls = extract_apex_urls(target.href)
        if not urls:
            raise MullerPortalError(f"The {day:%B %Y} link could not be opened.")
        return self._get(urls[0])

    def _csv_from_urls(self, urls: list[str]) -> str | None:
        for url in urls:
            if "csv" not in url.lower() and with_apex_request(url, "CSV") != url:
                continue
            try:
                _url, body = self._get(url)
            except httpx.HTTPError:
                logger.warning("Müller CSV link failed: %s", url[:120])
                continue
            if looks_like_collection_csv(body):
                return body
        return None

    def _plugin(self, form: ApexForm, config: dict[str, str], action: str, mod: str) -> str:
        payload = {
            "p_flow_id": form.flow_id,
            "p_flow_step_id": form.step_id,
            "p_instance": form.instance,
            "p_debug": "",
            "p_request": "PLUGIN=" + config["ajax_id"],
            "p_widget_name": "worksheet",
            "p_widget_mod": mod,
            "p_widget_action": action,
            "p_widget_num_return": "50",
            "x01": config["worksheet_id"],
            "x02": config["report_id"],
            "p_json": _page_json(form, {}),
        }
        response = self.client.post(self.base + "wwv_flow.ajax", data=payload)
        response.raise_for_status()
        self._dump(f"plugin-{action}", response.text)
        return response.text

    def _download_csv(self, page_url: str, html: str) -> str:
        direct = [url for url in extract_apex_urls(html) if "csv" in url.lower()]
        found = self._csv_from_urls(direct)
        if found:
            return found
        rewritten: list[str] = []
        for url in [page_url, *extract_apex_urls(html)[:8]]:
            for request_name in ("CSV",):
                changed = with_apex_request(url, request_name)
                if changed and changed not in rewritten:
                    rewritten.append(changed)
        for region in re.findall(r'id="(R\d+)', html)[:4]:
            changed = with_apex_request(page_url, f"IR{region}_CSV")
            if changed:
                rewritten.append(changed)
        found = self._csv_from_urls(rewritten)
        if found:
            return found
        form, _parser = parse_page(html)
        for config in ir_configs(html)[:2]:
            if form is None:
                break
            dialog = self._plugin(form, config, "SHOW_DOWNLOAD", "CONTROL")
            if looks_like_collection_csv(dialog):
                return dialog
            dialog_urls = [url for url in extract_apex_urls(dialog) if "csv" in url.lower()]
            found = self._csv_from_urls(dialog_urls or extract_apex_urls(dialog)[:6])
            if found:
                return found
        raise MullerPortalError(
            "Opened the month, but Actions → Download did not return a collection CSV."
        )

    def download_month(self, day: dt.date) -> dict[str, str]:
        """Return farm code -> CSV text for the month containing `day`."""
        self.login()
        files: dict[str, str] = {}
        saw_switch = False
        for farm in PORTAL_FARMS:
            ref = FARM_PRODUCER_REF[farm]
            _url, page = self._get(self.home_url)
            switched = self._switch_producer(page, ref)
            if switched is None:
                if saw_switch:
                    raise MullerPortalError(f"Could not switch the portal to {farm} ({ref}).")
                text = self._download_farm_month(page, day)
                pieces = split_csv_by_producer(text)
                if not pieces:
                    raise MullerPortalError(
                        "The portal did not offer a producer switch for "
                        + ", ".join(f"{FARM_PRODUCER_REF[code]} ({code})" for code in PORTAL_FARMS)
                        + ", and the CSV has no producer column to split."
                    )
                return {code: pieces[code] for code in PORTAL_FARMS if code in pieces}
            saw_switch = True
            text = self._download_farm_month(switched, day)
            pieces = split_csv_by_producer(text)
            if pieces and farm not in pieces:
                raise MullerPortalError(
                    f"The {farm} download had no rows for producer {ref}."
                )
            files[farm] = pieces[farm] if pieces else text
        return {farm: files[farm] for farm in PORTAL_FARMS if farm in files}

    def _download_farm_month(self, page: str, day: dt.date) -> str:
        opened = self._open_named(page, _is_collections_link)
        if opened is None:
            raise MullerPortalError("Signed in, but there is no Collections link.")
        _collections_url, collections = opened
        detail_url, detail = self._open_current_month(collections, day)
        return self._download_csv(detail_url, detail)


def sync_muller_collections(*, month: dt.date | None = None) -> dict[str, Any]:
    """Download the current UK month and write weighbridge litres."""
    day = month or dt.datetime.now(_UK).date()
    portal = MullerPortal()
    try:
        downloaded = portal.download_month(day)
    finally:
        portal.close()
    if not downloaded:
        raise MullerPortalError("The portal did not return a collection file.")
    payloads: list[tuple[str, bytes]] = []
    for farm, text in downloaded.items():
        if not looks_like_collection_csv(text):
            raise MullerPortalError(f"{farm} file is not a Müller collection CSV.")
        payloads.append((collection_filename(farm, day), text.encode("utf-8")))
    result = import_muller_uploads(payloads)
    result["month"] = f"{day:%Y-%m}"
    result["farms"] = list(downloaded)
    logger.info(
        "Müller %s updated %s weighbridge value(s) from %s",
        result["month"],
        result["rows_updated"],
        ", ".join(result["farms"]),
    )
    return result

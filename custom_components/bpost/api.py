"""Web client for bpost Mijn bpost."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import ClientError, ClientResponseError, ClientSession

LOGIN_URL = "https://www.bpost.be/nl/saml_login?destination=%2Fnl%2Fmijn-bpost"
MIJN_BPOST_URL = "https://www.bpost.be/nl/mijn-bpost?check_logged_in=1"
LOGIN_HOST = "login.bpost.be"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_LOGGER = logging.getLogger(__name__)
_TRACKING_RE = re.compile(r"\b[A-Z]{2}\d{9}[A-Z]{2}\b|\b\d{10,30}\b")


class BpostApiError(Exception):
    """Base bpost API error."""


class BpostAuthenticationError(BpostApiError):
    """Raised when bpost authentication fails."""


class BpostConnectionError(BpostApiError):
    """Raised when bpost cannot be reached."""


@dataclass(slots=True)
class Parcel:
    """A parcel shown by Mijn bpost."""

    tracking_id: str
    name: str
    status: str | None = None
    expected_delivery: str | None = None
    sender: str | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def delivered(self) -> bool:
        """Return whether the parcel appears to be delivered."""
        if self.status is None:
            return False
        return any(word in self.status.lower() for word in ("delivered", "geleverd", "livr", "zugestellt"))

    @property
    def attributes(self) -> dict[str, Any]:
        """Return Home Assistant state attributes."""
        attributes: dict[str, Any] = {
            "tracking_id": self.tracking_id,
        }
        if self.status:
            attributes["status"] = self.status
        if self.expected_delivery:
            attributes["expected_delivery"] = self.expected_delivery
        if self.sender:
            attributes["sender"] = self.sender
        if self.raw:
            attributes["raw"] = dict(self.raw)
        return attributes


class _FormParser(HTMLParser):
    """Extract forms and script contents from a page."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self._current_form: dict[str, Any] | None = None
        self._in_script = False
        self._script_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._current_form = {
                "action": attrs_dict.get("action", ""),
                "method": attrs_dict.get("method", "get").lower(),
                "inputs": {},
            }
        elif tag == "input" and self._current_form is not None:
            name = attrs_dict.get("name")
            if name:
                self._current_form["inputs"][name] = attrs_dict.get("value", "")
        elif tag == "script":
            self._in_script = True
            self._script_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None
        elif tag == "script" and self._in_script:
            self.scripts.append("".join(self._script_chunks).strip())
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_chunks.append(data)


class BpostWebApi:
    """Small browser-like client for Mijn bpost."""

    def __init__(self, session: ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._logged_in = False

    async def async_login(self) -> None:
        """Log in through bpost's PingFederate SAML flow."""
        html, url = await self._async_get_text(LOGIN_URL)
        for _ in range(5):
            parser = _parse_html(html)
            form = _find_login_form(parser.forms) or _find_saml_form(parser.forms)
            if form is None:
                break

            target = urljoin(url, form["action"])
            payload = dict(form["inputs"])
            if "pf.username" in payload:
                payload["pf.username"] = self._email
                payload["pf.pass"] = self._password
                payload["pf.ok"] = "clicked"
            html, url = await self._async_post_text(target, payload, referer=url)

            if not _is_login_page(html, url):
                self._logged_in = True
                return

        if _is_login_page(html, url):
            raise BpostAuthenticationError("bpost rejected the supplied credentials")

        self._logged_in = True

    async def async_fetch_parcels(self) -> list[Parcel]:
        """Fetch parcels from Mijn bpost."""
        if not self._logged_in:
            await self.async_login()

        html, url = await self._async_get_text(MIJN_BPOST_URL)
        if _is_login_page(html, url):
            self._logged_in = False
            raise BpostAuthenticationError("bpost session expired")

        return parse_parcels(html)

    async def _async_get_text(self, url: str) -> tuple[str, str]:
        try:
            response = await self._session.get(url, headers=_headers(url))
            response.raise_for_status()
            return await response.text(), str(response.url)
        except (ClientError, ClientResponseError) as exc:
            raise BpostConnectionError(exc) from exc

    async def _async_post_text(self, url: str, data: Mapping[str, str], referer: str) -> tuple[str, str]:
        try:
            response = await self._session.post(
                url,
                data=data,
                headers=_headers(url, referer=referer, form=True),
                allow_redirects=True,
            )
            response.raise_for_status()
            return await response.text(), str(response.url)
        except (ClientError, ClientResponseError) as exc:
            raise BpostConnectionError(exc) from exc


def parse_parcels(html: str) -> list[Parcel]:
    """Parse parcel-like entries from a Mijn bpost HTML response."""
    parser = _parse_html(html)
    parcels_by_id: dict[str, Parcel] = {}

    for script in parser.scripts:
        for payload in _json_payloads(script):
            for item in _walk_dicts(payload):
                parcel = _parcel_from_mapping(item)
                if parcel is not None:
                    parcels_by_id[parcel.tracking_id] = parcel

    for tracking_id in _TRACKING_RE.findall(html):
        parcels_by_id.setdefault(
            tracking_id,
            Parcel(tracking_id=tracking_id, name=tracking_id),
        )

    return [parcel for parcel in parcels_by_id.values() if not parcel.delivered]


def _parse_html(html: str) -> _FormParser:
    parser = _FormParser()
    parser.feed(html)
    return parser


def _find_login_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((form for form in forms if "pf.username" in form["inputs"] and "pf.pass" in form["inputs"]), None)


def _find_saml_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((form for form in forms if "SAMLResponse" in form["inputs"]), None)


def _is_login_page(html: str, url: str) -> bool:
    return LOGIN_HOST in url or ("pf.username" in html and "pf.pass" in html)


def _headers(url: str, referer: str | None = None, form: bool = False) -> dict[str, str]:
    parsed = urlparse(url)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": USER_AGENT,
    }
    if referer is not None:
        headers["Referer"] = referer
        referer_host = urlparse(referer).hostname
        headers["Sec-Fetch-Site"] = "same-origin" if referer_host == parsed.hostname else "cross-site"
    if form:
        origin_url = urlparse(referer) if referer is not None else parsed
        headers["Origin"] = f"{origin_url.scheme}://{origin_url.hostname}"
    return headers


def _json_payloads(script: str) -> list[Any]:
    payloads: list[Any] = []
    text = script.strip()
    if not text:
        return payloads

    if text.startswith("{") or text.startswith("["):
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            pass

    for match in re.finditer(r"JSON\.parse\((?P<quote>['\"])(?P<body>.*?)(?P=quote)\)", text):
        try:
            payloads.append(json.loads(bytes(match.group("body"), "utf-8").decode("unicode_escape")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.debug("Could not decode embedded bpost JSON payload", exc_info=True)

    return payloads


def _walk_dicts(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        result: list[Mapping[str, Any]] = [value]
        for child in value.values():
            result.extend(_walk_dicts(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_walk_dicts(child))
        return result
    return []


def _parcel_from_mapping(item: Mapping[str, Any]) -> Parcel | None:
    tracking_id = _first_text(
        item,
        (
            "barcode",
            "barCode",
            "trackingNumber",
            "tracking_number",
            "itemCode",
            "item_code",
            "shipmentId",
            "shipment_id",
            "parcelId",
            "parcel_id",
        ),
    )
    if not tracking_id:
        return None

    keys = {key.lower() for key in item}
    if not any(word in key for key in keys for word in ("parcel", "shipment", "delivery", "tracking", "barcode")):
        return None

    status = _first_text(item, ("status", "statusText", "phase", "state", "eventDescription"))
    expected_delivery = _first_text(
        item,
        (
            "expectedDeliveryDate",
            "expected_delivery_date",
            "deliveryDate",
            "delivery_date",
            "plannedDeliveryDate",
            "planned_delivery_date",
        ),
    )
    sender = _first_text(item, ("sender", "senderName", "retailerName", "shopName", "shipper"))
    name = sender or _first_text(item, ("title", "name", "description", "productName")) or tracking_id

    return Parcel(
        tracking_id=tracking_id,
        name=name,
        status=status,
        expected_delivery=expected_delivery,
        sender=sender,
        raw=item,
    )


def _first_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, Mapping):
            nested = _first_text(value, ("name", "label", "value", "text"))
            if nested:
                return nested
    return None

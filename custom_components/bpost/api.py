"""Web client for bpost Mijn bpost."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

from aiohttp import ClientError, ClientResponseError, ClientSession
from yarl import URL

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi.requests import RequestsError as CurlRequestsError
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment]
    CurlRequestsError = None  # type: ignore[assignment]

LOGIN_URL = "https://www.bpost.be/nl/saml_login?destination=%2Fnl%2Fmijn-bpost"
MIJN_BPOST_URL = "https://www.bpost.be/nl/mijn-bpost?check_logged_in=1"
TRACKING_URL = "https://track.bpost.be/btr/web/#/search?lang=nl&itemCode={tracking_id}"
TRACKING_API_URL = "https://track.bpost.cloud/track/items?itemIdentifier={tracking_id}"
LOGIN_HOST = "login.bpost.be"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_LOGGER = logging.getLogger(__name__)


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
    postal_code: str | None = None
    tracking_url: str | None = None
    tracking_details: Mapping[str, Any] | None = None
    tracking_summary: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] | None = None
    source: str = "account"

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
            "tracking_url": self.tracking_url or _tracking_url(self.tracking_id, self.postal_code),
        }
        if self.status:
            attributes["status"] = self.status
        if self.expected_delivery:
            attributes["expected_delivery"] = self.expected_delivery
        if self.sender:
            attributes["sender"] = self.sender
        if self.postal_code:
            attributes["postal_code"] = self.postal_code
        if self.tracking_details:
            attributes["tracking_details"] = dict(self.tracking_details)
        if self.tracking_summary:
            attributes["tracking_summary"] = dict(self.tracking_summary)
        if self.raw:
            attributes["raw"] = dict(self.raw)
        return attributes


class _FormParser(HTMLParser):
    """Extract forms and script contents from a page."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self.links: list[str] = []
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
        elif tag == "a":
            href = attrs_dict.get("href")
            if href:
                self.links.append(href)

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

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        postal_code: str | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._postal_code = postal_code
        self._logged_in = False
        self._curl_session = CurlAsyncSession(impersonate="chrome") if CurlAsyncSession is not None else None

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

    async def async_close(self) -> None:
        """Close the browser-like session."""
        if self._curl_session is not None:
            await self._curl_session.close()

    async def async_fetch_parcels(self) -> list[Parcel]:
        """Fetch parcels from Mijn bpost."""
        if not self._logged_in:
            await self.async_login()

        html, url = await self._async_get_text(MIJN_BPOST_URL)
        if _is_login_page(html, url):
            self._logged_in = False
            raise BpostAuthenticationError("bpost session expired")

        profile_postal_code = parse_profile_postal_code(html)
        parcels = parse_parcels(html)
        for parcel in parcels:
            parcel.postal_code = parcel.postal_code or profile_postal_code
        return await self._async_enrich_parcels(parcels)

    async def _async_enrich_parcels(self, parcels: list[Parcel]) -> list[Parcel]:
        """Fetch public tracking details for every parcel where possible."""
        enriched: list[Parcel] = []
        for parcel in parcels:
            parcel.postal_code = parcel.postal_code or self._postal_code
            parcel.tracking_url = _tracking_url(parcel.tracking_id, parcel.postal_code)
            try:
                details = await self._async_fetch_tracking_details(parcel.tracking_id, parcel.postal_code)
            except BpostConnectionError:
                _LOGGER.debug("Could not fetch bpost tracking details for %s", parcel.tracking_id, exc_info=True)
                continue

            if details:
                _merge_tracking_details(parcel, details)
            elif parcel.source == "tracking_link":
                _LOGGER.debug(
                    "Ignoring bpost tracking-link candidate %s because tracking returned no details",
                    parcel.tracking_id,
                )
                continue
            enriched.append(parcel)
        return enriched

    async def _async_fetch_tracking_details(
        self,
        tracking_id: str,
        postal_code: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Fetch detailed tracking data for a parcel."""
        url = TRACKING_API_URL.format(tracking_id=quote(tracking_id, safe=""))
        if postal_code:
            url += f"&postalCode={quote(postal_code, safe='')}"
        html, _url = await self._async_get_text(url)
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            return None

        if isinstance(data, Mapping) and data.get("error"):
            return None

        item = _first_tracking_item(data)
        return item if item is not None else data

    async def _async_get_text(self, url: str) -> tuple[str, str]:
        if self._curl_session is not None:
            return await self._async_curl_get_text(url)

        try:
            response = await self._session.get(URL(url, encoded=True), headers=_headers(url))
            response.raise_for_status()
            return await response.text(), str(response.url)
        except (ClientError, ClientResponseError) as exc:
            raise BpostConnectionError(exc) from exc

    async def _async_post_text(self, url: str, data: Mapping[str, str], referer: str) -> tuple[str, str]:
        if self._curl_session is not None:
            return await self._async_curl_post_text(url, data, referer)

        try:
            response = await self._session.post(
                URL(url, encoded=True),
                data=data,
                headers=_headers(url, referer=referer, form=True),
                allow_redirects=True,
            )
            response.raise_for_status()
            return await response.text(), str(response.url)
        except (ClientError, ClientResponseError) as exc:
            raise BpostConnectionError(exc) from exc

    async def _async_curl_get_text(self, url: str) -> tuple[str, str]:
        try:
            response = await self._curl_session.get(url, headers=_headers(url), allow_redirects=True)
        except CurlRequestsError as exc:
            raise BpostConnectionError(exc) from exc

        _raise_for_curl_status(response)
        return response.text, str(response.url)

    async def _async_curl_post_text(self, url: str, data: Mapping[str, str], referer: str) -> tuple[str, str]:
        try:
            response = await self._curl_session.post(
                url,
                data=dict(data),
                headers=_headers(url, referer=referer, form=True),
                allow_redirects=True,
            )
        except CurlRequestsError as exc:
            raise BpostConnectionError(exc) from exc

        _raise_for_curl_status(response)
        return response.text, str(response.url)


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

    for tracking_id, postal_code in _tracking_links_from_html(parser):
        parcels_by_id.setdefault(
            tracking_id,
            Parcel(
                tracking_id=tracking_id,
                name=tracking_id,
                postal_code=postal_code,
                tracking_url=_tracking_url(tracking_id, postal_code),
                source="tracking_link",
            ),
        )

    _LOGGER.debug(
        "Parsed %s bpost parcel candidate(s) from Mijn bpost (%s tracking link(s), %s script(s))",
        len(parcels_by_id),
        len(parser.links),
        len(parser.scripts),
    )
    return [parcel for parcel in parcels_by_id.values() if not parcel.delivered]


def parse_profile_postal_code(html: str) -> str | None:
    """Parse the account/profile postal code from a Mijn bpost HTML response."""
    parser = _parse_html(html)
    candidates: list[str] = []

    for script in parser.scripts:
        for payload in _json_payloads(script):
            for item in _walk_dicts(payload):
                if _looks_like_profile_address(item):
                    postal_code = _first_nested_text(
                        item,
                        (
                            "postalCode",
                            "postal_code",
                            "postcode",
                            "zipCode",
                            "zip",
                        ),
                    )
                    if postal_code and _looks_like_postal_code(postal_code):
                        candidates.append(postal_code)

    if candidates:
        return candidates[0]
    return None


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


def _raise_for_curl_status(response: Any) -> None:
    if response.status_code >= 400:
        raise BpostConnectionError(
            f"{response.status_code}, message='{response.reason}', url='{response.url}'"
        )


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
    postal_code = _first_nested_text(
        item,
        (
            "postalCode",
            "postal_code",
            "postcode",
            "receiverPostcode",
            "receiverPostalCode",
            "zipCode",
            "zip",
        ),
    )
    name = sender or _first_text(item, ("title", "name", "description", "productName")) or tracking_id
    tracking_url = _first_url(
        item,
        (
            "trackingUrl",
            "trackingURL",
            "tracking_url",
            "trackAndTraceUrl",
            "trackAndTraceURL",
            "url",
            "link",
        ),
    )
    postal_code = postal_code or _postal_code_from_url(tracking_url)
    tracking_url = _tracking_url(tracking_id, postal_code) if not tracking_url else _ensure_postal_code(tracking_url, postal_code)

    return Parcel(
        tracking_id=tracking_id,
        name=name,
        status=status,
        expected_delivery=expected_delivery,
        sender=sender,
        postal_code=postal_code,
        tracking_url=tracking_url,
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


def _first_nested_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    text = _first_text(item, keys)
    if text:
        return text

    normalized_keys = {key.lower() for key in keys}
    for key, value in item.items():
        if key.lower() in normalized_keys and isinstance(value, (str, int, float)):
            return str(value).strip()
        if isinstance(value, Mapping):
            nested = _first_nested_text(value, keys)
            if nested:
                return nested
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, Mapping):
                    nested = _first_nested_text(child, keys)
                    if nested:
                        return nested
    return None


def _first_url(item: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        if isinstance(value, Mapping):
            nested = _first_url(value, ("href", "url", "link"))
            if nested:
                return nested
    return None


def _tracking_url(tracking_id: str, postal_code: str | None = None) -> str:
    url = TRACKING_URL.format(tracking_id=quote(tracking_id, safe=""))
    if postal_code:
        url += f"&postalCode={quote(postal_code, safe='')}"
    return url


def _ensure_postal_code(url: str, postal_code: str | None) -> str:
    if not postal_code or "postalCode=" in url:
        return url
    separator = "&" if "?" in url.rsplit("#", maxsplit=1)[-1] else "?"
    return f"{url}{separator}postalCode={quote(postal_code, safe='')}"


def _postal_code_from_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    query_parts = [parsed.query]
    if "?" in parsed.fragment:
        query_parts.append(parsed.fragment.split("?", maxsplit=1)[1])

    for query in query_parts:
        values = parse_qs(query).get("postalCode")
        if values and values[0]:
            return values[0]
    return None


def _tracking_links_from_html(parser: _FormParser) -> list[tuple[str, str | None]]:
    tracking_links: list[tuple[str, str | None]] = []
    for href in parser.links:
        if "itemCode" not in href and "itemCodes" not in href:
            continue

        parsed = urlparse(href)
        host = parsed.hostname or ""
        if host and not host.endswith(("bpost.be", "bpost.cloud")):
            continue

        query_parts = [parsed.query]
        if "?" in parsed.fragment:
            query_parts.append(parsed.fragment.split("?", maxsplit=1)[1])

        postal_code = None
        tracking_ids: list[str] = []
        for query in query_parts:
            params = parse_qs(query)
            postal_code = postal_code or _first_query_value(params, "postalCode")
            tracking_ids.extend(_tracking_ids_from_query_values(params.get("itemCode", [])))
            tracking_ids.extend(_tracking_ids_from_query_values(params.get("itemCodes", [])))

        for tracking_id in tracking_ids:
            tracking_links.append((tracking_id, postal_code))
    return tracking_links


def _first_query_value(params: Mapping[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if values and values[0]:
        return values[0]
    return None


def _tracking_ids_from_query_values(values: list[str]) -> list[str]:
    tracking_ids: list[str] = []
    for value in values:
        for tracking_id in re.split(r"[,;\s]+", value):
            tracking_id = tracking_id.strip()
            if _looks_like_tracking_id(tracking_id):
                tracking_ids.append(tracking_id)
    return tracking_ids


def _looks_like_tracking_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}\d{9}[A-Z]{2}|\d{10,30}", value))


def _looks_like_profile_address(item: Mapping[str, Any]) -> bool:
    keys = {key.lower() for key in item}
    has_postal_code = bool({"postalcode", "postal_code", "postcode", "zipcode", "zip"} & keys)
    if not has_postal_code:
        return False

    address_keys = {
        "address",
        "addresses",
        "city",
        "country",
        "countrycode",
        "municipality",
        "profile",
        "receiver",
        "street",
        "streetname",
        "streetnumber",
    }
    return bool(address_keys & keys)


def _looks_like_postal_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{2,10}", value.strip()))


def _first_tracking_item(data: Any) -> Mapping[str, Any] | None:
    if isinstance(data, Mapping):
        for key in ("items", "item", "shipments", "shipment", "data"):
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], Mapping):
                return value[0]
            if isinstance(value, Mapping):
                return value
        return data
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return data[0]
    return None


def _merge_tracking_details(parcel: Parcel, details: Mapping[str, Any]) -> None:
    parcel.tracking_details = details
    parcel.tracking_summary = _summarize_tracking_details(details)
    parcel.status = _first_text(
        details,
        ("status", "statusText", "statusDescription", "phase", "state", "eventDescription"),
    ) or _latest_event_text(details) or parcel.status
    parcel.expected_delivery = _first_text(
        details,
        (
            "expectedDeliveryDate",
            "expected_delivery_date",
            "deliveryDate",
            "delivery_date",
            "plannedDeliveryDate",
            "planned_delivery_date",
        ),
    ) or parcel.expected_delivery
    parcel.sender = _first_text(details, ("sender", "senderName", "retailerName", "shopName", "shipper")) or parcel.sender
    parcel.postal_code = _first_nested_text(
        details,
        (
            "postalCode",
            "postal_code",
            "postcode",
            "receiverPostcode",
            "receiverPostalCode",
            "zipCode",
            "zip",
        ),
    ) or parcel.postal_code
    parcel.tracking_url = _tracking_url(parcel.tracking_id, parcel.postal_code)


def _summarize_tracking_details(details: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for source, target in (
        ("itemCode", "item_code"),
        ("itemIdentifier", "item_identifier"),
        ("shipmentType", "shipment_type"),
        ("productCategory", "product_category"),
        ("deliveryPreferenceType", "delivery_preference_type"),
    ):
        value = details.get(source)
        if value is not None:
            summary[target] = value

    events = details.get("events")
    if isinstance(events, list):
        summary["events"] = [_summarize_event(event) for event in events if isinstance(event, Mapping)]
    delivery_point = details.get("deliveryPoint")
    if isinstance(delivery_point, Mapping):
        summary["delivery_point"] = _compact_mapping(delivery_point)
    delivery_info = details.get("actualDeliveryInformation")
    if isinstance(delivery_info, Mapping):
        summary["actual_delivery_information"] = _compact_mapping(delivery_info)
    return summary


def _summarize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("date", "time", "timestamp", "status", "eventCode", "eventDescription", "location"):
        value = event.get(key)
        if value is not None:
            summary[_camel_to_snake(key)] = value
    return summary or _compact_mapping(event)


def _latest_event_text(details: Mapping[str, Any]) -> str | None:
    events = details.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if isinstance(event, Mapping):
            text = _first_text(event, ("eventDescription", "status", "description", "label"))
            if text:
                return text
    return None


def _compact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, child in value.items():
        if child is None or isinstance(child, (list, dict)):
            continue
        compact[_camel_to_snake(str(key))] = child
    return compact


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()

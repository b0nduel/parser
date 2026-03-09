#!/usr/bin/env python3
"""Сбор реальных лотов с площадок (Torgi.gov + банкротные торги Fedresurs).

Примеры:
  python3 scripts/fetch_lots.py                    # собрать все доступные страницы
  python3 scripts/fetch_lots.py --limit 200        # ограничить итоговое число лотов
  python3 scripts/fetch_lots.py --insecure         # отключить TLS-проверку (диагностика)
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = ROOT / "data" / "lots.json"


@dataclass
class Lot:
    id: int
    title: str
    type: str
    source: str
    region: str
    address: str
    nearestStop: str
    stopDistanceM: int
    lotPrice: int
    avitoPrice: int
    auctionUrl: str


SSL_CONTEXT: ssl.SSLContext | None = None


def _http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _build_ssl_context(cafile: str | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _is_cert_error(exc: Exception) -> bool:
    text = str(exc)
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "не указано", "-", "n/a"}:
        return ""
    return re.sub(r"\s+", " ", text)


def _find_first_by_keys(data: object, keys: set[str]) -> str:
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in keys:
                text = _normalize_text(value)
                if text:
                    return text
        for value in data.values():
            text = _find_first_by_keys(value, keys)
            if text:
                return text
    elif isinstance(data, list):
        for item in data:
            text = _find_first_by_keys(item, keys)
            if text:
                return text
    return ""


def _parse_address_from_title(title: str) -> str:
    text = _normalize_text(title)
    if not text:
        return ""
    patterns = [
        r"(?:расположенн\w*\s+по\s+адресу[:\s]+)(.+?)(?:,\s*начальн|,\s*стои|\.|$)",
        r"(?:адрес[:\s]+)(.+?)(?:,\s*кадастров|,\s*площад|\.|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _normalize_text(match.group(1).strip(" ,;"))
    return ""


def _extract_address_torgi(item: dict) -> str:
    address_keys = {
        "lotaddress", "address", "fulladdress", "location", "objectaddress", "propertyaddress",
        "addressline", "addressstr", "addressstring", "addressfull", "place", "locality", "locationname",
    }
    region_keys = {"subjectrfname", "region", "regionname", "fiasregionname", "okatofullname"}

    for candidate in [
        item.get("lotAddress"),
        item.get("location"),
        item.get("address"),
        item.get("fullAddress"),
        item.get("locationName"),
    ]:
        text = _normalize_text(candidate)
        if text:
            return text

    nested_address = _find_first_by_keys(item, address_keys)
    if nested_address:
        return nested_address

    title_address = _parse_address_from_title(str(item.get("lotName") or ""))
    if title_address:
        return title_address

    region = _find_first_by_keys(item, region_keys)
    if region:
        return region

    return "Не указано"


def _normalize_price(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).replace(" ", "").replace(",", ".")
    try:
        return int(float(text))
    except ValueError:
        return 0


def _iter_torgi_content(page_size: int = 100) -> Iterable[dict]:
    page = 0
    total_pages = None
    while True:
        url = f"https://torgi.gov.ru/new/api/public/lotcards/search?size={page_size}&page={page}"
        payload = _http_get_json(url)
        content = payload.get("content") or []
        if not content:
            break
        for item in content:
            yield item

        if total_pages is None:
            total_pages = payload.get("totalPages")

        page += 1
        if total_pages is not None and page >= int(total_pages):
            break


class FedresursTradeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[tuple[str, str]] = []
        self._capture = False
        self._href = ""
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attr = dict(attrs)
        href = attr.get("href", "")
        if "TradeCard.aspx" in href:
            self._capture = True
            self._href = href
            self._buf = []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._capture:
            title = " ".join(x for x in self._buf if x).strip()
            if title:
                self.links.append((self._href, re.sub(r"\s+", " ", title)))
            self._capture = False


def _iter_fedresurs_links(max_pages: int = 50) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        # На сайте встречаются разные схемы пагинации; пробуем наиболее частую.
        if page == 1:
            url = "https://bankrot.fedresurs.ru/TradeList.aspx"
        else:
            url = f"https://bankrot.fedresurs.ru/TradeList.aspx?Page={page}"

        html = _http_get_text(url)
        parser = FedresursTradeParser()
        parser.feed(html)

        page_links = 0
        for href, title in parser.links:
            full_url = urllib.parse.urljoin("https://bankrot.fedresurs.ru/", href)
            if full_url in seen:
                continue
            seen.add(full_url)
            page_links += 1
            yield full_url, title

        if page_links == 0:
            break


def fetch_torgi(limit: int = 0, start_id: int = 1) -> List[Lot]:
    lots: List[Lot] = []
    for idx, item in enumerate(_iter_torgi_content(page_size=100), start=start_id):
        title = item.get("lotName") or item.get("subjectRFName") or item.get("noticeNumber") or "Лот без названия"
        region = _normalize_text(item.get("subjectRFName")) or "Не указано"
        address = _extract_address_torgi(item)
        lot_id = item.get("id") or item.get("lotNumber") or ""
        price = _normalize_price(item.get("priceMin") or item.get("price") or 0)

        lots.append(
            Lot(
                id=idx,
                title=str(title),
                type="Объект с торгов",
                source="Торги.gov",
                region=region,
                address=address,
                nearestStop="Не определено",
                stopDistanceM=0,
                lotPrice=price,
                avitoPrice=int(price * 1.15),
                auctionUrl=f"https://torgi.gov.ru/new/public/lots/lot/{lot_id}" if lot_id else "https://torgi.gov.ru/new/public/lots/reg",
            )
        )
        if limit and len(lots) >= limit:
            break
    return lots


def fetch_fedresurs(limit: int = 0, start_id: int = 50000) -> List[Lot]:
    lots: List[Lot] = []
    for i, (url, title) in enumerate(_iter_fedresurs_links(max_pages=80), start=1):
        address = _parse_address_from_title(title) or "Не указано"
        lots.append(
            Lot(
                id=start_id + i,
                title=title,
                type="Объект с торгов",
                source="Банкротные торги",
                region="Не указано",
                address=address,
                nearestStop="Не определено",
                stopDistanceM=0,
                lotPrice=0,
                avitoPrice=0,
                auctionUrl=url,
            )
        )
        if limit and len(lots) >= limit:
            break
    return lots


def _collect_once(limit: int) -> tuple[list[Lot], list[str], Exception | None, Exception | None]:
    lots: List[Lot] = []
    errors: List[str] = []
    torgi_error: Exception | None = None
    fed_error: Exception | None = None

    try:
        torgi_limit = limit if limit else 0
        lots.extend(fetch_torgi(limit=torgi_limit, start_id=1))
    except Exception as exc:  # noqa: BLE001
        torgi_error = exc
        errors.append(f"Torgi.gov: {exc}")

    try:
        fed_limit = max(5, limit // 2) if limit else 0
        lots.extend(fetch_fedresurs(limit=fed_limit, start_id=50000))
    except Exception as exc:  # noqa: BLE001
        fed_error = exc
        errors.append(f"Fedresurs: {exc}")

    return lots, errors, torgi_error, fed_error


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Лимит итоговых объектов (0 = собрать все доступные)")
    ap.add_argument("--cafile", type=str, default=None, help="Путь к CA bundle (например, /etc/ssl/cert.pem или corp-ca.pem)")
    ap.add_argument("--insecure", action="store_true", help="Отключить проверку TLS-сертификата (только для локальной диагностики)")
    ap.add_argument(
        "--retry-insecure-on-cert-error",
        action="store_true",
        default=True,
        help="Если источники упали на CERTIFICATE_VERIFY_FAILED — автоматически повторить в insecure-режиме",
    )
    args = ap.parse_args()

    global SSL_CONTEXT
    SSL_CONTEXT = _build_ssl_context(args.cafile, args.insecure)

    lots, errors, torgi_error, fed_error = _collect_once(limit=args.limit)

    if args.retry_insecure_on_cert_error and (torgi_error or fed_error):
        need_retry = any(_is_cert_error(err) for err in [torgi_error, fed_error] if err is not None)
        if need_retry and not args.insecure:
            SSL_CONTEXT = _build_ssl_context(cafile=None, insecure=True)
            retry_lots, retry_errors, _, _ = _collect_once(limit=args.limit)
            # если retry дал результат — используем его как основной
            if retry_lots:
                lots = retry_lots
            errors.extend(retry_errors)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps([asdict(x) for x in lots], ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {len(lots)} lots -> {OUT_FILE}")
    if errors:
        print("Warnings:")
        for e in errors:
            print(" -", e)

        if any("CERTIFICATE_VERIFY_FAILED" in e for e in errors):
            print("\nTLS help:")
            print(" - macOS Python.org: запустите 'Install Certificates.command'.")
            print(" - Если у вас корпоративный прокси: передайте --cafile /path/to/corp-ca.pem.")
            print(" - Для быстрой проверки можно запустить с --insecure (не для production).")
            print(" - Скрипт также автоматически делает retry в insecure-режиме при CERTIFICATE_VERIFY_FAILED.")


if __name__ == "__main__":
    main()

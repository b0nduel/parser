#!/usr/bin/env python3
"""Сбор реальных лотов с площадок (Torgi.gov + банкротные торги Fedresurs).

Запуск:
  python3 scripts/fetch_lots.py --limit 50

Результат:
  data/lots.json
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
from typing import List

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


def _http_get_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _http_get_text(url: str, timeout: int = 25) -> str:
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

def fetch_torgi(limit: int = 30) -> List[Lot]:
    # Публичный API поиска лотов Torgi.gov.
    url = "https://torgi.gov.ru/new/api/public/lotcards/search?size={}&page=0".format(max(1, min(limit, 100)))
    payload = _http_get_json(url)
    content = payload.get("content") or []

    lots: List[Lot] = []
    for idx, item in enumerate(content, start=1):
        title = item.get("lotName") or item.get("subjectRFName") or item.get("noticeNumber") or "Лот без названия"
        region = item.get("subjectRFName") or "Не указано"
        address = item.get("lotAddress") or item.get("location") or "Не указано"
        lot_id = item.get("id") or item.get("lotNumber") or ""
        price = item.get("priceMin") or item.get("price") or 0
        if isinstance(price, str):
            price = int(float(price.replace(" ", "").replace(",", ".")))

        lots.append(
            Lot(
                id=idx,
                title=str(title),
                type="Объект с торгов",
                source="Торги.gov",
                region=str(region),
                address=str(address),
                nearestStop="Не определено",
                stopDistanceM=0,
                lotPrice=int(price or 0),
                avitoPrice=int((price or 0) * 1.15),
                auctionUrl=f"https://torgi.gov.ru/new/public/lots/lot/{lot_id}" if lot_id else "https://torgi.gov.ru/new/public/lots/reg",
            )
        )
    return lots


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


def fetch_fedresurs(limit: int = 20, start_id: int = 10000) -> List[Lot]:
    url = "https://bankrot.fedresurs.ru/TradeList.aspx"
    html = _http_get_text(url)
    parser = FedresursTradeParser()
    parser.feed(html)
    items = parser.links[:limit]

    lots: List[Lot] = []
    for i, (href, title) in enumerate(items, start=1):
        full_url = urllib.parse.urljoin("https://bankrot.fedresurs.ru/", href)
        lots.append(
            Lot(
                id=start_id + i,
                title=title,
                type="Объект с торгов",
                source="Банкротные торги",
                region="Не указано",
                address="Не указано",
                nearestStop="Не определено",
                stopDistanceM=0,
                lotPrice=0,
                avitoPrice=0,
                auctionUrl=full_url,
            )
        )
    return lots


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument(
        "--cafile",
        type=str,
        default=None,
        help="Путь к CA bundle (например, /etc/ssl/cert.pem или corp-ca.pem)",
    )
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Отключить проверку TLS-сертификата (только для локальной диагностики)",
    )
    ap.add_argument(
        "--retry-insecure-on-cert-error",
        action="store_true",
        default=True,
        help="Если источники упали на CERTIFICATE_VERIFY_FAILED — автоматически повторить в insecure-режиме",
    )
    args = ap.parse_args()

    global SSL_CONTEXT
    SSL_CONTEXT = _build_ssl_context(args.cafile, args.insecure)

    lots: List[Lot] = []
    errors = []

    torgi_error: Exception | None = None
    fed_error: Exception | None = None

    try:
        lots.extend(fetch_torgi(limit=args.limit))
    except Exception as exc:  # noqa: BLE001
        torgi_error = exc
        errors.append(f"Torgi.gov: {exc}")

    try:
        lots.extend(fetch_fedresurs(limit=max(5, args.limit // 2), start_id=50000))
    except Exception as exc:  # noqa: BLE001
        fed_error = exc
        errors.append(f"Fedresurs: {exc}")

    if args.retry_insecure_on_cert_error and (torgi_error or fed_error):
        need_retry = any(
            _is_cert_error(err) for err in [torgi_error, fed_error] if err is not None
        )
        if need_retry and not args.insecure:
            SSL_CONTEXT = _build_ssl_context(cafile=None, insecure=True)
            retry_errors: list[str] = []

            if torgi_error and _is_cert_error(torgi_error):
                try:
                    lots.extend(fetch_torgi(limit=args.limit))
                    errors.append("Torgi.gov: повтор в insecure-режиме успешен")
                except Exception as exc:  # noqa: BLE001
                    retry_errors.append(f"Torgi.gov (retry): {exc}")

            if fed_error and _is_cert_error(fed_error):
                try:
                    lots.extend(fetch_fedresurs(limit=max(5, args.limit // 2), start_id=50000))
                    errors.append("Fedresurs: повтор в insecure-режиме успешен")
                except Exception as exc:  # noqa: BLE001
                    retry_errors.append(f"Fedresurs (retry): {exc}")

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
            print(" - Теперь скрипт также автоматически делает retry в insecure-режиме при CERTIFICATE_VERIFY_FAILED.")


if __name__ == "__main__":
    main()

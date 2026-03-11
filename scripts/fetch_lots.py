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
import os
import urllib.request
import urllib.error
import time
import socket
import subprocess
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

REQUEST_TIMEOUT = 15
MAX_PAGES_TORGI = 200
MAX_PAGES_FED = 80
RETRIES = 2
RETRY_DELAY = 1.0
USE_CURL_FALLBACK = True
PROXY_URL: str | None = None
PROXY_INSECURE = False
CURL_FORCE_TLS12 = True


def _http_open_with_curl(url: str, timeout: int = 15) -> bytes:
    # На macOS/корп. сети curl часто лучше обрабатывает системные proxy/cert цепочки.
    cmd = [
        "curl", "-sS", "-L",
        "--connect-timeout", str(min(10, timeout)),
        "--max-time", str(timeout),
        "--http1.1",
        "-A", "Mozilla/5.0",
    ]
    if CURL_FORCE_TLS12:
        cmd.append("--tlsv1.2")
    if PROXY_URL:
        cmd.extend(["--proxy", PROXY_URL])
        if PROXY_INSECURE:
            cmd.append("--proxy-insecure")
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="ignore") or f"curl exit {proc.returncode}")
    return proc.stdout


def _http_open(req: urllib.request.Request, timeout: int = 15):
    effective_timeout = REQUEST_TIMEOUT if timeout is None else timeout
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            if PROXY_URL:
                proxy_handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
                https_handler = urllib.request.HTTPSHandler(context=SSL_CONTEXT)
                opener = urllib.request.build_opener(proxy_handler, https_handler)
                return opener.open(req, timeout=effective_timeout)
            return urllib.request.urlopen(req, timeout=effective_timeout, context=SSL_CONTEXT)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            last_exc = exc
            if attempt >= RETRIES:
                raise
            time.sleep(RETRY_DELAY * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected network state")


def _http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _http_open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        if not USE_CURL_FALLBACK:
            raise
        raw = _http_open_with_curl(url, timeout=timeout)
        return json.loads(raw.decode("utf-8", errors="ignore"))


def _http_get_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _http_open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        if not USE_CURL_FALLBACK:
            raise
        raw = _http_open_with_curl(url, timeout=timeout)
        return raw.decode("utf-8", errors="ignore")


def _build_ssl_context(cafile: str | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _is_cert_error(exc: Exception) -> bool:
    text = str(exc)
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def _is_timeout_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "handshake operation timed out" in text


def _build_proxy_url(
    proxy: str | None,
    proxy_host: str | None,
    proxy_port: int | None,
    proxy_user: str | None,
    proxy_password: str | None,
) -> str | None:
    if proxy:
        return proxy

    host = proxy_host or os.getenv("HTTPS_PROXY_HOST") or os.getenv("HTTP_PROXY_HOST")
    port_raw = proxy_port or os.getenv("HTTPS_PROXY_PORT") or os.getenv("HTTP_PROXY_PORT")
    if not host or not port_raw:
        return None

    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"Invalid proxy port: {port_raw}")

    user = proxy_user if proxy_user is not None else os.getenv("HTTPS_PROXY_USER") or os.getenv("HTTP_PROXY_USER")
    password = proxy_password if proxy_password is not None else os.getenv("HTTPS_PROXY_PASSWORD") or os.getenv("HTTP_PROXY_PASSWORD")

    if user:
        pwd = password or ""
        auth = f"{urllib.parse.quote(user)}:{urllib.parse.quote(pwd)}@"
    else:
        auth = ""

    return f"http://{auth}{host}:{port}"


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


def _iter_torgi_content(page_size: int = 100, max_pages: int | None = None) -> Iterable[dict]:
    page = 0
    total_pages = None
    effective_max = MAX_PAGES_TORGI if max_pages is None else max_pages
    while page < effective_max:
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


def _iter_fedresurs_links(max_pages: int | None = None) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    effective_max = MAX_PAGES_FED if max_pages is None else max_pages
    for page in range(1, effective_max + 1):
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


def fetch_torgi(limit: int = 0, start_id: int = 1, max_pages: int | None = None) -> List[Lot]:
    lots: List[Lot] = []
    for idx, item in enumerate(_iter_torgi_content(page_size=100, max_pages=max_pages), start=start_id):
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


def fetch_fedresurs(limit: int = 0, start_id: int = 50000, max_pages: int | None = None) -> List[Lot]:
    lots: List[Lot] = []
    for i, (url, title) in enumerate(_iter_fedresurs_links(max_pages=max_pages), start=1):
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


def _collect_once(limit: int, max_pages_torgi: int | None = None, max_pages_fed: int | None = None) -> tuple[list[Lot], list[str], Exception | None, Exception | None]:
    lots: List[Lot] = []
    errors: List[str] = []
    torgi_error: Exception | None = None
    fed_error: Exception | None = None

    try:
        torgi_limit = limit if limit else 0
        lots.extend(fetch_torgi(limit=torgi_limit, start_id=1, max_pages=max_pages_torgi))
    except Exception as exc:  # noqa: BLE001
        torgi_error = exc
        errors.append(f"Torgi.gov: {exc}")

    try:
        fed_limit = max(5, limit // 2) if limit else 0
        lots.extend(fetch_fedresurs(limit=fed_limit, start_id=50000, max_pages=max_pages_fed))
    except Exception as exc:  # noqa: BLE001
        fed_error = exc
        errors.append(f"Fedresurs: {exc}")

    return lots, errors, torgi_error, fed_error


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Лимит итоговых объектов (0 = собрать все доступные)")
    ap.add_argument("--request-timeout", type=int, default=15, help="Таймаут HTTP-запроса в секундах")
    ap.add_argument("--retries", type=int, default=2, help="Количество повторов сетевого запроса при ошибке")
    ap.add_argument("--retry-delay", type=float, default=1.0, help="Базовая задержка между ретраями (сек)")
    ap.add_argument("--no-curl-fallback", action="store_true", help="Отключить fallback через curl")
    ap.add_argument("--proxy", type=str, default=None, help="Явный HTTPS proxy, например http://user:pass@host:port")
    ap.add_argument("--proxy-insecure", action="store_true", help="Отключить TLS-проверку сертификата прокси для curl")
    ap.add_argument("--no-curl-tls12", action="store_true", help="Не форсировать TLS1.2 для curl")
    ap.add_argument("--proxy-host", type=str, default=None, help="Хост прокси (если не указываете полный --proxy)")
    ap.add_argument("--proxy-port", type=int, default=None, help="Порт прокси")
    ap.add_argument("--proxy-user", type=str, default=None, help="Логин прокси")
    ap.add_argument("--proxy-password", type=str, default=None, help="Пароль прокси")
    ap.add_argument("--allow-empty-write", action="store_true", help="Разрешить перезапись data/lots.json пустым массивом")
    ap.add_argument("--max-pages-torgi", type=int, default=200, help="Максимум страниц Torgi.gov за запуск")
    ap.add_argument("--max-pages-fed", type=int, default=80, help="Максимум страниц Fedresurs за запуск")
    ap.add_argument("--cafile", type=str, default=None, help="Путь к CA bundle (например, /etc/ssl/cert.pem или corp-ca.pem)")
    ap.add_argument("--insecure", action="store_true", help="Отключить проверку TLS-сертификата (только для локальной диагностики)")
    ap.add_argument(
        "--retry-insecure-on-cert-error",
        action="store_true",
        default=True,
        help="Если источники упали на CERTIFICATE_VERIFY_FAILED — автоматически повторить в insecure-режиме",
    )
    args = ap.parse_args()

    global SSL_CONTEXT, REQUEST_TIMEOUT, MAX_PAGES_TORGI, MAX_PAGES_FED, RETRIES, RETRY_DELAY, USE_CURL_FALLBACK, PROXY_URL, PROXY_INSECURE, CURL_FORCE_TLS12
    SSL_CONTEXT = _build_ssl_context(args.cafile, args.insecure)
    REQUEST_TIMEOUT = max(3, int(args.request_timeout))
    MAX_PAGES_TORGI = max(1, int(args.max_pages_torgi))
    MAX_PAGES_FED = max(1, int(args.max_pages_fed))
    RETRIES = max(0, int(args.retries))
    RETRY_DELAY = max(0.1, float(args.retry_delay))
    USE_CURL_FALLBACK = not args.no_curl_fallback
    PROXY_URL = _build_proxy_url(args.proxy, args.proxy_host, args.proxy_port, args.proxy_user, args.proxy_password)
    PROXY_INSECURE = args.proxy_insecure
    CURL_FORCE_TLS12 = not args.no_curl_tls12

    lots, errors, torgi_error, fed_error = _collect_once(limit=args.limit, max_pages_torgi=MAX_PAGES_TORGI, max_pages_fed=MAX_PAGES_FED)

    if args.retry_insecure_on_cert_error and not args.insecure:
        insecure_ctx = _build_ssl_context(cafile=None, insecure=True)

        if torgi_error and _is_cert_error(torgi_error):
            prev_ctx = SSL_CONTEXT
            SSL_CONTEXT = insecure_ctx
            try:
                recovered = fetch_torgi(limit=args.limit if args.limit else 0, start_id=1, max_pages=MAX_PAGES_TORGI)
                lots = [x for x in lots if x.source != "Торги.gov"] + recovered
                errors.append("Torgi.gov: восстановлено через insecure retry")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Torgi.gov (retry): {exc}")
            finally:
                SSL_CONTEXT = prev_ctx

        if fed_error and _is_cert_error(fed_error):
            prev_ctx = SSL_CONTEXT
            SSL_CONTEXT = insecure_ctx
            try:
                recovered = fetch_fedresurs(
                    limit=max(5, args.limit // 2) if args.limit else 0,
                    start_id=50000,
                    max_pages=MAX_PAGES_FED,
                )
                lots = [x for x in lots if x.source != "Банкротные торги"] + recovered
                errors.append("Fedresurs: восстановлено через insecure retry")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Fedresurs (retry): {exc}")
            finally:
                SSL_CONTEXT = prev_ctx

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    should_write = True
    if not lots and errors and not args.allow_empty_write and OUT_FILE.exists():
        old_raw = OUT_FILE.read_text(encoding="utf-8").strip()
        if old_raw and old_raw != "[]":
            should_write = False

    if should_write:
        OUT_FILE.write_text(json.dumps([asdict(x) for x in lots], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {len(lots)} lots -> {OUT_FILE}")
    else:
        print(f"Saved 0 lots, but kept previous non-empty file -> {OUT_FILE}")
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
        if any("timed out" in e.lower() for e in errors):
            print("\nNetwork timeout help:")
            print(" - Уменьшите страницы: --max-pages-torgi 20 --max-pages-fed 10")
            print(" - Увеличьте таймаут: --request-timeout 30")
            print(" - Добавьте ретраи: --retries 3 --retry-delay 2")
            print(" - Включен fallback через curl (можно отключить флагом --no-curl-fallback)")
            print(" - Если у вас корпоративная сеть: укажите --proxy http://host:port")
            print(" - Для proxy с авторизацией: --proxy-host HOST --proxy-port PORT --proxy-user LOGIN --proxy-password PASS")
        if any("ssl_error_syscall" in e.lower() for e in errors):
            print("\nProxy SSL help:")
            print(" - Добавьте флаг --proxy-insecure")
            print(" - Оставьте принудительный TLS1.2 (по умолчанию включен)")
            print(" - Если не помогло, попробуйте --no-curl-fallback для диагностики urllib")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user (Ctrl+C). Partial results may be incomplete.")

import re

import requests
from bs4 import BeautifulSoup, SoupStrainer

BASE = "https://ideco.com.jo"
# الفواتير المسددة وغير المسددة أصبحتا طلبَي AJAX منفصلَين في الموقع الجديد
PAID_URL = f"{BASE}/Website/EServices/SubscriberReceivableLinks"
UNPAID_URL = f"{BASE}/Website/EServices/SubscriberReceivableLinksNotBuyed"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": PAID_URL,
}

# جلسة واحدة مشتركة لكل الطلبات: تبقي اتصال HTTPS مفتوحاً (keep-alive)
_session = requests.Session()
_session.headers.update(_HEADERS)

# الموقع الجديد يرجع جزء HTML فيه الجدول فقط — لا ViewState ولا تحديث دوري
_RESULT_STRAINER = SoupStrainer(("table", "input", "p"))

# أعمدة تُحذف من العرض لأنها روابط/فارغة
_DROP_HEADERS = ("", "اختيار", "التفاصيل")


class IDECOFetchError(Exception):
    """تعذر الاتصال بموقع شركة الكهرباء."""


def start_background_refresh():
    """لم يعد هناك ViewState يحتاج تسخيناً؛ أبقينا الدالة لتوافق الاستدعاء."""


def _norm(text: str) -> str:
    return " ".join(text.split())


def _to_number(text: str):
    cleaned = text.replace(",", "").strip()
    return float(cleaned) if re.fullmatch(r"-?\d+(\.\d+)?", cleaned) else None


def _post_lookup(url: str, subscriber: str) -> BeautifulSoup:
    try:
        result = _session.post(url, data={"customerNumber": subscriber}, timeout=30)
        result.raise_for_status()
    except requests.RequestException as exc:
        raise IDECOFetchError(str(exc)) from exc
    return BeautifulSoup(result.text, "lxml", parse_only=_RESULT_STRAINER)


def fetch_receivable(subscriber: str) -> dict:
    """جلب الذمم المستحقة من موقع IDECO الجديد.

    Returns {"status": "found", "total": "...", "unpaid": {...}, "paid": {...}}
    or     {"status": "no_invoices", "message": "..."}
    """
    unpaid_soup = _post_lookup(UNPAID_URL, subscriber)
    paid_soup = _post_lookup(PAID_URL, subscriber)

    unpaid = _extract_grid(unpaid_soup, kind="unpaid")
    paid = _extract_grid(paid_soup, kind="paid")
    if unpaid is None and paid is None:
        return {"status": "no_invoices", "message": "لا توجد فواتير لهذا الاشتراك"}

    return {
        "status": "found",
        "total": _extract_total(unpaid_soup, unpaid),
        "unpaid": unpaid,
        "paid": paid,
    }


def _extract_grid(soup: BeautifulSoup, kind: str):
    """استخراج جدول الفواتير (غير المسددة أو المسددة) من الصفحة.

    جدول المسددة يحوي "القيمة المسددة"، وجدول غير المسددة لا يحويها.
    """
    for table in soup.find_all("table"):
        if table.find("table"):  # تخطَّ جداول التخطيط الحاوية لجداول أخرى
            continue
        headers = [_norm(th.get_text()) for th in table.find_all("th")]
        if "شهر الإصدار" not in headers:
            continue
        is_paid = "القيمة المسددة" in headers
        if (kind == "paid") != is_paid:
            continue

        keep = [i for i, h in enumerate(headers) if h not in _DROP_HEADERS]
        rows = []
        for tr in table.find_all("tr"):
            cells = [_norm(td.get_text()) for td in tr.find_all("td")]
            if cells and any(cells):
                rows.append([cells[i] if i < len(cells) else "" for i in keep])
        if not rows:
            continue  # جدول بلا بيانات = لا فواتير من هذا النوع
        return {"headers": [headers[i] for i in keep], "rows": rows}
    return None


def _extract_total(soup: BeautifulSoup, unpaid: dict | None):
    """قراءة "مجموع الذمم" من الحقل الرسمي، وإن غاب نجمع القيم المتبقية."""
    for p in soup.find_all("p"):
        if "مجموع الذمم" in _norm(p.get_text()):
            field = p.find("input")
            if field and field.get("value", "").strip():
                return field["value"].strip()

    if unpaid:
        idx = next(
            (i for i, h in enumerate(unpaid["headers"]) if h == "القيمة المتبقية"),
            None,
        )
        if idx is not None:
            values = [
                _to_number(r[idx])
                for r in unpaid["rows"]
                if idx < len(r) and _to_number(r[idx]) is not None
            ]
            if values:
                return f"{sum(values):.3f}"
    return None

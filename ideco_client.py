import re
import threading
import time

import requests
from bs4 import BeautifulSoup, SoupStrainer

URL = "https://www.ideco.com.jo/portal/WebForms/SubscriberReceivableLinks.aspx"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# جلسة واحدة مشتركة لكل الطلبات: تبقي اتصال HTTPS مفتوحاً (keep-alive)
# بدل فتح اتصال جديد ومصافحة TLS كاملة مع كل طلب — التحديث الدوري للـ
# ViewState يمر على نفس الجلسة فيبقيها "دافئة" باستمرار.
_session = requests.Session()
_session.headers.update(_HEADERS)

# فلاتر تحليل مخصّصة: نبني شجرة DOM فقط لما نحتاجه فعلياً بدل الصفحة
# كاملة (صفحات ASP.NET فيها __VIEWSTATE قد يصل لعشرات الكيلوبايتات).
_HIDDEN_INPUTS_STRAINER = SoupStrainer("input", attrs={"type": "hidden"})
_RESULT_STRAINER = SoupStrainer(("table", "input", "span"))

CPH = "ctl00_ContentPlaceHolder1_"
CUSTOMER_NO = "ctl00$ContentPlaceHolder1$txtCustomerNo"
SUBMIT_BTN = "ctl00$ContentPlaceHolder1$btnGetInvoices2"
CITY_SELECT = "ctl00$ContentPlaceHolder1$ddlCities"

# أعمدة تُحذف من العرض لأنها روابط/فارغة
_DROP_HEADERS = ("", "اختيار")

# الـ ViewState يصلح لأي رقم اشتراك؛ نحدّثه في الخلفية كل 30 دقيقة
_STATE_TTL = 1800
_state_lock = threading.Lock()
_state = {"fields": None, "time": 0.0}


class IDECOFetchError(Exception):
    """تعذر الاتصال بموقع شركة الكهرباء."""


def _fetch_page_state() -> dict:
    try:
        page = _session.get(URL, timeout=30)
        page.raise_for_status()
    except requests.RequestException as exc:
        raise IDECOFetchError(str(exc)) from exc
    soup = BeautifulSoup(page.text, "lxml", parse_only=_HIDDEN_INPUTS_STRAINER)
    return {
        h.get("name"): h.get("value", "")
        for h in soup.find_all("input")
        if h.get("name")
    }


def _get_state(force: bool = False) -> dict:
    with _state_lock:
        stale = time.monotonic() - _state["time"] >= _STATE_TTL
        if _state["fields"] is None or stale or force:
            _state["fields"] = _fetch_page_state()
            _state["time"] = time.monotonic()
        return dict(_state["fields"])


def _warm_state_loop():
    while True:
        try:
            _get_state()
        except IDECOFetchError:
            pass
        time.sleep(_STATE_TTL)


def start_background_refresh():
    """تشغيل تحديث الـ ViewState دورياً ليبقى الرد بطلب واحد."""
    t = threading.Thread(target=_warm_state_loop, daemon=True)
    t.start()


def _norm(text: str) -> str:
    return " ".join(text.split())


def _to_number(text: str):
    cleaned = text.replace(",", "").strip()
    return float(cleaned) if re.fullmatch(r"-?\d+(\.\d+)?", cleaned) else None


def _post_lookup(subscriber: str, fields: dict) -> BeautifulSoup:
    payload = dict(fields)
    payload[CUSTOMER_NO] = subscriber
    payload[CITY_SELECT] = "-1"
    payload[SUBMIT_BTN] = ""
    try:
        result = _session.post(URL, data=payload, timeout=30)
        result.raise_for_status()
    except requests.RequestException as exc:
        raise IDECOFetchError(str(exc)) from exc
    return BeautifulSoup(result.text, "lxml", parse_only=_RESULT_STRAINER)


def _looks_valid(soup: BeautifulSoup) -> bool:
    return (
        soup.find("input", id=f"{CPH}txtSum") is not None
        or soup.find("span", id=f"{CPH}lblNoInvoices") is not None
    )


def fetch_receivable(subscriber: str) -> dict:
    """جلب الذمم المستحقة من موقع IDECO بطلب واحد (ViewState مخزّن مسبقاً).

    Returns {"status": "found", "total": "...", "unpaid": {...}, "paid": {...}}
    or     {"status": "no_invoices", "message": "..."}
    """
    soup = _post_lookup(subscriber, _get_state())

    # ViewState منتهي الصلاحية؟ جدّده وأعد المحاولة مرة واحدة
    if not _looks_valid(soup):
        soup = _post_lookup(subscriber, _get_state(force=True))

    no_msg = soup.find("span", id=f"{CPH}lblNoInvoices")
    if no_msg and _norm(no_msg.get_text()):
        return {"status": "no_invoices", "message": _norm(no_msg.get_text())}

    unpaid = _extract_grid(soup, kind="unpaid")
    paid = _extract_grid(soup, kind="paid")
    if unpaid is None and paid is None:
        return {"status": "no_invoices", "message": "لا توجد فواتير لهذا الاشتراك"}

    return {
        "status": "found",
        "total": _extract_total(soup, unpaid),
        "unpaid": unpaid,
        "paid": paid,
    }


def _extract_grid(soup: BeautifulSoup, kind: str):
    """استخراج جدول الفواتير (غير المسددة أو المسددة) من الصفحة.

    جدول غير المسددة يحوي "القيمة المطلوبة" دون "القيمة المسددة".
    جدول المسددة يحوي "القيمة المسددة".
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
        return {"headers": [headers[i] for i in keep], "rows": rows}
    return None


def _extract_total(soup: BeautifulSoup, unpaid: dict | None):
    """قراءة "مجموع الذمم" من الحقل الرسمي txtSum، وإن غاب نجمع القيم المطلوبة."""
    txt_sum = soup.find("input", id=f"{CPH}txtSum")
    if txt_sum and txt_sum.get("value", "").strip():
        return txt_sum["value"].strip()

    if unpaid:
        idx = next(
            (i for i, h in enumerate(unpaid["headers"]) if h == "القيمة المطلوبة"),
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

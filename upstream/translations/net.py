"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    "The server refused the download (HTTP {code}).": "سرور دانلود را رد کرد (HTTP {code}).",
    "The server is not answering properly (HTTP {code}). Try again later.":
        "سرور پاسخ درستی نمی‌دهد (HTTP {code}). کمی بعد دوباره امتحان کن.",
    "The connection keeps failing, so the download stopped: {error}":
        "اتصال بارها قطع شد و دانلود متوقف شد: {error}",
    "The connection closed before the whole file arrived": "اتصال پیش از رسیدن کامل فایل بسته شد",
    "Couldn't save the download: {error}": "ذخیرهٔ فایل دانلود‌شده ممکن نشد: {error}",
    "Refused: this download isn't served over HTTPS and has no published checksum.":
        "رد شد: این فایل با HTTPS ارائه نمی‌شود و چک‌سام منتشرشده‌ای هم ندارد.",
    "Refused: the server redirected the download to a less secure address.":
        "رد شد: سرور دانلود را به آدرسی ناامن‌تر هدایت کرد.",
    "The proxy address in Settings isn't valid.": "آدرس پروکسی در تنظیمات معتبر نیست.",
    "This PC is on a metered connection. Download updates anyway?":
        "اتصال این رایانه حجمی است. با این حال به‌روزرسانی‌ها دانلود شوند؟",
    "Updates can be large and may count against your data plan.":
        "به‌روزرسانی‌ها ممکن است حجیم باشند و از بستهٔ اینترنت کم کنند.",
    "Download anyway": "با این حال دانلود کن",
    "Not now": "حالا نه",
    "Not updated: this PC is on a metered connection.": "به‌روزرسانی نشد: اتصال این رایانه حجمی است.",
    "The battery is at {percent}% and the PC is not plugged in. Update anyway?":
        "شارژ باتری {percent}٪ است و رایانه به برق وصل نیست. با این حال به‌روزرسانی شود؟",
    "Battery saver is on and the PC is not plugged in. Update anyway?":
        "حالت صرفه‌جویی باتری روشن است و رایانه به برق وصل نیست. با این حال به‌روزرسانی شود؟",
    "If the power runs out in the middle of an installation, the app can be left broken. Plugging in is safer.":
        "اگر وسط نصب شارژ تمام شود، ممکن است برنامه خراب بماند. وصل کردن شارژر امن‌تر است.",
    "Update on battery": "روی باتری به‌روزرسانی کن",
    "Not updated: the battery is at {percent}% and the PC is not plugged in.":
        "به‌روزرسانی نشد: شارژ باتری {percent}٪ است و رایانه به برق وصل نیست.",
    "Not updated: battery saver is on and the PC is not plugged in.":
        "به‌روزرسانی نشد: حالت صرفه‌جویی باتری روشن است و رایانه به برق وصل نیست.",
}

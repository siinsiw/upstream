"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    "Cancelled.": "لغو شد.",
    "Upstream": "Upstream",
    "{error}": "{error}",
    "Yes": "بله",
    "No": "خیر",
    "Install anyway": "با این حال نصب کن",
    "Don't install": "نصب نکن",
    "Not installed: you declined a file that wasn't signed by the app's usual publisher.":
        "نصب نشد: فایلی را رد کردی که ناشر همیشگی برنامه آن را امضا نکرده بود.",
    "{name} has an installer with no silent mode, so it can't be updated unattended. Update it yourself from the list.":
        "نصب‌کنندهٔ برنامهٔ {name} حالت بی‌صدا ندارد و بدون حضور تو به‌روز نمی‌شود. خودت از فهرست به‌روزش کن.",
    "winget was still preparing the download after {minutes} minutes, so Upstream stopped it. Nothing was installed.":
        "پس از {minutes} دقیقه هنوز winget در حال آماده‌سازی دانلود بود و Upstream آن را متوقف کرد. چیزی نصب نشد.",
}

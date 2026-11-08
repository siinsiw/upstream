"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    # tabs and header
    "Discover": "کشف برنامه",
    "History": "تاریخچه",
    "Export list…": "خروجی فهرست…",
    "Update all ({n}) · {size}": "به‌روزرسانی همه ({n}) · {size}",
    "Update all ({n}) · at least {size}": "به‌روزرسانی همه ({n}) · دست‌کم {size}",
    "Download size: {size}": "حجم دانلود: {size}",
    "Download size: at least {size}": "حجم دانلود: دست‌کم {size}",
    "Download size, largest first": "حجم دانلود، بزرگ‌ترین اول",
    "Last checked {time}, refreshing…": "آخرین بررسی {time}، در حال تازه‌سازی…",
    "Running as administrator": "اجرا با دسترسی مدیر",
    "Wait until the current work has finished.": "صبر کن تا کار جاری تمام شود.",
    "An uninstall or cleanup is still running. Closing Upstream does not stop an uninstaller, and Upstream will no "
    "longer show how it went. A cleanup in progress would stop where it is.":
        "حذف برنامه یا پاک‌سازی هنوز در جریان است. بستن Upstream حذف‌کنندهٔ برنامه را متوقف نمی‌کند، ولی Upstream "
        "دیگر نتیجه‌اش را نشان نمی‌دهد. پاک‌سازیِ در حال انجام هم همان‌جا می‌ایستد.",
    "Wait until the current check or uninstall has finished, then try again.":
        "صبر کن تا بررسی یا حذف جاری تمام شود، بعد دوباره امتحان کن.",
    "Wait until the check finishes, then try again.": "صبر کن تا بررسی تمام شود، بعد دوباره امتحان کن.",
    # pin
    "Pin version — {name}": "سنجاق کردن نسخه — {name}",
    "Choose how Upstream treats newer versions of {name}.": "مشخص کن Upstream با نسخه‌های جدیدتر {name} چه کند.",
    "Hold at {version}": "نگه‌داشتن روی نسخهٔ {version}",
    "the current version": "فعلی",
    "No update is offered for this app.": "برای این برنامه هیچ به‌روزرسانی پیشنهاد نمی‌شود.",
    "Stay on version {line}": "ماندن روی نسخهٔ {line}",
    "Stay on this major version": "ماندن روی همین نسخهٔ اصلی",
    "Updates within that major version are offered; a newer major version is not.":
        "به‌روزرسانی‌های درون همین نسخهٔ اصلی پیشنهاد می‌شوند؛ نسخهٔ اصلیِ جدید پیشنهاد نمی‌شود.",
    "Needs a numeric version, such as 3.12.4.": "به شمارهٔ نسخه‌ای مثل 3.12.4 نیاز دارد.",
    "Don't pin": "سنجاق نکردن",
    "Every update is offered.": "هر به‌روزرسانی پیشنهاد می‌شود.",
    # rollback
    "Looking up older versions…": "در حال یافتن نسخه‌های قدیمی‌تر…",
    "Roll back — {name}": "بازگشت به نسخهٔ قبلی — {name}",
    "Installed: {version}. Choose an older version to install over it.":
        "نسخهٔ نصب‌شده: {version}. یک نسخهٔ قدیمی‌تر را برای نصب روی آن انتخاب کن.",
    "Some apps can't be downgraded, and an older version may not understand data that a newer one saved. "
    "Close the app first.":
        "بعضی برنامه‌ها را نمی‌توان به نسخهٔ قدیمی‌تر برگرداند، و نسخهٔ قدیمی‌تر ممکن است نتواند داده‌هایی را "
        "بخواند که نسخهٔ جدیدتر ذخیره کرده. اول برنامه را ببند.",
    "Continue": "ادامه",
    "No older version of {name} is available through winget.": "نسخهٔ قدیمی‌تری از {name} در winget پیدا نشد.",
    "Roll back {name} to {version}?": "{name} به نسخهٔ {version} برگردانده شود؟",
    "{name} {installed} will be replaced by the older version {version}. Upstream asks winget to do it; "
    "if the app can't be downgraded, nothing changes.":
        "{name} {installed} با نسخهٔ قدیمی‌تر {version} جایگزین می‌شود. Upstream این کار را از winget می‌خواهد؛ "
        "اگر برنامه قابل برگرداندن به نسخهٔ قدیمی‌تر نباشد، چیزی تغییر نمی‌کند.",
    "Roll back": "بازگشت به نسخهٔ قبلی",
    "Also hold this app at {version} so it is not updated again":
        "همچنین برنامه را روی نسخهٔ {version} نگه‌دار تا دوباره به‌روز نشود",
    "{name} could not be rolled back.": "{name} به نسخهٔ قبلی برگردانده نشد.",
    "winget doesn't know this app, so it can't be rolled back.":
        "این برنامه در winget شناخته نیست، پس نمی‌توان آن را به نسخهٔ قبلی برگرداند.",
}

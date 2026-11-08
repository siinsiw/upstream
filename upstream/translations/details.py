"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    # dialog
    "Details — {name}": "جزئیات — {name}",
    "General": "اطلاعات کلی",
    "Architecture": "معماری",
    "64-bit": "۶۴ بیتی",
    "32-bit": "۳۲ بیتی",
    "Installed for": "نصب برای",
    "All users": "همهٔ کاربران",
    "Current user only": "فقط کاربر فعلی",
    "Installer": "نصب‌کننده",
    "Installed on": "تاریخ نصب",
    "Size on disk": "حجم روی دیسک",
    "Calculating…": "در حال محاسبه…",
    "At least {size}": "دست‌کم {size}",
    "Unknown": "نامشخص",
    "Signature": "امضای دیجیتال",
    "Signed by {signer}": "امضا شده توسط {signer}",
    "Not signed": "امضا نشده",
    "The signature is broken: the file was changed after it was signed":
        "امضا خراب است: فایل بعد از امضا شدن تغییر کرده",
    "Signed by {signer}, but not trusted by Windows": "امضا شده توسط {signer}، ولی ویندوز به آن اعتماد ندارد",
    "Windows doesn't trust the signature": "ویندوز به این امضا اعتماد ندارد",
    "No program file to check": "فایل برنامه‌ای برای بررسی پیدا نشد",
    "Couldn't check the signature": "بررسی امضا ممکن نشد",
    "Location": "محل نصب",
    "No install folder is recorded for this app.": "پوشهٔ نصبی برای این برنامه ثبت نشده است.",
    "Installed version": "نسخهٔ نصب‌شده",
    "Latest version": "آخرین نسخه",
    "Source": "منبع",
    "{label} — {origin}": "{label} — {origin}",
    "Links": "لینک‌ها",
    "Uninstall command": "دستور حذف",
    "No uninstall command is registered with Windows.": "دستور حذفی برای این برنامه در ویندوز ثبت نشده است.",
    "Copy": "کپی",
    "Copy details": "کپی جزئیات",
    "Copied to the clipboard.": "در کلیپ‌بورد کپی شد.",
    # repair and modify
    "Repair": "تعمیر",
    "Modify": "تغییر",
    "Repair {name}": "تعمیر {name}",
    "Windows Installer will reinstall all the files and registry entries of {name}. Settings kept outside the app's folder are not touched. Windows may ask for permission.":
        "برنامهٔ Windows Installer همهٔ فایل‌ها، کلیدها و مقدارهای رجیستری {name} را دوباره نصب می‌کند. تنظیماتی "
        "که بیرون از پوشهٔ برنامه نگهداری می‌شوند دست نمی‌خورند. ممکن است ویندوز اجازه بخواهد.",
    "Upstream stops waiting for Windows Installer. The repair itself is not stopped.":
        "انتظار برای Windows Installer متوقف می‌شود، ولی خودِ تعمیر متوقف نمی‌شود.",
    "Updates are running right now. Wait until they finish, then try again.":
        "الان به‌روزرسانی در جریان است. صبر کن تمام شود، بعد دوباره امتحان کن.",
    "Repairing {name}…": "در حال تعمیر {name}…",
    "Opening the Modify window of {name}…": "در حال باز کردن پنجرهٔ تغییر {name}…",
    "{name} was repaired.": "{name} تعمیر شد.",
    "{name} was repaired. Restart Windows to finish.": "{name} تعمیر شد. برای تمام شدن کار، ویندوز را دوباره راه‌اندازی کن.",
    "The repair was cancelled.": "تعمیر لغو شد.",
    "Couldn't open the Modify window (Windows error {code}).":
        "باز کردن پنجرهٔ تغییر ممکن نشد (خطای ویندوز {code}).",
    "Opened the Modify window of {name}. Follow the steps there.": "پنجرهٔ تغییر {name} باز شد. مراحل را همان‌جا ادامه بده.",
    "{name} doesn't offer a Modify option.": "{name} گزینهٔ تغییر ندارد.",
    "Upstream won't run the Modify command of {name}: its program is missing or not safe to start.":
        "برنامهٔ Upstream دستور تغییر {name} را اجرا نمی‌کند: برنامهٔ آن پیدا نشد یا اجرایش ایمن نیست.",
    "Only apps installed with Windows Installer (MSI) can be repaired from here.":
        "فقط برنامه‌هایی که با Windows Installer (MSI) نصب شده‌اند از اینجا تعمیر می‌شوند.",
    "The installer of {name} says it can't be repaired.": "نصب‌کنندهٔ {name} اعلام کرده که قابل تعمیر نیست.",
    "Windows Installer no longer knows {name}, so it can't be repaired.":
        "برنامهٔ Windows Installer دیگر {name} را نمی‌شناسد، برای همین تعمیر نمی‌شود.",
}

"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    # generic results
    "The installation was cancelled": "نصب لغو شد",
    "The installer needs administrator permission": "نصب‌کننده به اجازهٔ مدیر سیستم نیاز دارد",
    "There isn't enough free disk space to install": "فضای خالی دیسک برای نصب کافی نیست",
    "The installer was denied access. It may need administrator permission (code {code})":
        "به نصب‌کننده دسترسی داده نشد؛ شاید به اجازهٔ مدیر سیستم نیاز دارد (کد {code})",
    "Another version of this app is already installed, so this installer can't continue":
        "نسخهٔ دیگری از این برنامه از قبل نصب است، برای همین این نصب‌کننده ادامه نمی‌دهد",
    "Another installation is running, waiting…": "نصب دیگری در جریان است، در انتظار…",
    # Windows Installer
    "The Windows Installer service couldn't be reached (code {code})":
        "سرویس Windows Installer در دسترس نبود (کد {code})",
    "Windows Installer hit a fatal error during installation (code {code})":
        "برنامهٔ Windows Installer هنگام نصب دچار خطای جدی شد (کد {code})",
    "The installation was suspended before it finished (code {code})":
        "نصب پیش از تمام شدن متوقف شد (کد {code})",
    "The installer package couldn't be opened (code {code})": "بستهٔ نصب باز نشد (کد {code})",
    "The installer package is invalid or damaged (code {code})": "بستهٔ نصب نامعتبر یا خراب است (کد {code})",
    "Windows Installer couldn't show its window (code {code})":
        "برنامهٔ Windows Installer نتوانست پنجرهٔ خودش را نشان بدهد (کد {code})",
    "Windows Installer couldn't write its log file (code {code})":
        "برنامهٔ Windows Installer نتوانست فایل گزارش خودش را بنویسد (کد {code})",
    "The installer's language isn't supported by this PC (code {code})":
        "زبان نصب‌کننده روی این رایانه پشتیبانی نمی‌شود (کد {code})",
    "Windows Installer couldn't apply a customization (code {code})":
        "برنامهٔ Windows Installer نتوانست یکی از سفارشی‌سازی‌ها را اعمال کند (کد {code})",
    "System policy forbids this installation (code {code})":
        "سیاست‌های سیستم اجازهٔ این نصب را نمی‌دهند (کد {code})",
    "This installer isn't supported on this PC (code {code})":
        "این نصب‌کننده روی این رایانه پشتیبانی نمی‌شود (کد {code})",
    "Windows Installer was given an invalid command line (code {code})":
        "دستور اجرای Windows Installer نامعتبر بود (کد {code})",
    "Windows doesn't allow installing from a remote session (code {code})":
        "ویندوز نصب از داخل نشست راه دور را مجاز نمی‌داند (کد {code})",
    # Inno Setup and NSIS
    "Setup couldn't start (code {code})": "اجرای نصب‌کننده ممکن نشد (کد {code})",
    "Setup hit a fatal error while preparing to install (code {code})":
        "نصب‌کننده هنگام آماده‌سازی نصب دچار خطای جدی شد (کد {code})",
    "Setup hit a fatal error while installing (code {code})":
        "نصب‌کننده هنگام نصب دچار خطای جدی شد (کد {code})",
    "Setup decided it can't install on this PC (code {code})":
        "نصب‌کننده تشخیص داد که نمی‌تواند روی این رایانه نصب کند (کد {code})",
    "Setup can't continue until Windows is restarted":
        "تا ویندوز دوباره راه‌اندازی نشود، نصب ادامه پیدا نمی‌کند",
    "The installer stopped itself before it finished (code {code})":
        "نصب‌کننده پیش از پایان کار، خودش را متوقف کرد (کد {code})",
    # winget
    "winget couldn't download the installer": "ابزار winget نتوانست نصب‌کننده را دانلود کند",
    "winget has no installer that fits this PC": "ابزار winget نصب‌کننده‌ای مناسب این رایانه ندارد",
    "winget couldn't find this package": "ابزار winget این بسته را پیدا نکرد",
    "winget has no update it can apply to this app":
        "ابزار winget برای این برنامه به‌روزرسانی قابل نصبی ندارد",
    "winget says another version of this app is already installed":
        "طبق گفتهٔ winget نسخهٔ دیگری از این برنامه از قبل نصب است",
    "winget says a newer version of this app is already installed":
        "طبق گفتهٔ winget نسخهٔ جدیدتری از این برنامه از قبل نصب است",
    "The download failed winget's integrity check; nothing was installed":
        "فایل دانلودشده در بررسی صحت winget قبول نشد؛ چیزی نصب نشد",
    "The app is still running. Close it and try again": "برنامه هنوز باز است. آن را ببند و دوباره امتحان کن",
    "A package this app depends on is missing": "یکی از بسته‌هایی که این برنامه به آن نیاز دارد نصب نیست",
    "The installer needs an internet connection": "نصب‌کننده به اتصال اینترنت نیاز دارد",
    "Windows must restart before winget can install this. Restart and try again":
        "پیش از نصب با winget باید ویندوز را دوباره راه‌اندازی کنی؛ بعد دوباره امتحان کن",
    "Organization policy blocks this installation": "سیاست‌های سازمان جلوی این نصب را گرفته‌اند",
    "This package isn't supported on this PC": "این بسته روی این رایانه پشتیبانی نمی‌شود",
    "The installer can't upgrade an installed copy": "این نصب‌کننده نمی‌تواند نسخهٔ نصب‌شده را ارتقا بدهد",
}

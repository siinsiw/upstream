"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    # discover.py
    "winget isn't installed on this PC.": "ابزار winget روی این رایانه نصب نیست.",
    "winget couldn't be started: {error}": "اجرای winget ممکن نشد: {error}",
    "winget took too long to answer.": "پاسخ winget بیش از حد طول کشید.",
    "winget couldn't search: {error}": "جست‌وجو با winget ناموفق بود: {error}",
    # the Discover tab
    "Search winget for an app to install": "جست‌وجوی برنامه برای نصب با winget",
    "Find an app to install": "برنامه‌ای برای نصب پیدا کن",
    "Type a name above. Results come from the winget catalogue, and each app is installed from its publisher's own installer, which winget checks first.":
        "بالا نام برنامه را بنویس. نتیجه‌ها از فهرست winget می‌آیند و هر برنامه با نصب‌کنندهٔ خودِ ناشرش نصب "
        "می‌شود که winget پیش از اجرا درستی‌اش را بررسی می‌کند.",
    "Searching…": "در حال جست‌وجو…",
    "Asking winget. This usually takes a second or two.": "از winget پرسیده می‌شود. معمولاً یکی دو ثانیه طول می‌کشد.",
    "{n} result": "{n} نتیجه",
    "{n} results": "{n} نتیجه",
    "Showing the first {n} results. Add more of the name to narrow them down.":
        "{n} نتیجهٔ اول نمایش داده شد. بخش بیشتری از نام را بنویس تا نتیجه‌ها محدودتر شوند.",
    "No match in winget": "در winget چیزی پیدا نشد",
    "Nothing in the winget catalogue matches “{query}”. Try another spelling, or only part of the name.":
        "در فهرست winget چیزی با «{query}» جور درنیامد. املای دیگری را امتحان کن، یا فقط بخشی از نام را بنویس.",
    "The search didn't work": "جست‌وجو انجام نشد",
    "Something went wrong: {error}": "مشکلی پیش آمد: {error}",
    "Try again": "تلاش دوباره",
    "winget isn't available": "ابزار winget در دسترس نیست",
    "Finding and installing new apps needs winget, which comes with App Installer from the Microsoft Store. Install or update it, then try again.":
        "برای پیدا کردن و نصب برنامهٔ جدید، ابزار winget لازم است که همراه «App Installer» از فروشگاه مایکروسافت "
        "می‌آید. آن را نصب یا به‌روز کن و دوباره امتحان کن.",
    # a result row
    "Install": "نصب",
    "Install {name}": "نصب {name}",
    "Installed": "نصب شده",
    "Copy winget ID": "کپی شناسهٔ winget",
    "Copy install command": "کپی دستور نصب",
}

"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    "{name} was installed with {installed}, but this update is packaged with {new}. "
    "Running it would add a second copy next to the old one.":
        "برنامهٔ {name} با {installed} نصب شده، ولی این به‌روزرسانی با {new} بسته‌بندی شده است. "
        "اجرای آن یک نسخهٔ دوم کنار نسخهٔ قدیمی اضافه می‌کند.",
    "This installer is not an upgrade of the {name} on this PC. Running it would add a "
    "second copy next to the old one.":
        "این نصب‌کننده ارتقای {name} روی این رایانه نیست. اجرای آن یک نسخهٔ دوم کنار نسخهٔ قدیمی اضافه می‌کند.",
    "{name} was installed as an MSI package, but this update is packaged with {new}. "
    "It may not replace the installed copy.":
        "برنامهٔ {name} به‌صورت بستهٔ MSI نصب شده، ولی این به‌روزرسانی با {new} بسته‌بندی شده است. "
        "شاید جایگزین نسخهٔ نصب‌شده نشود.",
    "The installed copy of {name} is for all users, but this installer sets the app up for the "
    "current user only. The update may end up as a second copy.":
        "نسخهٔ نصب‌شدهٔ {name} برای همهٔ کاربران است، ولی این نصب‌کننده فقط برای کاربر فعلی نصب می‌کند. "
        "شاید نتیجه یک نسخهٔ دوم باشد.",
    "The installed copy of {name} is for the current user only, but this installer "
    "sets up the app for all users. The update may end up as a second copy.":
        "نسخهٔ نصب‌شدهٔ {name} فقط برای کاربر فعلی است، ولی این نصب‌کننده برنامه را برای همهٔ کاربران نصب می‌کند. "
        "شاید نتیجه یک نسخهٔ دوم باشد.",
}

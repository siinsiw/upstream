"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    # why an item was left alone, and what happened (cleanup.py)
    "It changed after the list was made, so it was left in place":
        "بعد از ساخته شدن فهرست تغییر کرده، برای همین دست‌نخورده ماند",
    "It contains a link to somewhere else, which Upstream never follows, so it was left in place":
        "پیوندی به جای دیگر دارد که Upstream هرگز دنبالش نمی‌کند، برای همین دست‌نخورده ماند",
    "A program is using it. Close the program and try again.":
        "برنامه‌ای در حال استفاده از آن است. برنامه را ببند و دوباره امتحان کن.",
    "It needs administrator rights. Upstream never gives itself administrator rights: start it as administrator "
    "to remove this.":
        "برای این کار دسترسی مدیر لازم است. Upstream هیچ‌وقت خودش را مدیر نمی‌کند؛ برای حذف این مورد آن را با "
        "دسترسی مدیر اجرا کن.",
    "Windows denied access to it. A program may be using it.":
        "ویندوز اجازهٔ دسترسی به آن را نداد. شاید برنامه‌ای در حال استفاده از آن باشد.",
    "There is not enough free disk space for the backup, so it was left in place":
        "برای پشتیبان‌گیری فضای خالی کافی روی دیسک نیست، برای همین دست‌نخورده ماند",
    "The backup failed, so nothing was removed: {error}": "پشتیبان‌گیری ناموفق بود، برای همین چیزی حذف نشد: {error}",
    "Only part of it could be removed. The rest is in use and stays where it is.":
        "فقط بخشی از آن حذف شد. بقیه در حال استفاده است و سر جایش می‌ماند.",
    "Part of this key was removed before it failed: {error}. The backup holds all of it, so Undo puts it back.":
        "بخشی از این کلید حذف شد و بعد کار ناموفق ماند: {error}. پشتیبان همهٔ آن را دارد و «برگرداندن پاک‌سازی» آن را سر جایش می‌گذارد.",
    "Upstream couldn't check which installed apps use this, so it was left in place":
        "بررسی این‌که کدام برنامه‌های نصب‌شده از این استفاده می‌کنند ممکن نشد، برای همین دست‌نخورده ماند",
    "Couldn't remove it: {error}": "حذف نشد: {error}",
    "Windows refused to remove the service: {error}": "ویندوز حذف سرویس را نپذیرفت: {error}",
    "Upstream doesn't know how to remove this kind of item": "این نوع مورد را Upstream نمی‌داند چطور حذف کند",
    "It already exists at the original place, so it was not overwritten":
        "این مورد همین حالا در جای اصلی‌اش هست، برای همین رونویسی نشد",
    "Files that already exist at the original place were left as they are":
        "فایل‌هایی که همین حالا در جای اصلی هستند دست‌نخورده ماندند",
    "The backup of this item is missing": "پشتیبان این مورد پیدا نشد",
    "This backup can't be read": "این پشتیبان خوانده نمی‌شود",
    "A service can't be restored from a backup. Reinstall the app to get it back.":
        "سرویس از روی پشتیبان بازگردانده نمی‌شود. برای داشتنش برنامه را دوباره نصب کن.",
    "Couldn't restore it: {error}": "بازگردانده نشد: {error}",
    "Creating a Windows restore point…": "در حال ساخت نقطهٔ بازیابی ویندوز…",
    "Removing {item}": "در حال حذف {item}",
    "Restoring {item}": "در حال بازگردانی {item}",
    # review of leftovers
    "Nothing is removed until you press Remove selected. Everything removed is backed up first, and you can undo it.":
        "تا «حذف موارد انتخاب‌شده» را نزنی چیزی حذف نمی‌شود. هرچه حذف شود اول پشتیبان‌گیری می‌شود و می‌شود آن را برگرداند.",
    "The review lists what was left behind. Nothing is removed until you choose it, and everything removed is backed up first.":
        "این بررسی باقی‌مانده‌ها را فهرست می‌کند. تا خودت انتخاب نکنی چیزی حذف نمی‌شود و هرچه حذف شود اول پشتیبان‌گیری می‌شود.",
    "Remove selected…": "حذف موارد انتخاب‌شده…",
    "Backups…": "پشتیبان‌ها…",
    "Space used": "فضای اشغال‌شده",
    "From app": "مربوط به برنامه",
    "Reverse order": "ترتیب برعکس",
    "From {app}": "مربوط به {app}",
    "{n} ticked": "{n} مورد تیک‌خورده",
    "{n} ticked · {size}": "{n} مورد تیک‌خورده · {size}",
    "Nothing ticked": "چیزی تیک نخورده",
    # kinds, counted
    "{n} folder": "{n} پوشه",
    "{n} folders": "{n} پوشه",
    "{n} file": "{n} فایل",
    "{n} files": "{n} فایل",
    "{n} shortcut": "{n} میان‌بر",
    "{n} shortcuts": "{n} میان‌بر",
    "{n} registry key": "{n} کلید رجیستری",
    "{n} registry keys": "{n} کلید رجیستری",
    "{n} startup entry": "{n} مورد راه‌اندازی",
    "{n} startup entries": "{n} مورد راه‌اندازی",
    "{n} service": "{n} سرویس",
    "{n} services": "{n} سرویس",
    # confirmation, progress, summary
    "Remove {n} item?": "{n} مورد حذف شود؟",
    "Remove {n} items?": "{n} مورد حذف شود؟",
    "About {size} of files and folders": "حدود {size} فایل و پوشه",
    "Each item is backed up first and kept for {days} days. You can undo this right after, or later from Backups.":
        "هر مورد اول پشتیبان‌گیری می‌شود و {days} روز نگه داشته می‌شود. می‌توانی همین حالا یا بعداً از «پشتیبان‌ها» کار را برگردانی.",
    "Each item is backed up first. You can undo this right after, or later from Backups.":
        "هر مورد اول پشتیبان‌گیری می‌شود. می‌توانی همین حالا یا بعداً از «پشتیبان‌ها» کار را برگردانی.",
    "{n} item may need administrator rights. Upstream never gives itself administrator rights, so if it can't "
    "remove it, it is left alone. Start Upstream as administrator to remove it.":
        "{n} مورد شاید دسترسی مدیر بخواهد. Upstream هیچ‌وقت خودش را مدیر نمی‌کند و اگر نتواند آن را حذف کند، "
        "دست‌نخورده می‌ماند. برای حذفش Upstream را با دسترسی مدیر اجرا کن.",
    "{n} items may need administrator rights. Upstream never gives itself administrator rights, so if it can't "
    "remove them, they are left alone. Start Upstream as administrator to remove them.":
        "{n} مورد شاید دسترسی مدیر بخواهند. Upstream هیچ‌وقت خودش را مدیر نمی‌کند و اگر نتواند آن‌ها را حذف کند، "
        "دست‌نخورده می‌مانند. برای حذفشان Upstream را با دسترسی مدیر اجرا کن.",
    "Undo can't bring a removed service back. To get one back, reinstall the app.":
        "با «برگرداندن پاک‌سازی» سرویس حذف‌شده برنمی‌گردد. برای داشتن دوبارهٔ آن، برنامه را دوباره نصب کن.",
    "Remove and back up": "حذف با پشتیبان‌گیری",
    "Back to the list": "بازگشت به فهرست",
    "Removing leftovers": "در حال حذف باقی‌مانده‌ها",
    "Each item is backed up before it is removed.": "هر مورد پیش از حذف پشتیبان‌گیری می‌شود.",
    "Stop after this item": "توقف بعد از این مورد",
    "Stopping after this item…": "در حال توقف بعد از این مورد…",
    "Leftovers removed": "باقی‌مانده‌ها حذف شد",
    "Cleanup stopped": "پاک‌سازی متوقف شد",
    "Nothing was removed": "چیزی حذف نشد",
    "Removed {n} item": "{n} مورد حذف شد",
    "Removed {n} items": "{n} مورد حذف شد",
    "{size} moved into the backup": "{size} به پشتیبان منتقل شد",
    "{n} item was already gone": "{n} مورد از قبل وجود نداشت",
    "{n} items were already gone": "{n} مورد از قبل وجود نداشت",
    "A Windows restore point was created.": "یک نقطهٔ بازیابی ویندوز ساخته شد.",
    "Windows did not create a restore point. That needs administrator rights and System Protection turned on, and Windows allows only one a day. Your backup still works.":
        "ویندوز نقطهٔ بازیابی نساخت. برای آن دسترسی مدیر و روشن بودن محافظت از سیستم لازم است و ویندوز در هر روز "
        "فقط یکی می‌سازد. نسخهٔ پشتیبان Upstream همچنان قابل بازگردانی است.",
    "Restart Windows to finish removing a service.": "برای کامل شدن حذف یک سرویس، ویندوز را دوباره راه‌اندازی کن.",
    "{n} item was not removed": "{n} مورد حذف نشد",
    "{n} items were not removed": "{n} مورد حذف نشد",
    "and {n} more": "و {n} مورد دیگر",
    "Everything removed was backed up first. Undo cleanup puts it all back.":
        "هرچه حذف شد اول پشتیبان‌گیری شد. «برگرداندن پاک‌سازی» همه را سر جایش برمی‌گرداند.",
    "Undo cleanup": "برگرداندن پاک‌سازی",
    "Restoring…": "در حال بازگردانی…",
    "Restored {n} item": "{n} مورد بازگردانده شد",
    "Restored {n} items": "{n} مورد بازگردانده شد",
    "{n} item was left as it is": "{n} مورد دست‌نخورده ماند",
    "{n} items were left as they are": "{n} مورد دست‌نخورده ماند",
    "Nothing was restored": "چیزی بازگردانده نشد",
    "Left as it is": "دست‌نخورده ماندند",
    "Nothing that exists now was overwritten.": "چیزی که همین حالا وجود دارد رونویسی نشد.",
    # backups dialog
    "Backups": "پشتیبان‌ها",
    "Leftovers that Upstream removed are kept here first, so you can undo a cleanup. Backups older than {days} days are deleted automatically.":
        "باقی‌مانده‌هایی که Upstream حذف می‌کند اول اینجا نگه داشته می‌شوند تا بتوانی پاک‌سازی را برگردانی. "
        "پشتیبان‌های قدیمی‌تر از {days} روز خودکار پاک می‌شوند.",
    "Leftovers that Upstream removed are kept here first, so you can undo a cleanup.":
        "باقی‌مانده‌هایی که Upstream حذف می‌کند اول اینجا نگه داشته می‌شوند تا بتوانی پاک‌سازی را برگردانی.",
    "Backed up on": "تاریخ پشتیبان‌گیری",
    "Items backed up": "تعداد موارد",
    "No backups yet.": "هنوز چیزی پشتیبان‌گیری نشده است.",
    "Restore backup": "بازگردانی پشتیبان",
    "Delete backup": "پاک کردن پشتیبان",
    "Delete the backup of {app} from {date} for good? Then it can no longer be restored.":
        "پشتیبان {app} مربوط به {date} برای همیشه پاک شود؟ بعد از آن دیگر نمی‌شود بازگردانی‌اش کرد.",
    "Delete for good": "پاک کردن برای همیشه",
    "Keep backup": "نگه داشتن پشتیبان",
    "Backup deleted.": "پشتیبان پاک شد.",
    "Couldn't delete this backup. A cleanup or an undo may be running, or it was made while Upstream ran as administrator.":
        "این پشتیبان پاک نشد. شاید پاک‌سازی یا برگرداندن پاک‌سازی در جریان باشد، یا پشتیبان زمانی ساخته شده که "
        "Upstream با دسترسی مدیر اجرا می‌شد.",
    # batch uninstall
    "Application": "برنامه",
    "Outcome": "نتیجه",
    "Heads-up": "هشدار",
    "Uninstall {n} app": "حذف {n} برنامه",
    "Uninstall {n} apps": "حذف {n} برنامه",
    "Uninstall {n} app?": "{n} برنامه حذف شود؟",
    "Uninstall {n} apps?": "{n} برنامه حذف شود؟",
    "Uninstalling {n} app": "در حال حذف {n} برنامه",
    "Uninstalling {n} apps": "در حال حذف {n} برنامه",
    "Uninstalling apps": "در حال حذف برنامه‌ها",
    "They are removed one after another. Apps that open their own window go first.":
        "برنامه‌ها یکی‌یکی حذف می‌شوند. برنامه‌هایی که پنجرهٔ خودشان را باز می‌کنند زودتر از بقیه حذف می‌شوند.",
    "{n} app can't be removed here and is skipped.": "{n} برنامه از اینجا حذف نمی‌شود و رد می‌شود.",
    "{n} apps can't be removed here and are skipped.": "{n} برنامه از اینجا حذف نمی‌شوند و رد می‌شوند.",
    "Can't be removed here": "از اینجا حذف نمی‌شود",
    "By Windows Installer": "با Windows Installer",
    "By winget": "با winget",
    "By its own uninstaller, quietly": "با حذف‌کنندهٔ خودش، بی‌صدا",
    "In its own uninstall window": "در پنجرهٔ حذف خودش",
    "One review covers every app that was removed. Nothing is removed from it until you choose it, and everything removed is backed up first.":
        "یک بررسی برای همهٔ برنامه‌های حذف‌شده انجام می‌شود. تا خودت انتخاب نکنی چیزی از آن حذف نمی‌شود و هرچه حذف شود اول پشتیبان‌گیری می‌شود.",
    "Finish the steps in an uninstaller's own window when one opens. Windows may ask for permission.":
        "اگر حذف‌کننده‌ای پنجرهٔ خودش را باز کرد، مراحل را همان‌جا ادامه بده. ممکن است ویندوز اجازه بخواهد.",
    "Stop after this one": "توقف بعد از این یکی",
    "Stopping after the one in progress…": "در حال توقف بعد از مورد جاری…",
    "Skipped because the list was stopped": "رد شد، چون فهرست متوقف شد",
    "Removed {done} of {total} apps": "{done} برنامه از {total} برنامه حذف شد",
    "Waiting to start": "در انتظار شروع",
    "Being removed…": "در حال حذف…",
    "Left alone": "دست‌نخورده",
    "Uninstalls are still running. Closing this window stops the rest of the list, but the one in progress keeps going and Upstream will no longer show how it went.":
        "حذف برنامه‌ها هنوز در جریان است. بستن این پنجره بقیهٔ فهرست را متوقف می‌کند، ولی حذفی که در جریان است ادامه پیدا می‌کند و Upstream دیگر نتیجه‌اش را نشان نمی‌دهد.",
}

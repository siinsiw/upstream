"""UI language. English text is the key; other languages map it to a translation.

    tr("Update all")                       -> "Update all" / "به‌روزرسانی همه"
    tr("Installed {version}", version=v)   -> placeholders are filled after translation
    trn("{n} update ready", "{n} updates ready", 3)

Counts go through num() so Persian shows Persian digits; version strings never do.
"""
from __future__ import annotations

LANGUAGES = {"en": "English", "fa": "فارسی"}
RTL_LANGUAGES = {"fa"}
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FSI, _PDI = "⁨", "⁩"   # first-strong isolate: keeps "3.9.0+38" or English text intact in RTL
SOURCE_LABELS = {"github": "GitHub", "winget": "Official site · winget", "site": "Official site"}
# Stored message arguments that are themselves translatable (a source label, a known error message).
_TRANSLATED_ARGS = {"source", "error"}

FA = {
    # window
    "Starting…": "در حال آماده‌سازی…",
    "Check again": "بررسی دوباره",
    "Update all": "به‌روزرسانی همه",
    "Update all ({n})": "به‌روزرسانی همه ({n})",
    "Settings": "تنظیمات",
    "Updates": "به‌روزرسانی‌ها",
    "All apps": "همهٔ برنامه‌ها",
    "Ignored": "نادیده‌گرفته‌ها",
    "Search apps": "جست‌وجو در برنامه‌ها",
    "Show log": "نمایش گزارش",
    "Hide log": "پنهان کردن گزارش",
    "Checking…": "در حال بررسی…",
    "{text} {i} of {n}": "{text} {i} از {n}",
    "Check failed: {error}": "بررسی ناموفق بود: {error}",
    "{n} update ready": "{n} به‌روزرسانی آماده",
    "{n} updates ready": "{n} به‌روزرسانی آماده",
    "Everything is up to date": "همه‌چیز به‌روز است",
    "{n} update in progress": "{n} به‌روزرسانی در جریان",
    "{n} updates in progress": "{n} به‌روزرسانی در جریان",
    " · last checked {time}": " · آخرین بررسی {time}",
    "{n} installed app": "{n} برنامهٔ نصب‌شده",
    "{n} installed apps": "{n} برنامهٔ نصب‌شده",
    "GitHub with token": "اتصال به GitHub با توکن",
    "GitHub without token (60 requests/hour)": "اتصال به GitHub بدون توکن (۶۰ درخواست در ساعت)",
    " · {n} requests left": " · {n} درخواست باقی‌مانده",
    "Checking your apps…": "در حال بررسی برنامه‌ها…",
    "The first check takes a little longer: each app's official source is found once and remembered.":
        "بار اول کمی طول می‌کشد: منبع رسمی هر برنامه یک بار پیدا و ذخیره می‌شود.",
    "Nothing matches": "چیزی پیدا نشد",
    "If an app's source wasn't found, set it from All apps → ⋯ → Update source.":
        "اگر منبع برنامه‌ای پیدا نشده، از «همهٔ برنامه‌ها» › ⋯ › «منبع به‌روزرسانی» تعیینش کن.",
    "Nothing here": "فهرست خالی است",
    "Apps you ignore are kept here.": "برنامه‌هایی که نادیده بگیری اینجا جمع می‌شوند.",
    "No apps found": "برنامه‌ای پیدا نشد",
    "No update pending anymore": "دیگر به‌روزرسانی‌ای در کار نیست",
    "Rechecking": "بررسی دوباره",
    "Updates are still running. The install that already started will finish, but the rest of the queue is cancelled.":
        "هنوز به‌روزرسانی در جریان است. نصبی که شروع شده ادامه پیدا می‌کند، ولی بقیهٔ صف لغو می‌شود.",
    "Close": "بستن",
    "Stay": "ماندن",
    # row
    "GitHub": "GitHub",
    "Official site · winget": "سایت رسمی · winget",
    "Official site": "سایت رسمی",
    "Unknown publisher": "ناشر نامشخص",
    "Update": "به‌روزرسانی",
    "Open website": "باز کردن سایت",
    "Source: {label} — {origin}": "منبع: {label} — {origin}",
    "Not checked": "بررسی نمی‌شود",
    "Skipped {version}": "نسخهٔ {version} رد شد",
    "Up to date": "به‌روز است",
    "No source found": "منبع پیدا نشد",
    "Queued": "در صف",
    "{pct}%": "{pct}٪",
    "Retry": "تلاش دوباره",
    "Release notes": "یادداشت‌های انتشار",
    "Official page": "صفحهٔ رسمی برنامه",
    "GitHub repository ({repo})": "مخزن GitHub ({repo})",
    "Update source…": "منبع به‌روزرسانی…",
    "Skip version {version}": "رد کردن نسخهٔ {version}",
    "Stop skipping this version": "نمایش دوبارهٔ این نسخه",
    "Stop ignoring": "برگرداندن به فهرست",
    "Ignore this app": "نادیده گرفتن این برنامه",
    "Open install folder": "باز کردن پوشهٔ نصب",
    "More actions": "کارهای بیشتر",
    # source dialog
    "Update source — {name}": "منبع به‌روزرسانی — {name}",
    "Checked in this order: GitHub, then the official site, then winget. Leave a field empty to keep automatic detection for it.":
        "ترتیب بررسی: اول GitHub، بعد سایت رسمی، بعد winget. هر فیلدی را خالی بگذاری، تشخیص خودکار برای آن انجام می‌شود.",
    "GitHub repository": "مخزن GitHub",
    "winget ID": "شناسهٔ winget",
    "Site download page": "صفحهٔ دانلود سایت",
    "Version pattern (regex)": "الگوی نسخه (regex)",
    "Download link": "لینک دانلود",
    "The download link can use {version}, {version_nodots} and {major}. Leave it empty and the Update button just opens the site.":
        "در لینک دانلود می‌توانی از {version}، {version_nodots} و {major} استفاده کنی. "
        "اگر خالی بماند، دکمهٔ به‌روزرسانی فقط صفحهٔ سایت را باز می‌کند.",
    "Save": "ذخیره",
    "Clear overrides": "حذف تنظیمات دستی",
    "Cancel": "انصراف",
    "The GitHub repository must look like owner/repo or a github.com link.":
        "مخزن GitHub باید به شکل owner/repo یا لینک github.com باشد.",
    "A site rule needs both the page address and the version pattern.":
        "برای سایت، هم آدرس صفحه و هم الگوی نسخه لازم است.",
    "The version pattern is not a valid regex: {error}": "الگوی نسخه معتبر نیست: {error}",
    # settings dialog
    "Language": "زبان",
    "Theme": "پوسته",
    "Sources": "منابع",
    "Use the GitHub CLI (gh) token when signed in": "اگر GitHub CLI (gh) وارد شده، از توکنش استفاده شود",
    "Offer pre-releases (beta, rc)": "نسخه‌های پیش‌انتشار (beta و rc) هم پیشنهاد شود",
    "Source changes apply on the next check.": "تغییر منابع از بررسی بعدی اعمال می‌شود.",
    "Done": "تمام",
    "System": "هماهنگ با ویندوز",
    "Paper": "کاغذی",
    "Graphite": "گرافیتی",
    "Midnight": "نیمه‌شب",
    "Arctic": "قطبی",
    "Dune": "شنی",
    "Moss": "خزه",
    "High contrast": "کنتراست بالا",
    # scanner
    "Reading installed apps…": "در حال خواندن برنامه‌های نصب‌شده…",
    "Getting the winget package list…": "در حال گرفتن فهرست winget…",
    "Finding each app's official source…": "در حال پیدا کردن منبع رسمی هر برنامه…",
    "Checking GitHub and official sites…": "در حال بررسی GitHub و سایت‌های رسمی…",
    "GitHub has {version}, but no Windows installer": "نسخهٔ {version} در GitHub هست ولی نصب‌کنندهٔ ویندوزی ندارد",
    "GitHub rate limit reached; using winget": "سقف درخواست‌های GitHub پر شده؛ از winget استفاده شد",
    "GitHub was unreachable": "دسترسی به GitHub ممکن نشد",
    "Site check failed: {error}": "بررسی سایت ناموفق بود: {error}",
    "Version pattern not found on the page": "الگوی نسخه در صفحه پیدا نشد",
    # installer / updater
    "The download's SHA-256 doesn't match the hash GitHub published; nothing was installed.":
        "هش فایل دانلودشده با هشی که GitHub اعلام کرده یکی نیست؛ چیزی نصب نشد.",
    "The downloaded file is not a Windows installer.": "فایل دانلودشده نصب‌کنندهٔ ویندوز نیست.",
    "Administrator permission (UAC) was declined.": "اجازهٔ مدیر سیستم (UAC) داده نشد.",
    "Couldn't start the installer (Windows error {code}).": "اجرای نصب‌کننده ممکن نشد (خطای ویندوز {code}).",
    "No source for an automatic download": "منبعی برای دانلود خودکار پیدا نشد",
    "Installer finished, but Windows still reports {version}": "نصب‌کننده تمام شد ولی ویندوز هنوز نسخهٔ {version} را نشان می‌دهد",
    "Installed {version}": "نسخهٔ {version} نصب شد",
    "{source} failed, trying the next source…": "مسیر {source} نشد، امتحان مسیر بعدی…",
    "Install failed": "نصب ناموفق بود",
    "Downloading": "در حال دانلود",
    "Checking signature": "بررسی امضای دیجیتال",
    "The file's digital signature is broken (tampered); nothing was installed.":
        "امضای دیجیتال فایل دست‌کاری شده است؛ چیزی نصب نشد.",
    "Installing": "در حال نصب",
    "Setup window opened": "پنجرهٔ نصب باز شد",
    "Installer exited with code {code}": "نصب‌کننده با کد {code} بسته شد",
    "Preparing (winget)": "آماده‌سازی (winget)",
    "Downloading (winget)": "دانلود (winget)",
    "winget exited with {code}": "کار winget با کد {code} تمام شد",
    "Couldn't install the MSIX package: {error}": "بستهٔ MSIX نصب نشد: {error}",
    "Couldn't install the MSIX package.": "بستهٔ MSIX نصب نشد.",
    # uninstall dialog
    "Uninstall…": "حذف کامل…",
    "Uninstall — {name}": "حذف کامل — {name}",
    "Uninstall": "حذف",
    "Version {version}": "نسخهٔ {version}",
    "How it will be removed": "روش حذف",
    "Windows Installer removes it and shows a small progress window.":
        "برنامهٔ Windows Installer آن را حذف می‌کند و یک پنجرهٔ کوچک پیشرفت نشان می‌دهد.",
    "Windows Installer removes it and may show its own windows.":
        "برنامهٔ Windows Installer آن را حذف می‌کند و ممکن است پنجره‌های خودش را نشان دهد.",
    "The app's own uninstaller removes it quietly.": "حذف‌کنندهٔ خود برنامه آن را بی‌صدا حذف می‌کند.",
    "Opens {name}'s own uninstall window. Follow the steps there.":
        "پنجرهٔ حذف {name} باز می‌شود. مراحل را همان‌جا ادامه بده.",
    "winget removes it quietly.": "ابزار winget آن را بی‌صدا حذف می‌کند.",
    "Windows may ask for permission.": "ممکن است ویندوز اجازه بخواهد.",
    "Run quietly when possible": "در صورت امکان بی‌صدا اجرا شود",
    "This app has only one way to be removed.": "این برنامه فقط یک روش حذف دارد.",
    "Look for leftovers afterwards": "بعد از حذف، باقی‌مانده‌ها بررسی شوند",
    "The review only lists what was left behind. Upstream deletes nothing from it.":
        "این بررسی فقط فهرست می‌کند چه چیزی جا مانده؛ Upstream چیزی از آن را پاک نمی‌کند.",
    "Another uninstall is still running. Wait until it finishes.": "حذف دیگری هنوز در جریان است. صبر کن تمام شود.",
    "An uninstall is still running. Wait until it finishes.": "حذف یک برنامه هنوز در جریان است. صبر کن تمام شود.",
    "I understand that other apps or devices may stop working":
        "می‌دانم که ممکن است برنامه‌ها یا دستگاه‌های دیگر از کار بیفتند",
    "Updates are running right now. Removing an app at the same time could make both fail, so wait until they finish.":
        "الان به‌روزرسانی در جریان است. حذف هم‌زمان یک برنامه ممکن است هر دو را خراب کند؛ صبر کن تمام شوند.",
    "Uninstalling {name}": "در حال حذف {name}",
    "Finish the steps in the uninstaller's own window. Windows may ask for permission first.":
        "مراحل را در پنجرهٔ خودِ حذف‌کننده ادامه بده. ممکن است ویندوز اول اجازه بخواهد.",
    "This can take a minute. Windows may ask for permission.":
        "ممکن است یک دقیقه طول بکشد. ممکن است ویندوز اجازه بخواهد.",
    "The uninstaller is still running. Closing this window does not stop it, and Upstream will no longer show how it went.":
        "حذف‌کننده هنوز در حال کار است. بستن این پنجره آن را متوقف نمی‌کند و Upstream دیگر نتیجه‌اش را نشان نمی‌دهد.",
    "Keep waiting": "منتظر می‌مانم",
    "Close window": "بستن پنجره",
    "An uninstall is still running. Closing Upstream does not stop it, but Upstream will no longer show how it went.":
        "حذف یک برنامه هنوز در جریان است. بستن Upstream آن را متوقف نمی‌کند، ولی Upstream دیگر نتیجه‌اش را نشان نمی‌دهد.",
    "Removed": "حذف شد",
    "Restart needed": "نیاز به راه‌اندازی دوباره",
    "Not removed yet": "هنوز حذف نشده",
    "Cancelled": "لغو شد",
    "Windows is busy": "ویندوز مشغول است",
    "Couldn't remove it": "حذف نشد",
    "Try with its own window": "امتحان با پنجرهٔ خود برنامه",
    "The uninstaller may still be working, or it finished without removing the app.":
        "شاید حذف‌کننده هنوز کار می‌کند، یا بدون حذف برنامه تمام شده است.",
    "Still listed. If the uninstaller is still working, wait a moment and check again.":
        "هنوز در فهرست است. اگر حذف‌کننده هنوز کار می‌کند، کمی صبر کن و دوباره بررسی کن.",
    # leftover review
    "Leftovers": "باقی‌مانده‌ها",
    "Looking for leftovers…": "در حال جست‌وجوی باقی‌مانده‌ها…",
    "Looking for leftovers failed: {error}": "جست‌وجوی باقی‌مانده‌ها ناموفق بود: {error}",
    "Upstream found nothing that was left behind.": "هیچ مورد باقی‌مانده‌ای پیدا نشد.",
    "{n} item found": "{n} مورد پیدا شد",
    "{n} items found": "{n} مورد پیدا شد",
    "Item": "مورد",
    "Type": "نوع",
    "Why": "دلیل",
    "Folder": "پوشه",
    "File": "فایل",
    "Shortcut": "میان‌بر",
    "Registry key": "کلید رجیستری",
    "Registry value": "مقدار رجیستری",
    "Service": "سرویس",
    "Certain": "قطعی",
    "Likely": "به احتمال زیاد",
    "Possible": "نامطمئن",
    "Protected": "محافظت‌شده",
    "Inside the app's folder, or pointing into it": "داخل پوشهٔ برنامه‌اند یا به آن اشاره می‌کنند",
    "Named exactly like the app": "دقیقاً هم‌نام برنامه‌اند",
    "Weaker matches: check them yourself": "تطابق ضعیف‌تر؛ خودت بررسی کن",
    "Found, but Upstream will never offer to remove them": "پیدا شدند، ولی Upstream هرگز پیشنهاد حذفشان را نمی‌دهد",
    "Nothing on this list has been deleted. It is only a report: cleanup with backup and undo will come in a later version.":
        "هیچ‌کدام از موارد این فهرست پاک نشده‌اند. این فقط یک گزارش است؛ پاک‌سازی همراه با پشتیبان و بازگردانی "
        "در نسخه‌ای بعدی اضافه می‌شود.",
    "Select a row to see its full location and why it was found.":
        "یک ردیف را انتخاب کن تا محل کامل و دلیل پیدا شدنش را ببینی.",
    "Size: {size}": "حجم: {size}",
    "Size not measured": "حجم اندازه‌گیری نشد",
    "Open location": "باز کردن محل",
    "That location no longer exists.": "این محل دیگر وجود ندارد.",
    "Copied the registry key. Paste it into the address bar of Registry Editor.":
        "کلید رجیستری کپی شد. آن را در نوار آدرس Registry Editor بچسبان.",
    "Export list…": "ذخیرهٔ فهرست…",
    "Export list": "ذخیرهٔ فهرست",
    "Text files (*.txt)": "فایل‌های متنی (*.txt)",
    "Leftovers of {name}": "باقی‌مانده‌های {name}",
    "Made by Upstream on {date}": "ساخته‌شده با Upstream در {date}",
    "Couldn't save the list: {error}": "ذخیرهٔ فهرست ممکن نشد: {error}",
    "Saved to {path}": "ذخیره شد در {path}",
    # uninstaller
    "Upstream can't remove itself.": "حذف خود Upstream از اینجا ممکن نیست.",
    "Upstream runs on this Python, so removing it would break Upstream.":
        "برنامهٔ Upstream روی همین Python اجرا می‌شود، پس حذفش آن را از کار می‌اندازد.",
    "{name} installs and removes Store apps, so it can't be removed here.":
        "{name} برنامه‌های فروشگاه را نصب و حذف می‌کند، برای همین از اینجا حذف نمی‌شود.",
    "{name} is a Windows component that other apps need, so it can't be removed here.":
        "{name} بخشی از ویندوز است که برنامه‌های دیگر به آن نیاز دارند، برای همین از اینجا حذف نمی‌شود.",
    "{name} is part of Windows and can't be removed here.": "{name} جزئی از ویندوز است و از اینجا حذف نمی‌شود.",
    "{name} is marked as not removable.": "{name} به‌عنوان غیرقابل‌حذف علامت‌گذاری شده است.",
    "{name} has no uninstaller registered with Windows.": "حذف‌کننده‌ای برای {name} در ویندوز ثبت نشده است.",
    "The uninstaller for {name} is missing: {path}": "حذف‌کنندهٔ {name} پیدا نشد: {path}",
    "winget isn't available, so {name} can't be removed from here.":
        "ابزار winget در دسترس نیست، برای همین {name} از اینجا حذف نمی‌شود.",
    "Other apps may need this. Removing it can stop them from working.":
        "ممکن است برنامه‌های دیگر به این نیاز داشته باشند. حذفش می‌تواند جلوی کار آن‌ها را بگیرد.",
    "Uninstalling (Windows may ask for permission)": "در حال حذف (ممکن است ویندوز اجازه بخواهد)",
    "Uninstalling (winget)": "در حال حذف (winget)",
    "Waiting for the uninstaller to finish": "منتظر پایان کار حذف‌کننده",
    "Checking that {name} is gone": "در حال بررسی اینکه {name} حذف شده",
    "{name} was removed": "{name} حذف شد",
    "{name} was removed (the uninstaller reported {code})": "{name} حذف شد (حذف‌کننده کد {code} را گزارش کرد)",
    "Restart Windows to finish removing {name}": "برای تمام شدن حذف {name}، ویندوز را دوباره راه‌اندازی کن",
    "Windows still lists {name}": "ویندوز هنوز {name} را در فهرست دارد",
    "The uninstall was cancelled": "حذف لغو شد",
    "Another installation is running. Try again when it has finished.":
        "نصب دیگری در جریان است. بعد از تمام شدنش دوباره امتحان کن.",
    "The uninstaller exited with code {code}": "حذف‌کننده با کد {code} بسته شد",
    "Couldn't start the uninstaller (Windows error {code}).": "اجرای حذف‌کننده ممکن نشد (خطای ویندوز {code}).",
    # leftover reasons (leftovers.py)
    "The app's install folder": "پوشهٔ نصب برنامه",
    "The install folder, worked out from where the uninstaller or icon is":
        "پوشهٔ نصب، که از محل حذف‌کننده یا آیکون برنامه حدس زده شده",
    "Inside the app's install folder": "داخل پوشهٔ نصب برنامه",
    "Another installed app uses this folder: {other}": "برنامهٔ نصب‌شدهٔ دیگری از این پوشه استفاده می‌کند: {other}",
    "Another installed app lives inside this folder: {other}": "برنامهٔ نصب‌شدهٔ دیگری داخل این پوشه است: {other}",
    "Another installed app has the same name: {other}": "برنامهٔ نصب‌شدهٔ دیگری همین نام را دارد: {other}",
    "A link to somewhere else; Upstream never follows links":
        "پیوندی به جای دیگر؛ Upstream هرگز پیوندها را دنبال نمی‌کند",
    "Folder named after the app": "پوشه‌ای به نام برنامه",
    "Folder named after the app, inside its publisher's folder": "پوشه‌ای به نام برنامه، داخل پوشهٔ ناشرش",
    "Name looks like the app's name": "نامش شبیه نام برنامه است",
    "The publisher's folder would be left empty": "پوشهٔ ناشر خالی می‌ماند",
    "Registry key named after the app": "کلید رجیستری به نام برنامه",
    "Registry key named after the app, under its publisher's key": "کلید رجیستری به نام برنامه، زیر کلید ناشرش",
    "Registry key whose name looks like the app's name": "کلید رجیستری که نامش شبیه نام برنامه است",
    "Registry key with values that point into the install folder":
        "کلید رجیستری با مقادیری که به پوشهٔ نصب اشاره می‌کنند",
    "Registry key that points into another installed app: {other}":
        "کلید رجیستری که به برنامهٔ نصب‌شدهٔ دیگری اشاره می‌کند: {other}",
    "Shortcut to a program in the install folder": "میان‌بر به برنامه‌ای در پوشهٔ نصب",
    "Shortcut in the app's own Start menu folder": "میان‌بر در پوشهٔ خود برنامه در منوی Start",
    "Shortcut named after the app whose program no longer exists":
        "میان‌بر به نام برنامه، در حالی که خود برنامه دیگر وجود ندارد",
    "Shortcut to another installed app: {other}": "میان‌بر به برنامهٔ نصب‌شدهٔ دیگری: {other}",
    "Start menu folder that would be empty without the app's shortcuts":
        "پوشهٔ منوی Start که بدون میان‌برهای برنامه خالی می‌ماند",
    "Startup entry that runs a program from the install folder":
        "مورد راه‌اندازی که برنامه‌ای را از پوشهٔ نصب اجرا می‌کند",
    "On/off switch of a startup entry that points into the install folder":
        "کلید روشن/خاموش یک مورد راه‌اندازی که به پوشهٔ نصب اشاره می‌کند",
    "App Paths entry for a program in the install folder": "مورد App Paths برای برنامه‌ای در پوشهٔ نصب",
    "Windows service that runs a program from the install folder":
        "سرویس ویندوزی که برنامه‌ای را از پوشهٔ نصب اجرا می‌کند",
    "The app's own entry in Settings > Apps": "مورد خود برنامه در تنظیمات › برنامه‌ها",
    "A second Settings > Apps entry for the same app, and its uninstaller is gone":
        "مورد دومی برای همین برنامه در تنظیمات › برنامه‌ها، و حذف‌کننده‌اش دیگر وجود ندارد",
    "A matching Settings > Apps entry still has a working uninstaller":
        "مورد مشابهی در تنظیمات › برنامه‌ها هنوز حذف‌کنندهٔ سالم دارد",
    "A matching Settings > Apps entry whose uninstaller Upstream could not check":
        "مورد مشابهی در تنظیمات › برنامه‌ها که Upstream نتوانست حذف‌کننده‌اش را بررسی کند",
    "It contains programs (.exe or .dll files), so check it before removing":
        "فایل‌های اجرایی (exe یا dll) دارد، پس پیش از حذف بررسی‌اش کن",
    "It was changed in the last 10 minutes": "در ۱۰ دقیقهٔ اخیر تغییر کرده",
    "It is large (over 5,000 files or 2 GB)": "حجیم است (بیش از ۵٬۰۰۰ فایل یا ۲ گیگابایت)",
    # protected places (guard.py)
    "Not a full path": "مسیر کامل نیست",
    "On a network or removable drive": "روی درایو شبکه‌ای یا جداشدنی",
    "A drive's root folder": "پوشهٔ ریشهٔ یک درایو",
    "Part of Windows": "جزئی از ویندوز",
    "A folder that holds many apps' files": "پوشه‌ای که فایل‌های برنامه‌های زیادی در آن است",
    "A user profile folder": "پوشهٔ پروفایل کاربر",
    "A standard Windows folder": "یکی از پوشه‌های استاندارد ویندوز",
    "Owned by Microsoft or Windows": "متعلق به مایکروسافت یا ویندوز",
    "Shared files used by many programs": "فایل‌های مشترکی که برنامه‌های زیادی از آن‌ها استفاده می‌کنند",
    "The temporary files folder": "پوشهٔ فایل‌های موقت",
    "Upstream's own files or the Python it runs on": "فایل‌های خود Upstream یا نسخهٔ Python که با آن اجرا می‌شود",
    "Contains folders that must never be removed": "پوشه‌هایی دارد که هرگز نباید حذف شوند",
    "Not a registry key Upstream can remove": "کلیدی از رجیستری نیست که Upstream بتواند حذفش کند",
    "A top-level registry key": "کلید سطح‌بالای رجیستری",
    "A registry area shared by Windows and every app": "بخشی از رجیستری که ویندوز و همهٔ برنامه‌ها از آن استفاده می‌کنند",
    "Registry area owned by Microsoft or Windows": "بخشی از رجیستری که متعلق به مایکروسافت یا ویندوز است",
    "File-type and COM registrations shared with Windows": "ثبت نوع فایل‌ها و COM که با ویندوز مشترک است",
    "Group policy settings": "تنظیمات سیاست گروهی",
    "System registry area, changed only through Windows services":
        "بخش سیستمی رجیستری که فقط از طریق سرویس‌های ویندوز تغییر می‌کند",
    "Outside the Software branch of the registry": "بیرون از شاخهٔ Software رجیستری",
    "Only startup entries are removed one value at a time": "فقط مقدارهای راه‌اندازی را می‌توان تک‌به‌تک حذف کرد",
}

_TABLES = {"fa": FA}
_lang = "en"


def set_language(lang: str):
    global _lang
    _lang = lang if lang in LANGUAGES else "en"


def language() -> str:
    return _lang


def is_rtl() -> bool:
    return _lang in RTL_LANGUAGES


def num(value) -> str:
    text = str(value)
    return text.translate(_FA_DIGITS) if _lang == "fa" else text


def isolate(text: str) -> str:
    return f"{_FSI}{text}{_PDI}" if text and is_rtl() else text


def isolate_lines(text: str) -> str:
    """Per line, because a line break inside a label does not start a new bidi paragraph."""
    return "\n".join(isolate(line) for line in text.split("\n")) if is_rtl() else text


def tr(source: str, /, **values) -> str:
    table = _TABLES.get(_lang)
    out = table.get(source, source) if table else source
    if not values:
        return out
    if is_rtl():
        values = {k: isolate(v) if isinstance(v, str) else v for k, v in values.items()}
    return out.format(**values)


def trn(singular: str, plural: str, n: int, /, **values) -> str:
    return tr(singular if n == 1 else plural, n=num(n), **values)


def render(template: str, args: dict | None = None) -> str:
    """Translate a stored (English template, args) pair in the current language."""
    if not template:
        return ""
    args = {k: tr(v) if k in _TRANSLATED_ARGS and isinstance(v, str) else v for k, v in (args or {}).items()}
    return tr(template, **args)


def english(template: str, args: dict | None = None) -> str:
    """The same pair as plain English, for the log file."""
    return template.format(**args) if args else template

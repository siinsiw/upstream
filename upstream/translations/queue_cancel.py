"""Persian strings for one feature. English text is the key; see i18n.py."""
FA: dict[str, str] = {
    "Cancel all": "لغو همه",
    "Stop waiting": "منتظر نمان",
    "Upstream stops waiting and moves on. The installer itself is not stopped.":
        "انتظار برای نصب‌کننده تمام می‌شود و نوبت برنامهٔ بعدی می‌رسد. خود نصب‌کننده متوقف نمی‌شود.",
    "Cancelling…": "در حال لغو…",
    "Stopped waiting for the installer. It may still be running.":
        "انتظار برای نصب‌کننده متوقف شد. ممکن است هنوز در حال اجرا باشد.",
    "The installer was still running after {minutes} minutes, so Upstream stopped waiting for it. "
    "It may still be running.":
        "نصب‌کننده پس از {minutes} دقیقه هنوز کار می‌کرد، برای همین انتظار برایش متوقف شد. "
        "ممکن است هنوز در حال اجرا باشد.",
    "Lost track of the installer (Windows error {code}).": "ردیابی نصب‌کننده ممکن نشد (خطای ویندوز {code}).",
}

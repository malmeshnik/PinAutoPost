// Stores the browser time zone in the cookie read by TimezoneMiddleware,
// so admin timestamps are rendered in the viewer's local time.
(function () {
  var COOKIE = "django_timezone";
  var timeZone;
  try {
    timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (error) {
    return;
  }
  if (!timeZone) {
    return;
  }

  var current = null;
  document.cookie.split("; ").forEach(function (part) {
    if (part.indexOf(COOKIE + "=") === 0) {
      current = decodeURIComponent(part.slice(COOKIE.length + 1));
    }
  });
  if (current === timeZone) {
    return;
  }

  document.cookie =
    COOKIE + "=" + encodeURIComponent(timeZone) +
    "; path=/; max-age=31536000; SameSite=Lax";

  // Re-render once so the first page is shown in local time as well.
  if (current === null) {
    try {
      if (sessionStorage.getItem(COOKIE + "_reloaded")) {
        return;
      }
      sessionStorage.setItem(COOKIE + "_reloaded", "1");
    } catch (error) {
      return;
    }
    window.location.reload();
  }
})();

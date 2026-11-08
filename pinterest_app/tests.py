from django.test import Client, TestCase
from django.contrib import admin
from django.contrib.auth.models import User
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)
from pinterest_app.models import (
    PinterestAccount,
    PinterestBoard,
    PinPublishTask,
    SystemAlertState,
)
from pinterest_app.monitoring import (
    HEALTH_SNAPSHOT_KEY,
    _check_database_alert,
    _probe_beat,
    check_and_alert_system_health,
    save_health_snapshot,
)
from pinterest_app.cookies import CookieFormatError, normalize_cookies
from pinterest_app.proxies import ProxyFormatError, normalize_proxy
from pinterest_app.serializers import PinPublishSerializer
from pinterest_app.services import (
    PinterestServiceError,
    check_proxy,
    extract_base_url,
    get_session_and_csrf,
    publish_pin_for_task,
    refresh_boards,
)
from pinterest_app.tasks import publish_pin_task, refresh_all_accounts_boards_task
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import Mock, patch
from datetime import timedelta
from django.utils import timezone
import uuid


class BaseURLExtractionTests(TestCase):
    def test_extract_base_url_from_dict_with_url(self):
        """Test extracting base URL from cookies dict with url field"""
        cookies = {
            "url": "https://ru.pinterest.com",
            "cookies": [{"name": "test", "value": "123"}]
        }
        base_url = extract_base_url(cookies)
        self.assertEqual(base_url, "ru.pinterest.com")

    def test_extract_base_url_from_dict_with_www(self):
        """Test extracting base URL from www.pinterest.com"""
        cookies = {
            "url": "https://www.pinterest.com",
            "cookies": [{"name": "test", "value": "123"}]
        }
        base_url = extract_base_url(cookies)
        self.assertEqual(base_url, "www.pinterest.com")

    def test_extract_base_url_default_for_list(self):
        """Test that list format returns default www.pinterest.com"""
        cookies = [{"name": "test", "value": "123"}]
        base_url = extract_base_url(cookies)
        self.assertEqual(base_url, "www.pinterest.com")

    def test_extract_base_url_default_for_dict_without_url(self):
        """Test that dict without url field returns default"""
        cookies = {"cookies": [{"name": "test", "value": "123"}]}
        base_url = extract_base_url(cookies)
        self.assertEqual(base_url, "www.pinterest.com")

    def test_extract_base_url_handles_invalid_url(self):
        cookies = {"url": "http://127.0.0.1/admin", "cookies": []}
        with self.assertRaises(ValueError):
            extract_base_url(cookies)


class CookieNormalizationTests(TestCase):
    NOW = 1_800_000_000

    def test_extension_export_is_filtered_deduplicated_and_trimmed(self):
        exported = [
            {
                "name": "csrftoken", "value": "old", "domain": ".pinterest.com",
                "path": "/", "hostOnly": False, "httpOnly": False, "secure": True,
                "sameSite": "lax", "storeId": "0", "expirationDate": self.NOW + 100.5,
            },
            {"name": "csrftoken", "value": "new", "domain": ".pinterest.com", "path": "/"},
            {"name": "_pinterest_sess", "value": "sess", "domain": ".pinterest.com"},
            {"name": "_auth", "value": "1", "domain": ".pinterest.com",
             "expirationDate": self.NOW - 1},
            {"name": "NID", "value": "x", "domain": ".google.com"},
        ]

        result = normalize_cookies(exported, now=self.NOW)

        self.assertEqual(result["url"], "https://www.pinterest.com")
        self.assertEqual(
            result["cookies"],
            [
                {"name": "_pinterest_sess", "value": "sess",
                 "domain": ".pinterest.com", "path": "/"},
                {"name": "csrftoken", "value": "new",
                 "domain": ".pinterest.com", "path": "/"},
            ],
        )

    def test_header_string_and_url_object(self):
        result = normalize_cookies(
            {
                "url": "https://ru.pinterest.com/some/page/",
                "cookies": "csrftoken=abc; _pinterest_sess=a=b=c",
            },
            now=self.NOW,
        )
        self.assertEqual(result["url"], "https://ru.pinterest.com")
        values = {c["name"]: c["value"] for c in result["cookies"]}
        self.assertEqual(values, {"csrftoken": "abc", "_pinterest_sess": "a=b=c"})

    def test_netscape_and_json_string_formats(self):
        netscape = (
            "# Netscape HTTP Cookie File\n"
            f".pinterest.com\tTRUE\t/\tTRUE\t{self.NOW + 60}\tcsrftoken\tabc\n"
            "#HttpOnly_.pinterest.com\tTRUE\t/\tTRUE\t0\t_pinterest_sess\tsess\n"
            ".example.com\tTRUE\t/\tFALSE\t0\tother\tx\n"
        )
        result = normalize_cookies(netscape, now=self.NOW)
        self.assertEqual(
            {c["name"] for c in result["cookies"]}, {"csrftoken", "_pinterest_sess"}
        )
        csrf = next(c for c in result["cookies"] if c["name"] == "csrftoken")
        self.assertEqual(csrf["expires"], self.NOW + 60)

        json_string = '[{"name":"csrftoken","value":"a"},{"name":"_pinterest_sess","value":"b"}]'
        self.assertEqual(len(normalize_cookies(json_string)["cookies"]), 2)

    def test_missing_login_cookies_are_reported(self):
        with self.assertRaisesMessage(CookieFormatError, "_pinterest_sess"):
            normalize_cookies([{"name": "csrftoken", "value": "a"}])
        with self.assertRaises(CookieFormatError):
            normalize_cookies({"url": "https://evil.example", "cookies": [
                {"name": "csrftoken", "value": "a"},
                {"name": "_pinterest_sess", "value": "b"},
            ]})

    def test_normalized_cookies_work_for_publishing_session(self):
        user = User.objects.create_user(username="cookie-user", password="pw")
        account = PinterestAccount.objects.create(
            user=user,
            name="normalized",
            cookies=normalize_cookies(
                {"url": "https://ru.pinterest.com", "cookies": "csrftoken=t; _pinterest_sess=s"}
            ),
            proxy="http://proxy.example:8080",
        )
        session, csrf_token, base_url = get_session_and_csrf(account)
        session.close()
        self.assertEqual(csrf_token, "t")
        self.assertEqual(base_url, "ru.pinterest.com")


class AccountApiTests(TestCase):
    COOKIES = [
        {"name": "csrftoken", "value": "csrf", "domain": ".pinterest.com", "path": "/"},
        {"name": "_pinterest_sess", "value": "sess", "domain": ".pinterest.com"},
        {"name": "NID", "value": "x", "domain": ".google.com"},
    ]

    def setUp(self):
        self.user = User.objects.create_user(username="api-user", password="api-pass-123")

    def _token_client(self):
        response = APIClient().post(
            "/api/v1/auth/token/",
            {"username": "api-user", "password": "api-pass-123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        return client

    def test_token_is_required_and_wrong_password_is_rejected(self):
        response = APIClient().post("/api/v1/accounts/", {}, format="json")
        self.assertIn(
            response.status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )
        response = APIClient().post(
            "/api/v1/auth/token/",
            {"username": "api-user", "password": "wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_account_with_token_normalizes_cookies_and_returns_boards(self):
        def fake_refresh(account, deadline=None):
            PinterestBoard.objects.create(
                account=account, board_id="b1", name="Recipes", url="/recipes/"
            )
            return 1

        with patch("pinterest_app.views.check_proxy"), patch(
            "pinterest_app.views.refresh_boards", side_effect=fake_refresh
        ):
            response = self._token_client().post(
                "/api/v1/accounts/",
                {
                    "name": "Kitchen",
                    "cookies": self.COOKIES,
                    "proxy": " http://user:pass@proxy.example:8080 ",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["boards"][0]["board_id"], "b1")
        self.assertIn("/api/v1/publish/", response.data["webhook_url"])
        self.assertNotIn("cookies", response.data)
        account = PinterestAccount.objects.get(name="Kitchen")
        self.assertEqual(account.user, self.user)
        self.assertEqual(account.proxy, "http://user:pass@proxy.example:8080")
        self.assertEqual(account.cookies["url"], "https://www.pinterest.com")
        self.assertEqual(
            [c["name"] for c in account.cookies["cookies"]],
            ["_pinterest_sess", "csrftoken"],
        )

    def test_create_account_accepts_provider_proxy_format_and_type(self):
        with patch("pinterest_app.views.check_proxy") as check_proxy_mock, patch(
            "pinterest_app.views.refresh_boards", return_value=1
        ):
            response = self._token_client().post(
                "/api/v1/accounts/",
                {
                    "name": "Garden",
                    "cookies": self.COOKIES,
                    "proxy": "10.1.2.3:8000:login:secret",
                    "proxy_type": "socks5",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        check_proxy_mock.assert_called_once_with("socks5://login:secret@10.1.2.3:8000")
        self.assertEqual(
            PinterestAccount.objects.get(name="Garden").proxy,
            "socks5://login:secret@10.1.2.3:8000",
        )

    def test_create_account_rejects_bad_proxy_without_echoing_it(self):
        with patch("pinterest_app.views.check_proxy") as check_proxy_mock:
            response = self._token_client().post(
                "/api/v1/accounts/",
                {"name": "Bad", "cookies": self.COOKIES, "proxy": "login:secret"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("proxy", response.data)
        self.assertNotIn("secret", str(response.data))
        check_proxy_mock.assert_not_called()

    def test_rejected_cookies_do_not_leave_an_account_behind(self):
        with patch("pinterest_app.views.check_proxy"), patch(
            "pinterest_app.views.refresh_boards",
            side_effect=PinterestServiceError("auth_error", "expired"),
        ):
            response = self._token_client().post(
                "/api/v1/accounts/",
                {"name": "Expired", "cookies": self.COOKIES, "proxy": "http://p.example:1"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["account"]["error"], "auth_error")
        self.assertIn("not saved", str(response.data["account"]["message"]))
        self.assertFalse(PinterestAccount.objects.exists())

    def test_invalid_cookie_format_is_rejected_before_network(self):
        with patch("pinterest_app.views.check_proxy") as check_proxy_mock:
            response = self._token_client().post(
                "/api/v1/accounts/",
                {"name": "Bad", "cookies": [{"name": "csrftoken", "value": "a"}],
                 "proxy": "http://p.example:1"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cookies", response.data)
        check_proxy_mock.assert_not_called()


class ProxyNormalizationTests(TestCase):
    def test_supported_formats(self):
        cases = [
            ("10.0.0.1:8000:user:secret", None, "http://user:secret@10.0.0.1:8000"),
            ("10.0.0.1:8000:user:secret", "socks5", "socks5://user:secret@10.0.0.1:8000"),
            ("user:secret:10.0.0.1:8000", None, "http://user:secret@10.0.0.1:8000"),
            ("user:secret@proxy.example:3128", None, "http://user:secret@proxy.example:3128"),
            ("proxy.example:3128", "socks5h", "socks5h://proxy.example:3128"),
            (" socks5://u:p%40@proxy.example:1080 ", None, "socks5://u:p%40@proxy.example:1080"),
            ("http://u:p@proxy.example:1080", "socks5", "socks5://u:p@proxy.example:1080"),
            ("10.0.0.1:8000:user:pa:ss", None, "http://user:pa%3Ass@10.0.0.1:8000"),
        ]
        for raw, proxy_type, expected in cases:
            with self.subTest(raw=raw, proxy_type=proxy_type):
                self.assertEqual(normalize_proxy(raw, proxy_type), expected)

    def test_invalid_proxies_do_not_leak_credentials(self):
        for raw in (
            "10.0.0.1:99999:user:secret",
            "ftp://user:secret@host:21",
            "user:secret",
            "http://user:secret@host",
            "",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ProxyFormatError) as context:
                    normalize_proxy(raw)
                self.assertNotIn("secret", str(context.exception))


class AdminCreateTests(TestCase):
    INLINE_MANAGEMENT = {
        "boards-TOTAL_FORMS": "0",
        "boards-INITIAL_FORMS": "0",
        "boards-MIN_NUM_FORMS": "0",
        "boards-MAX_NUM_FORMS": "1000",
    }

    def setUp(self):
        from django.urls import reverse

        self.reverse = reverse
        self.admin_user = User.objects.create_superuser(username="root", password="pw")
        self.client = Client()
        self.client.force_login(self.admin_user)

    def _account_payload(self, **overrides):
        payload = {
            "name": "kitchen_pins",
            "user": str(self.admin_user.pk),
            "proxy_type": "socks5",
            "proxy": "10.1.2.3:8000:login:p@ss:word",
            "cookies_input": "csrftoken=abc; _pinterest_sess=sess; other=1",
            **self.INLINE_MANAGEMENT,
        }
        payload.update(overrides)
        return payload

    def test_admin_can_add_account_with_normalized_cookies_and_boards(self):
        with patch("pinterest_app.forms.check_proxy"), patch(
            "pinterest_app.forms.fetch_boards",
            return_value=[{"board_id": "b1", "name": "Recipes", "url": "/recipes/"}],
        ):
            response = self.client.post(
                self.reverse("admin:pinterest_app_pinterestaccount_add"),
                self._account_payload(),
            )

        self.assertEqual(response.status_code, 302)
        account = PinterestAccount.objects.get(name="kitchen_pins")
        self.assertTrue(account.is_active)
        self.assertEqual(account.proxy, "socks5://login:p%40ss%3Aword@10.1.2.3:8000")
        self.assertEqual(
            {c["name"] for c in account.cookies["cookies"]},
            {"csrftoken", "_pinterest_sess", "other"},
        )
        self.assertEqual(list(account.boards.values_list("board_id", flat=True)), ["b1"])

    def test_admin_account_with_rejected_cookies_is_not_saved(self):
        with patch("pinterest_app.forms.check_proxy"), patch(
            "pinterest_app.forms.fetch_boards",
            side_effect=PinterestServiceError("auth_error", "expired"),
        ):
            response = self.client.post(
                self.reverse("admin:pinterest_app_pinterestaccount_add"),
                self._account_payload(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("cookies_input", response.context["adminform"].form.errors)
        self.assertFalse(PinterestAccount.objects.exists())

    def test_admin_account_add_page_renders(self):
        response = self.client.get(self.reverse("admin:pinterest_app_pinterestaccount_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="cookies_input"')

    def test_admin_can_add_publish_task_and_it_is_queued_after_commit(self):
        account = PinterestAccount.objects.create(
            user=self.admin_user,
            name="kitchen_pins",
            cookies=[{"name": "csrftoken", "value": "x"}],
            proxy="http://proxy.example:8080",
        )
        board = PinterestBoard.objects.create(
            account=account, board_id="b1", name="Recipes", url="/recipes/"
        )
        add_url = self.reverse("admin:pinterest_app_pinpublishtask_add")
        self.assertContains(self.client.get(add_url), "kitchen_pins → Recipes")

        with patch("pinterest_app.admin.publish_pin_task.delay") as delay_mock:
            delay_mock.return_value.id = "celery-admin"
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    add_url,
                    {
                        "board": str(board.pk),
                        "title": "Soup",
                        "description": "",
                        "link": "",
                        "image_url": "https://image.example/soup.jpg",
                    },
                )

        self.assertEqual(response.status_code, 302)
        task = PinPublishTask.objects.get()
        self.assertEqual(task.account, account)
        self.assertEqual(task.board_id, "b1")
        self.assertTrue(task.idempotency_key.startswith("admin:"))
        delay_mock.assert_called_once_with(task.id)
        task.refresh_from_db()
        self.assertEqual(task.celery_task_id, "celery-admin")

        change = self.client.get(
            self.reverse("admin:pinterest_app_pinpublishtask_change", args=[task.pk])
        )
        self.assertEqual(change.status_code, 200)


class ProxyDiagnosticTests(TestCase):
    def test_socks_authentication_failure_has_actionable_safe_message(self):
        with patch(
            "pinterest_app.services.curl_requests.get",
            side_effect=RuntimeError(
                "User was rejected by the SOCKS5 server; user=private password=secret"
            ),
        ):
            with self.assertRaises(PinterestServiceError) as context:
                check_proxy("socks5://private:secret@proxy.example:1080")

        self.assertEqual(context.exception.code, "proxy_error")
        self.assertIn("логін і пароль", context.exception.message)
        self.assertIn("закодуйте їх для URL", context.exception.message)
        self.assertNotIn("private", context.exception.message)
        self.assertNotIn("secret", context.exception.message)

    def test_proxy_timeout_message_suggests_checks(self):
        with patch(
            "pinterest_app.services.curl_requests.get",
            side_effect=RuntimeError("Connection timed out"),
        ):
            with self.assertRaises(PinterestServiceError) as context:
                check_proxy("http://proxy.example:8080")

        self.assertIn("очікування відповіді проксі", context.exception.message)
        self.assertIn("hostname і порт", context.exception.message)

    def test_proxy_http_407_is_reported_as_authentication_failure(self):
        response = Mock(status_code=407)
        with patch("pinterest_app.services.curl_requests.get", return_value=response):
            with self.assertRaises(PinterestServiceError) as context:
                check_proxy("http://proxy.example:8080")

        self.assertIn("відхилив авторизацію", context.exception.message)

    def test_proxy_connection_refused_suggests_endpoint_checks(self):
        with patch(
            "pinterest_app.services.curl_requests.get",
            side_effect=RuntimeError("Connection refused"),
        ):
            with self.assertRaises(PinterestServiceError) as context:
                check_proxy("socks5://proxy.example:1080")

        self.assertIn("Не вдалося підключитися до проксі", context.exception.message)
        self.assertIn("порт і тип проксі", context.exception.message)


class MonitoringAlertTests(TestCase):
    def test_database_alert_is_throttled_and_sends_recovery(self):
        redis_client = Mock()
        redis_client.set.side_effect = [True, None]
        redis_client.get.return_value = b"1"

        with patch(
            "pinterest_app.monitoring.redis.Redis.from_url",
            return_value=redis_client,
        ), patch(
            "pinterest_app.monitoring.send_telegram_alert", return_value=True
        ) as send_alert:
            _check_database_alert(database_ok=False, redis_ok=True)
            _check_database_alert(database_ok=False, redis_ok=True)
            _check_database_alert(database_ok=True, redis_ok=True)

        self.assertEqual(send_alert.call_count, 2)
        self.assertIn("не може підключитися", send_alert.call_args_list[0].args[0])
        self.assertIn("відновлено", send_alert.call_args_list[1].args[0])
        redis_client.delete.assert_called_once()


class AdminRegistrationTests(TestCase):
    def test_celery_beat_models_are_hidden_from_admin(self):
        beat_models = (
            PeriodicTask,
            IntervalSchedule,
            CrontabSchedule,
            SolarSchedule,
            ClockedSchedule,
        )

        for model in beat_models:
            with self.subTest(model=model.__name__):
                self.assertFalse(admin.site.is_registered(model))

    def test_admin_index_renders_monitoring_dashboard(self):
        from django.urls import reverse

        user = User.objects.create_superuser(
            username="dashboard-admin",
            password="test-password",
        )
        client = Client()
        self.assertTrue(client.login(username=user.username, password="test-password"))

        save_health_snapshot(
            [
                {"name": "Database", "ok": True, "detail": "PostgreSQL connection"},
                {"name": "Redis", "ok": False, "detail": "Celery broker connection"},
                {"name": "Celery workers", "ok": True, "detail": "ping"},
                {"name": "Celery beat", "ok": None, "detail": "heartbeat"},
            ]
        )
        with patch("pinterest_app.monitoring.get_system_health") as probe_mock:
            response = client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTemplateUsed(response, "admin/dashboard.html")
        probe_mock.assert_not_called()
        dashboard = response.context["dashboard"]
        self.assertEqual(
            [component["state"] for component in dashboard["health"]],
            ["ok", "issue", "ok", "unknown"],
        )
        self.assertFalse(dashboard["health_stale"])
        self.assertEqual(dashboard["health_issues"], 1)

    def test_dashboard_marks_health_unknown_when_monitor_stops_reporting(self):
        from django.urls import reverse

        user = User.objects.create_superuser(username="stale-admin", password="pw")
        client = Client()
        client.force_login(user)
        SystemAlertState.objects.create(
            key=HEALTH_SNAPSHOT_KEY,
            payload={
                "checked_at": (timezone.now() - timedelta(minutes=10)).isoformat(),
                "components": [{"name": "Redis", "ok": True, "detail": "broker"}],
            },
        )

        response = client.get(reverse("admin:index"))

        dashboard = response.context["dashboard"]
        self.assertTrue(dashboard["health_stale"])
        self.assertEqual(dashboard["health"][1]["state"], "unknown")


class HealthDebounceTests(TestCase):
    def test_single_failed_check_does_not_alert(self):
        def services(workers_ok):
            return [
                {"name": "Database", "ok": True, "detail": "db"},
                {"name": "Redis", "ok": True, "detail": "redis"},
                {"name": "Celery workers", "ok": workers_ok, "detail": "ping"},
            ]

        with patch(
            "pinterest_app.monitoring._check_database_alert"
        ), patch(
            "pinterest_app.monitoring.send_telegram_alert", return_value=True
        ) as send_alert, patch(
            "pinterest_app.monitoring.get_system_health",
            side_effect=[services(False), services(True), services(False), services(False)],
        ):
            check_and_alert_system_health()
            snapshot = SystemAlertState.objects.get(key=HEALTH_SNAPSHOT_KEY).payload
            self.assertIsNone(snapshot["components"][2]["ok"])
            check_and_alert_system_health()
            check_and_alert_system_health()
            send_alert.assert_not_called()

            check_and_alert_system_health()

        send_alert.assert_called_once()
        self.assertIn("Celery workers", send_alert.call_args.args[0])


class BeatHeartbeatTests(TestCase):
    def test_beat_probe_uses_heartbeat_age(self):
        client = Mock()
        with patch(
            "pinterest_app.monitoring.redis.Redis.from_url", return_value=client
        ):
            client.get.return_value = str(timezone.now().timestamp()).encode()
            self.assertTrue(_probe_beat())
            client.get.return_value = str(
                (timezone.now() - timedelta(minutes=5)).timestamp()
            ).encode()
            self.assertFalse(_probe_beat())
            client.get.return_value = None
            self.assertIsNone(_probe_beat())


class SessionCreationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')

    def test_get_session_extracts_csrf_token(self):
        """Test that CSRF token is extracted from cookies"""
        account = PinterestAccount.objects.create(
            user=self.user,
            name="Test Account",
            cookies={
                "url": "https://www.pinterest.com",
                "cookies": [
                    {"name": "csrftoken", "value": "test_csrf_123", "domain": ".pinterest.com", "path": "/"},
                    {"name": "_auth", "value": "1", "domain": ".pinterest.com", "path": "/"}
                ]
            },
            proxy="http://user:pass@proxy.example.com:8080"
        )

        session, csrf_token, base_url = get_session_and_csrf(account)
        self.assertEqual(csrf_token, "test_csrf_123")
        self.assertEqual(base_url, "www.pinterest.com")
        session.close()

    def test_refresh_boards_replaces_data_after_valid_api_response(self):
        account = PinterestAccount.objects.create(
            user=self.user,
            name="Test Account",
            cookies=[{"name": "csrftoken", "value": "csrf"}],
            proxy="http://proxy.example.com:8080",
        )
        PinterestBoard.objects.create(
            account=account, board_id="old", name="Old Board", url="/old/"
        )
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "resource_response": {
                "data": [{"id": "new", "name": "New Board", "url": "/new/"}]
            }
        }
        with patch(
            "pinterest_app.services.get_session_and_csrf",
            return_value=(session, "csrf", "www.pinterest.com"),
        ):
            self.assertEqual(refresh_boards(account), 1)

        self.assertFalse(account.boards.filter(board_id="old").exists())
        self.assertEqual(account.boards.get().board_id, "new")

    def test_retryable_board_api_error_preserves_existing_data(self):
        account = PinterestAccount.objects.create(
            user=self.user,
            name="Test Account",
            cookies=[{"name": "csrftoken", "value": "csrf"}],
            proxy="http://proxy.example.com:8080",
        )
        existing_board = PinterestBoard.objects.create(
            account=account, board_id="old", name="Old Board", url="/old/"
        )
        session = Mock()
        session.post.return_value.status_code = 503
        with patch(
            "pinterest_app.services.get_session_and_csrf",
            return_value=(session, "csrf", "www.pinterest.com"),
        ):
            with self.assertRaises(PinterestServiceError) as context:
                refresh_boards(account)

        self.assertEqual(context.exception.code, "proxy_error")
        self.assertTrue(PinterestBoard.objects.filter(pk=existing_board.pk).exists())

    def test_get_session_handles_list_cookies(self):
        """Test that old list format cookies still work"""
        account = PinterestAccount.objects.create(
            user=self.user,
            name="Test Account",
            cookies=[
                {"name": "csrftoken", "value": "csrf_456", "domain": ".pinterest.com", "path": "/"}
            ],
            proxy="http://proxy.example.com:8080"
        )

        session, csrf_token, base_url = get_session_and_csrf(account)
        self.assertEqual(csrf_token, "csrf_456")
        self.assertEqual(base_url, "www.pinterest.com")
        session.close()


class PinterestAppTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(username='user1', password='password')
        self.user2 = User.objects.create_user(username='user2', password='password')
        self.admin = User.objects.create_superuser(username='admin', password='password')

        self.acc1 = PinterestAccount.objects.create(
            user=self.user1,
            name="Acc 1",
            cookies=[{"name": "test", "value": "123"}],
            proxy="http://proxy1"
        )
        self.acc2 = PinterestAccount.objects.create(
            user=self.user2,
            name="Acc 2",
            cookies=[{"name": "test", "value": "456"}],
            proxy="http://proxy2"
        )

        self.board1 = PinterestBoard.objects.create(
            account=self.acc1,
            board_id="p1",
            name="Board 1",
            url="/b1/"
        )

    def test_multi_tenancy_api(self):
        client = APIClient()
        client.force_authenticate(user=self.user1)

        # User 1 should only see their own account
        response = client.get('/api/v1/accounts/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Acc 1")
        self.assertNotIn("cookies", response.data[0])
        self.assertNotIn("proxy", response.data[0])

        # User 2 should only see their own
        client.force_authenticate(user=self.user2)
        response = client.get('/api/v1/accounts/')
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Acc 2")

        # Admin should see everything
        client.force_authenticate(user=self.admin)
        response = client.get('/api/v1/accounts/')
        self.assertEqual(len(response.data), 2)

    def test_account_creation_returns_actionable_proxy_diagnostic(self):
        client = APIClient()
        client.force_authenticate(user=self.user1)
        message = "Проксі відхилив авторизацію. Перевірте логін і пароль."
        with patch(
            "pinterest_app.views.check_proxy",
            side_effect=PinterestServiceError("proxy_error", message),
        ):
            response = client.post(
                "/api/v1/accounts/",
                {
                    "name": "Proxy test",
                    "cookies": [
                        {"name": "csrftoken", "value": "csrf", "domain": ".pinterest.com"},
                        {"name": "_pinterest_sess", "value": "s", "domain": ".pinterest.com"},
                    ],
                    "proxy": "socks5://proxy.example:1080",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["proxy"], message)
        self.assertFalse(PinterestAccount.objects.filter(name="Proxy test").exists())

    def test_monitor_alerts_repeated_proxy_errors_once_and_notifies_recovery(self):
        for index in range(3):
            PinPublishTask.objects.create(
                account=self.acc1,
                image_url=f"https://image.example/{index}.jpg",
                board_id="p1",
                status="FAILED",
                error_json={"error": "proxy_error", "message": "Proxy unavailable."},
            )

        healthy_services = [
            {"name": name, "ok": True, "detail": "available"}
            for name in ("Database", "Redis", "Celery workers", "Celery beat")
        ]
        with patch(
            "pinterest_app.monitoring.get_system_health",
            return_value=healthy_services,
        ), patch(
            "pinterest_app.monitoring._check_database_alert"
        ), patch(
            "pinterest_app.monitoring.send_telegram_alert"
        ) as send_alert:
            check_and_alert_system_health()
            check_and_alert_system_health()

            self.assertEqual(send_alert.call_count, 1)
            self.assertIn("3 помилки", send_alert.call_args.args[0])
            self.assertNotIn("Proxy unavailable", send_alert.call_args.args[0])

            PinPublishTask.objects.update(updated_at=timezone.now() - timedelta(minutes=20))
            check_and_alert_system_health()

        self.assertEqual(send_alert.call_count, 2)
        self.assertIn("відновлено", send_alert.call_args.args[0])

    def test_webhook_returns_raw_pinterest_response_without_wait_parameters(self):
        client = APIClient()
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        pinterest_response = Mock()
        pinterest_response.status_code = status.HTTP_200_OK
        pinterest_response.content = b'{"resource_response":{"data":{"id":"pin-123"}}}'
        pinterest_response.headers = {"content-type": "application/json"}
        pinterest_response.json.return_value = {
            "resource_response": {"data": {"id": "pin-123"}}
        }
        with patch(
            "pinterest_app.services.create_pin", return_value=pinterest_response
        ) as create_pin_mock, patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch("pinterest_app.services.refresh_boards", return_value=1):
            response = client.post(
                url,
                {
                    "image_url": "https://image.example/image.jpg",
                    "board_name": "Board 1"
                },
                format='json',
                HTTP_IDEMPOTENCY_KEY="publish-test-1",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, pinterest_response.content)
        self.assertEqual(response["Content-Type"], "application/json")
        create_pin_mock.assert_called_once()
        task = PinPublishTask.objects.get()
        self.assertEqual(task.account, self.acc1)
        self.assertEqual(task.status, "SUCCESS")
        self.assertEqual(response["X-PinAutoPost-Task-ID"], str(task.id))
        self.assertIn(str(task.status_token), response["X-PinAutoPost-Status-URL"])

    def test_webhook_preserves_pinterest_error_response(self):
        client = APIClient()
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        pinterest_response = Mock()
        pinterest_response.status_code = status.HTTP_400_BAD_REQUEST
        pinterest_response.content = b'{"resource_response":{"error":{"message":"Invalid board"}}}'
        pinterest_response.headers = {"content-type": "application/json"}
        pinterest_response.json.return_value = {
            "resource_response": {"error": {"message": "Invalid board"}}
        }
        with patch(
            "pinterest_app.services.create_pin", return_value=pinterest_response
        ), patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch("pinterest_app.services.refresh_boards", return_value=1):
            response = client.post(
                url,
                {
                    "image_url": "https://image.example/image.jpg",
                    "board_name": "Board 1"
                },
                format='json',
                HTTP_IDEMPOTENCY_KEY="publish-error-1",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.content, pinterest_response.content)
        task = PinPublishTask.objects.get()
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "api_error")

    def _ok_pin_response(self):
        pinterest_response = Mock()
        pinterest_response.status_code = status.HTTP_200_OK
        pinterest_response.content = b'{"resource_response":{"data":{"id":"pin-1"}}}'
        pinterest_response.headers = {"content-type": "application/json"}
        pinterest_response.json.return_value = {
            "resource_response": {"data": {"id": "pin-1"}}
        }
        return pinterest_response

    def test_publish_without_idempotency_key_is_accepted_without_dedup(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        payload = {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"}
        with patch(
            "pinterest_app.services.create_pin", return_value=self._ok_pin_response()
        ) as create_pin_mock, patch("pinterest_app.services.check_proxy"):
            first = APIClient().post(url, payload, format="json")
            second = APIClient().post(url, payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(create_pin_mock.call_count, 2)
        self.assertEqual(PinPublishTask.objects.count(), 2)

    def test_wait_false_queues_task_and_returns_status_url(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/?wait=false'
        with patch("pinterest_app.views.publish_pin_task.delay") as delay_mock:
            delay_mock.return_value.id = "celery-1"
            response = APIClient().post(
                url,
                {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        task = PinPublishTask.objects.get()
        delay_mock.assert_called_once_with(task.id)
        self.assertEqual(task.celery_task_id, "celery-1")
        self.assertIn(str(task.status_token), response.data["status_url"])

    def test_wait_false_reports_queue_outage_as_retryable(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/?wait=false'
        with patch(
            "pinterest_app.views.publish_pin_task.delay",
            side_effect=ConnectionError("redis down"),
        ):
            response = APIClient().post(
                url,
                {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="queue-down",
            )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertTrue(PinPublishTask.objects.get().error_json["retryable"])

    def test_legacy_integer_status_route_exposes_status_only(self):
        task = PinPublishTask.objects.create(
            account=self.acc1,
            image_url="https://image.example/image.jpg",
            board_id="p1",
            status="FAILED",
            error_json={"error": "proxy_error", "message": "secret details"},
        )
        response = APIClient().get(f"/api/v1/task/{task.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "failed")
        self.assertEqual(response.data["error"], "proxy_error")
        self.assertNotIn("message", response.data)

    def test_equivalent_retry_with_nulls_is_not_a_conflict(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        with patch(
            "pinterest_app.services.create_pin", return_value=self._ok_pin_response()
        ) as create_pin_mock, patch("pinterest_app.services.check_proxy"):
            first = APIClient().post(
                url,
                {
                    "image_url": "https://image.example/image.jpg",
                    "board_name": "Board 1",
                    "link": None,
                    "board_id": None,
                    "description": "",
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="same-request",
            )
            second = APIClient().post(
                url,
                {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="same-request",
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(create_pin_mock.call_count, 1)

    def test_retry_with_same_key_republishes_after_retryable_failure(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        payload = {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"}
        with patch(
            "pinterest_app.services.check_proxy",
            side_effect=PinterestServiceError("proxy_error", "Proxy down."),
        ), patch("pinterest_app.services.sleep"), patch(
            "pinterest_app.services.create_pin"
        ) as create_pin_mock:
            first = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="retry-me"
            )
        self.assertEqual(first.status_code, status.HTTP_502_BAD_GATEWAY)
        create_pin_mock.assert_not_called()

        with patch(
            "pinterest_app.services.create_pin", return_value=self._ok_pin_response()
        ) as create_pin_mock, patch("pinterest_app.services.check_proxy"):
            second = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="retry-me"
            )

        self.assertEqual(second.status_code, status.HTTP_200_OK)
        create_pin_mock.assert_called_once()
        self.assertEqual(PinPublishTask.objects.get().status, "SUCCESS")

    def test_publish_unknown_is_not_republished_with_same_key(self):
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        payload = {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"}
        with patch("pinterest_app.services.check_proxy"), patch(
            "pinterest_app.services.create_pin",
            side_effect=PinterestServiceError("publish_unknown", "maybe sent"),
        ) as create_pin_mock:
            first = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="unknown"
            )
            second = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="unknown"
            )

        self.assertEqual(first.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        self.assertEqual(second.content, first.content)
        self.assertEqual(create_pin_mock.call_count, 1)

    def test_stale_processing_task_is_resolved_on_retry(self):
        from pinterest_app.views import _request_fingerprint

        payload = {"image_url": "https://image.example/image.jpg", "board_name": "Board 1"}
        task = PinPublishTask.objects.create(
            account=self.acc1,
            status="PROCESSING",
            idempotency_key="crashed",
            request_fingerprint=_request_fingerprint(payload),
            **payload,
        )
        PinPublishTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )
        with patch("pinterest_app.services.create_pin") as create_pin_mock:
            response = APIClient().post(
                f'/api/v1/publish/{self.acc1.webhook_token}/',
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="crashed",
            )

        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)
        create_pin_mock.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "publish_unknown")

    def test_webhook_rejects_title_over_pinterest_limit_before_creating_task(self):
        response = APIClient().post(
            f'/api/v1/publish/{self.acc1.webhook_token}/',
            {
                "title": "x" * 101,
                "image_url": "https://image.example/image.jpg",
                "board_name": "Board 1",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="long-title",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", response.data)
        self.assertEqual(PinPublishTask.objects.count(), 0)

    def test_webhook_accepts_title_at_pinterest_limit(self):
        serializer = PinPublishSerializer(
            data={
                "title": "x" * 100,
                "image_url": "https://image.example/image.jpg",
                "board_name": "Board 1",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_repeated_idempotency_key_returns_same_response_without_reposting(self):
        pinterest_response = Mock()
        pinterest_response.status_code = status.HTTP_200_OK
        pinterest_response.content = b'{"resource_response":{"data":{"id":"pin-123"}}}'
        pinterest_response.headers = {"content-type": "application/json"}
        pinterest_response.json.return_value = {
            "resource_response": {"data": {"id": "pin-123"}}
        }
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        payload = {
            "image_url": "https://image.example/image.jpg",
            "board_name": "Board 1",
        }
        with patch(
            "pinterest_app.services.create_pin", return_value=pinterest_response
        ) as create_pin_mock, patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch("pinterest_app.services.refresh_boards", return_value=1):
            first = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="logical-pin-1"
            )
            second = APIClient().post(
                url, payload, format="json", HTTP_IDEMPOTENCY_KEY="logical-pin-1"
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.content, first.content)
        self.assertEqual(
            second["X-PinAutoPost-Task-ID"], first["X-PinAutoPost-Task-ID"]
        )
        self.assertEqual(create_pin_mock.call_count, 1)
        self.assertEqual(PinPublishTask.objects.count(), 1)

    def test_idempotency_key_cannot_be_reused_for_different_payload(self):
        response_mock = Mock()
        response_mock.status_code = status.HTTP_200_OK
        response_mock.content = b'{"resource_response":{"data":{"id":"pin-123"}}}'
        response_mock.headers = {"content-type": "application/json"}
        response_mock.json.return_value = {
            "resource_response": {"data": {"id": "pin-123"}}
        }
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        with patch(
            "pinterest_app.services.create_pin", return_value=response_mock
        ), patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch("pinterest_app.services.refresh_boards", return_value=1):
            first = APIClient().post(
                url,
                {
                    "image_url": "https://image.example/image.jpg",
                    "board_name": "Board 1",
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="logical-pin-2",
            )
            second = APIClient().post(
                url,
                {
                    "image_url": "https://image.example/other.jpg",
                    "board_name": "Board 1",
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="logical-pin-2",
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["error"], "idempotency_key_conflict")
        self.assertEqual(PinPublishTask.objects.count(), 1)

    def test_board_refresh_mock(self):
        # This tests if the action is reachable
        client = APIClient()
        client.force_authenticate(user=self.user1)
        response = client.get(f'/api/v1/accounts/{self.acc1.id}/boards/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], "Board 1")

    def test_inactive_account_webhook_rejected(self):
        """Test that inactive accounts cannot publish pins"""
        self.acc1.is_active = False
        self.acc1.save()

        client = APIClient()
        url = f'/api/v1/publish/{self.acc1.webhook_token}/'
        response = client.post(url, {
            "image_url": "http://image.jpg",
            "board_name": "Board 1"
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

    def test_get_task_status_pending(self):
        """Test getting status of a pending task"""
        client = APIClient()
        task = PinPublishTask.objects.create(
            account=self.acc1,
            image_url="http://image.jpg",
            board_id="123",
            status='PENDING'
        )

        response = client.get(
            f'/api/v1/publish/status/{task.status_token}/'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'pending')

    def test_get_task_status_success(self):
        """Test getting status of a successful task"""
        client = APIClient()
        task = PinPublishTask.objects.create(
            account=self.acc1,
            image_url="http://image.jpg",
            board_id="123",
            status='SUCCESS'
        )

        response = client.get(
            f'/api/v1/publish/status/{task.status_token}/'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'success')
        self.assertIn('message', response.data)

    def test_get_task_status_failed(self):
        """Test getting status of a failed task with error details"""
        client = APIClient()
        task = PinPublishTask.objects.create(
            account=self.acc1,
            image_url="http://image.jpg",
            board_id="123",
            status='FAILED',
            error_json={
                "error": "auth_error",
                "message": "Cookies expired at https://proxy-user:secret@example.com"
            }
        )

        response = client.get(
            f'/api/v1/publish/status/{task.status_token}/'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data['error'], 'auth_error')
        self.assertIn('message', response.data)
        self.assertNotIn("proxy-user:secret", response.data["message"])

    def test_get_task_status_preserves_actionable_proxy_message(self):
        message = "Проксі відхилив авторизацію. Перевірте логін і пароль."
        task = PinPublishTask.objects.create(
            account=self.acc1,
            image_url="https://image.example/image.jpg",
            board_id="123",
            status="FAILED",
            error_json={"error": "proxy_error", "message": message},
        )

        response = APIClient().get(
            f"/api/v1/publish/status/{task.status_token}/"
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["message"], message)

    def test_task_status_requires_the_task_status_token(self):
        PinPublishTask.objects.create(
            account=self.acc1,
            image_url="https://image.example/image.jpg",
            board_id="123",
        )
        response = APIClient().get(
            f'/api/v1/publish/status/{uuid.uuid4()}/'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PinPublishTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.account = PinterestAccount.objects.create(
            user=self.user,
            name="Test Account",
            cookies=[{"name": "test", "value": "123"}],
            proxy="http://proxy.example.com:8080"
        )
        PinterestBoard.objects.create(
            account=self.account,
            board_id="123",
            name="Test Board",
            url="/test-board/",
        )

    def test_task_creation(self):
        """Test that task is created with correct default status"""
        task = PinPublishTask.objects.create(
            account=self.account,
            title="Test Pin",
            description="Test Description",
            image_url="http://example.com/image.jpg",
            board_id="123"
        )

        self.assertEqual(task.status, 'PENDING')
        self.assertIsNone(task.error_json)

    def test_scheduled_board_refresh_without_account_ids_targets_active_accounts(self):
        with patch("pinterest_app.tasks.refresh_boards") as refresh_mock:
            refresh_all_accounts_boards_task.run()

        refresh_mock.assert_called_once()
        self.assertEqual(refresh_mock.call_args.args[0], self.account)

    def test_task_error_json_structure(self):
        """Test that error_json has correct structure"""
        task = PinPublishTask.objects.create(
            account=self.account,
            title="Test Pin",
            image_url="http://example.com/image.jpg",
            board_id="123",
            status='FAILED',
            error_json={
                "error": "auth_error",
                "message": "Cookies expired"
            }
        )

        self.assertEqual(task.error_json['error'], 'auth_error')
        self.assertIn('message', task.error_json)

    def test_proxy_preflight_failure_prevents_pin_request(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            image_url="https://image.example/image.jpg",
            board_id="123",
        )
        with patch(
            "pinterest_app.services.check_proxy",
            side_effect=PinterestServiceError(
                "proxy_error", "Проксі відхилив авторизацію."
            ),
        ) as check_proxy_mock, patch(
            "pinterest_app.services.sleep"
        ), patch("pinterest_app.services.create_pin") as create_pin_mock:
            publish_pin_for_task(task)

        task.refresh_from_db()
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "proxy_error")
        self.assertIn("відхилив авторизацію", task.error_json["message"])
        self.assertEqual(task.error_json["retry_count"], 2)
        self.assertTrue(task.error_json["retryable"])
        self.assertEqual(check_proxy_mock.call_count, 3)
        self.assertEqual(task.response_status_code, status.HTTP_502_BAD_GATEWAY)
        create_pin_mock.assert_not_called()

    def test_cached_board_id_does_not_refresh_boards(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            image_url="https://image.example/image.jpg",
            board_id="123",
        )
        pin_response = Mock(status_code=200, content=b"{}", headers={})
        pin_response.json.return_value = {"resource_response": {"data": {}}}
        with patch("pinterest_app.services.check_proxy"), patch(
            "pinterest_app.services.refresh_boards"
        ) as refresh_mock, patch(
            "pinterest_app.services.create_pin", return_value=pin_response
        ):
            publish_pin_for_task(task)

        refresh_mock.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, "SUCCESS")

    def test_unknown_board_name_refreshes_boards_once(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            image_url="https://image.example/image.jpg",
            board_name="New Board",
        )

        def add_board(account, deadline=None):
            PinterestBoard.objects.create(
                account=account, board_id="456", name="New Board", url="/new/"
            )
            return 1

        pin_response = Mock(status_code=200, content=b"{}", headers={})
        pin_response.json.return_value = {"resource_response": {"data": {}}}
        with patch("pinterest_app.services.check_proxy"), patch(
            "pinterest_app.services.refresh_boards", side_effect=add_board
        ) as refresh_mock, patch(
            "pinterest_app.services.create_pin", return_value=pin_response
        ) as create_pin_mock:
            publish_pin_for_task(task)

        refresh_mock.assert_called_once()
        self.assertEqual(create_pin_mock.call_args.kwargs["board_id"], "456")

    def test_task_rejects_legacy_title_over_pinterest_limit_before_network(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            title="x" * 101,
            image_url="https://image.example/image.jpg",
            board_id="123",
        )
        with patch(
            "pinterest_app.services.check_proxy"
        ) as check_proxy_mock, patch(
            "pinterest_app.services.create_pin"
        ) as create_pin_mock:
            publish_pin_for_task(task)

        task.refresh_from_db()
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "validation_error")
        self.assertEqual(task.response_status_code, status.HTTP_400_BAD_REQUEST)
        check_proxy_mock.assert_not_called()
        create_pin_mock.assert_not_called()

    def test_board_id_must_belong_to_account_before_pin_request(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            image_url="https://image.example/image.jpg",
            board_id="other-account-board",
        )
        with patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch(
            "pinterest_app.services.refresh_boards", return_value=1
        ), patch("pinterest_app.services.create_pin") as create_pin_mock:
            publish_pin_for_task(task)

        task.refresh_from_db()
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "validation_error")
        create_pin_mock.assert_not_called()

    def test_ambiguous_pin_timeout_is_not_retried(self):
        task = PinPublishTask.objects.create(
            account=self.account,
            image_url="https://image.example/image.jpg",
            board_id="123",
        )
        with patch(
            "pinterest_app.services.check_proxy", return_value=True
        ), patch(
            "pinterest_app.services.refresh_boards", return_value=1
        ), patch(
            "pinterest_app.services.create_pin",
            side_effect=PinterestServiceError(
                "publish_unknown", "safe timeout message"
            ),
        ) as create_pin_mock:
            publish_pin_task.apply(args=[task.id], throw=False)

        task.refresh_from_db()
        self.assertEqual(create_pin_mock.call_count, 1)
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.error_json["error"], "publish_unknown")


SUMMARY_RESPONSE = {
    "data": {
        "v3AnalyticsMetricsDeltasQuery": {
            "__typename": "AnalyticsSummaryMetricsResponse",
            "data": {
                "numOfDays": 31,
                "summaryMetrics": {
                    "impressionFloat": 125731.0,
                    "engagementFloat": 4779.0,
                    "outboundClickFloat": 1.0,
                    "saveFloat": 1439.0,
                },
                "deltas": {
                    "impression": -0.2414,
                    "engagement": -0.2481,
                    "outboundClick": None,
                    "save": -0.288,
                },
                "dateAvailability": {
                    "impression": {"isRealtime": True, "summaryDataStatus": "READY"},
                    "engagement": {"summaryDataStatus": "READY"},
                    "outboundClick": {"summaryDataStatus": "READY"},
                    "save": {"summaryDataStatus": "READY"},
                },
            },
        }
    }
}


def _json_response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class AccountStatsTests(TestCase):
    def setUp(self):
        from datetime import date

        self.today = date(2026, 11, 8)
        self.user = User.objects.create_user(username="stats-user", password="pw")
        self.account = PinterestAccount.objects.create(
            user=self.user,
            name="stats_account",
            cookies=[{"name": "csrftoken", "value": "csrf"}],
            proxy="http://proxy.example.com:8080",
        )

    def _session(self, *responses):
        session = Mock()
        session.request.side_effect = list(responses)
        return session

    def _refresh(self, session):
        from pinterest_app.analytics import refresh_account_stats

        with patch(
            "pinterest_app.analytics.get_session_and_csrf",
            return_value=(session, "csrf", "www.pinterest.com"),
        ):
            return refresh_account_stats(self.account, today=self.today)

    def test_period_and_variables_match_the_analytics_ui(self):
        from datetime import date
        from pinterest_app.analytics import build_summary_variables, stats_period

        start, end = stats_period(self.today)
        self.assertEqual((start, end), (date(2026, 10, 7), date(2026, 11, 6)))
        variables = build_summary_variables("1121326144629908734", start, end)
        self.assertEqual(variables["startTimestamp"], 1791331200)
        self.assertEqual(variables["endTimestamp"], 1794009599)
        self.assertEqual(variables["startDate"], "2026-10-07")
        self.assertEqual(variables["endDate"], "2026-11-06")

    def test_parse_summary_response(self):
        from pinterest_app.analytics import parse_summary_response

        values = parse_summary_response(SUMMARY_RESPONSE)
        self.assertEqual(values["impressions"], 125731)
        self.assertEqual(values["engagements"], 4779)
        self.assertEqual(values["outbound_clicks"], 1)
        self.assertEqual(values["saves"], 1439)
        self.assertAlmostEqual(values["impressions_delta"], -0.2414)
        self.assertIsNone(values["outbound_clicks_delta"])
        self.assertTrue(values["is_complete"])

    def test_refresh_resolves_user_id_once_and_stores_snapshot(self):
        import json as json_module

        user_response = _json_response(
            {"resource_response": {"data": {"id": "1121326144629908734"}}}
        )
        session = self._session(user_response, _json_response(SUMMARY_RESPONSE))
        snapshot = self._refresh(session)

        self.account.refresh_from_db()
        self.assertEqual(self.account.pinterest_user_id, "1121326144629908734")
        self.assertEqual(snapshot.impressions, 125731)
        self.assertEqual(str(snapshot.period_end), "2026-11-06")
        graphql_call = session.request.call_args_list[1].kwargs
        body = json_module.loads(graphql_call["data"])
        self.assertEqual(body["variables"]["user"], "1121326144629908734")
        self.assertEqual(graphql_call["headers"]["x-csrftoken"], "csrf")

        # The id is cached: the next refresh makes only the analytics request.
        session = self._session(_json_response(SUMMARY_RESPONSE))
        self._refresh(session)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(self.account.stats_snapshots.count(), 2)

    def test_user_id_comes_from_the_logged_in_session(self):
        user_response = _json_response(
            {
                "client_context": {"user": {"id": "1099793308911756505"}},
                "resource_response": {"data": {"id": "999"}},
            }
        )
        session = self._session(user_response, _json_response(SUMMARY_RESPONSE))
        self._refresh(session)

        self.account.refresh_from_db()
        self.assertEqual(self.account.pinterest_user_id, "1099793308911756505")
        session.cookies.set.assert_called_once_with(
            "csrftoken", "csrf", domain="analytics.pinterest.com", path="/"
        )
        profile_call = session.request.call_args_list[0].kwargs
        self.assertEqual(profile_call["headers"]["x-pinterest-pws-handler"], "www/[username].js")

    def test_failed_refresh_records_error_without_deactivating(self):
        self.account.pinterest_user_id = "1"
        self.account.save()
        session = self._session(
            _json_response({"errors": [{"message": "PersistedQueryNotFound"}]})
        )
        with self.assertRaises(PinterestServiceError):
            self._refresh(session)

        self.account.refresh_from_db()
        self.assertTrue(self.account.is_active)
        self.assertIn("query hash", self.account.stats_error)
        self.assertFalse(self.account.stats_snapshots.exists())

    def test_successful_refresh_clears_previous_error(self):
        self.account.pinterest_user_id = "1"
        self.account.stats_error = "old error"
        self.account.save()
        self._refresh(self._session(_json_response(SUMMARY_RESPONSE)))
        self.account.refresh_from_db()
        self.assertEqual(self.account.stats_error, "")

    def test_refresh_requests_are_limited_to_one_per_hour(self):
        from pinterest_app.analytics import request_stats_refresh

        with patch("pinterest_app.tasks.refresh_account_stats_task.delay") as delay_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(request_stats_refresh(self.account))
                self.assertFalse(request_stats_refresh(self.account))
            PinterestAccount.objects.filter(pk=self.account.pk).update(
                stats_requested_at=timezone.now() - timedelta(minutes=61)
            )
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(request_stats_refresh(self.account))
                self.assertTrue(request_stats_refresh(self.account, force=True))
        self.assertEqual(delay_mock.call_count, 3)

    def test_queue_failure_allows_the_next_request(self):
        from pinterest_app.analytics import request_stats_refresh

        with patch(
            "pinterest_app.tasks.refresh_account_stats_task.delay",
            side_effect=ConnectionError("redis down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                request_stats_refresh(self.account)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.stats_requested_at)

    def test_successful_publish_requests_stats_refresh(self):
        PinterestBoard.objects.create(
            account=self.account, board_id="b1", name="Board", url="/b/"
        )
        task = PinPublishTask.objects.create(
            account=self.account, image_url="https://image.example/i.jpg", board_id="b1"
        )
        response = Mock(status_code=200, headers={}, content=b"{}")
        response.json.return_value = {"resource_response": {"data": {"id": "pin"}}}
        with patch("pinterest_app.services.check_proxy"), patch(
            "pinterest_app.services.create_pin", return_value=response
        ), patch("pinterest_app.analytics.request_stats_refresh") as request_mock:
            publish_pin_for_task(task)

        task.refresh_from_db()
        self.assertEqual(task.status, "SUCCESS")
        request_mock.assert_called_once()

    def test_failed_publish_does_not_request_stats_refresh(self):
        task = PinPublishTask.objects.create(
            account=self.account, image_url="https://image.example/i.jpg", board_id="b1"
        )
        with patch(
            "pinterest_app.services.check_proxy",
            side_effect=PinterestServiceError("auth_error", "denied"),
        ), patch("pinterest_app.analytics.request_stats_refresh") as request_mock:
            publish_pin_for_task(task)
        request_mock.assert_not_called()

    def test_renaming_account_resets_user_id(self):
        self.account.pinterest_user_id = "123"
        self.account.save()
        account = PinterestAccount.objects.get(pk=self.account.pk)
        account.name = "new_name"
        account.save()
        account.refresh_from_db()
        self.assertEqual(account.pinterest_user_id, "")


class AccountStatsAdminTests(TestCase):
    def setUp(self):
        from datetime import date
        from pinterest_app.models import AccountStatsSnapshot

        self.admin_user = User.objects.create_superuser(username="stats-admin", password="pw")
        self.owner = User.objects.create_user(username="owner", password="pw", is_staff=True)
        self.other = User.objects.create_user(username="other", password="pw")
        self.account = PinterestAccount.objects.create(
            user=self.owner, name="mine", cookies=[], proxy="http://p:1"
        )
        self.foreign = PinterestAccount.objects.create(
            user=self.other, name="foreign", cookies=[], proxy="http://p:1"
        )
        for account, impressions, delta in (
            (self.account, 1000, 0.25),
            (self.foreign, 3000, -0.5),
        ):
            AccountStatsSnapshot.objects.create(
                account=account,
                period_start=date(2026, 10, 7),
                period_end=date(2026, 11, 6),
                impressions=impressions,
                engagements=10,
                outbound_clicks=0,
                saves=5,
                impressions_delta=delta,
            )

    def _url(self, account=None):
        from django.urls import reverse

        if account is None:
            return reverse("admin:pinterest_app_accountstatistics_changelist")
        return reverse("admin:pinterest_app_accountstatistics_change", args=[account.pk])

    def test_overview_shows_totals_for_all_accounts(self):
        client = Client()
        client.force_login(self.admin_user)
        response = client.get(self._url())

        self.assertEqual(response.status_code, 200)
        stats = response.context["stats"]
        self.assertEqual([row["account"].name for row in stats["rows"]], ["foreign", "mine"])
        impressions = stats["totals"][0]
        self.assertEqual(impressions["value"], "4 000")
        # Previous periods: 1000 / 1.25 + 3000 / 0.5 = 6800 -> 4000 / 6800 - 1.
        self.assertEqual(impressions["delta"]["text"], "-41.2%")
        self.assertContains(response, self._url(self.account))

    def test_staff_user_sees_only_own_accounts(self):
        from django.contrib.auth.models import Permission

        self.owner.user_permissions.add(
            Permission.objects.get(codename="view_pinterestaccount")
        )
        client = Client()
        client.force_login(self.owner)
        response = client.get(self._url())
        self.assertEqual(
            [row["account"].name for row in response.context["stats"]["rows"]], ["mine"]
        )
        self.assertEqual(client.get(self._url(self.foreign)).status_code, 404)
        # View permission alone does not allow queuing a refresh.
        self.assertEqual(client.post(self._url(self.account)).status_code, 403)

    def test_user_without_account_permission_is_denied(self):
        client = Client()
        client.force_login(self.owner)
        self.assertEqual(client.get(self._url()).status_code, 403)

    def test_detail_page_and_manual_refresh(self):
        client = Client()
        client.force_login(self.admin_user)
        url = self._url(self.account)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["metrics"][0]["delta"]["text"], "+25%")
        self.assertEqual(len(response.context["stats"]["history"]), 1)

        snapshot = self.account.stats_snapshots.get()
        with patch("pinterest_app.admin.refresh_account_stats", return_value=snapshot) as refresh_mock:
            response = client.post(url, follow=True)
        refresh_mock.assert_called_once_with(self.account)
        self.assertContains(response, "1 000")
        self.account.refresh_from_db()
        # A manual refresh counts as the hourly one.
        self.assertIsNotNone(self.account.stats_requested_at)

    def test_manual_refresh_shows_pinterest_error(self):
        client = Client()
        client.force_login(self.admin_user)
        with patch(
            "pinterest_app.admin.refresh_account_stats",
            side_effect=PinterestServiceError("api_error", "Pinterest Analytics returned HTTP 418."),
        ):
            response = client.post(self._url(self.account), follow=True)
        self.assertContains(response, "Pinterest Analytics returned HTTP 418.")

    def test_refresh_all_queues_active_accounts(self):
        PinterestAccount.objects.filter(pk=self.foreign.pk).update(is_active=False)
        client = Client()
        client.force_login(self.admin_user)
        with patch("pinterest_app.admin.request_stats_refresh", return_value=True) as request_mock:
            response = client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual([c.args[0] for c in request_mock.call_args_list], [self.account])
        self.assertTrue(request_mock.call_args.kwargs["force"])

    def test_account_list_links_to_statistics(self):
        from django.urls import reverse

        client = Client()
        client.force_login(self.admin_user)
        response = client.get(reverse("admin:pinterest_app_pinterestaccount_changelist"))
        self.assertContains(response, self._url(self.account))

    def test_main_dashboard_shows_statistics_tile(self):
        from django.urls import reverse

        client = Client()
        client.force_login(self.admin_user)
        response = client.get(reverse("admin:index"))
        self.assertEqual(
            response.context["dashboard"]["statistics"]["impressions"], "4 000"
        )

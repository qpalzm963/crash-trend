"""BigQuery 憑證解析測試。

守住 issue #79 的核心不變式：Crashlytics（fetch_bigquery.make_client）與
Sessions（fetch_sessions.make_sessions_client）兩條路徑必須以完全相同的規則解析憑證——
per-app 覆寫全域、adc/none/空值走 ADC、指定的檔案不存在時大聲失敗（不得退回 ADC）。
兩條路徑若再度分岔，同一個 App 的 pipeline 兩半就會用不同身分抓資料，是靜默的資料正確性問題。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.bq_credentials import (
    BQCredentialsError,
    make_bq_client,
    resolve_bq_credentials,
)


class TestResolveBQCredentials(unittest.TestCase):
    """憑證解析的單一實作（不碰 google 套件，純設定邏輯）。"""

    def setUp(self) -> None:
        # 用本檔案自身當「存在的檔案」，避免產生任何 SA json 檔
        self.existing = Path(__file__).resolve()

    # -- 優先序 --------------------------------------------------------------

    def test_per_app_overrides_global(self) -> None:
        """per-app bq_service_account 必須勝過全域，否則多 GCP 專案情境無法分身分。"""
        cfg = {"credentials": {"bq_service_account": "/nonexistent/global.json"}}
        got = resolve_bq_credentials({"bq_service_account": str(self.existing)}, cfg=cfg)
        self.assertEqual(got, self.existing)

    def test_global_used_when_app_has_no_key(self) -> None:
        cfg = {"credentials": {"bq_service_account": str(self.existing)}}
        got = resolve_bq_credentials({"display_name": "example_app"}, cfg=cfg)
        self.assertEqual(got, self.existing)

    def test_per_app_sentinel_overrides_global_service_account(self) -> None:
        """全域有 SA、單一 App 想改用 ADC：per-app 寫 sentinel 必須真的切回 ADC。"""
        cfg = {"credentials": {"bq_service_account": str(self.existing)}}
        self.assertIsNone(resolve_bq_credentials({"bq_service_account": "adc"}, cfg=cfg))
        self.assertIsNone(resolve_bq_credentials({"bq_service_account": None}, cfg=cfg))

    # -- sentinel ------------------------------------------------------------

    def test_sentinels_select_adc(self) -> None:
        for raw in ("adc", "ADC", " Adc ", "none", "NONE", "", "   "):
            with self.subTest(raw=raw):
                cfg = {"credentials": {"bq_service_account": raw}}
                self.assertIsNone(resolve_bq_credentials(None, cfg=cfg))

    # -- 預設行為 ------------------------------------------------------------

    def test_nothing_configured_selects_adc(self) -> None:
        self.assertIsNone(resolve_bq_credentials(None, cfg={}))
        self.assertIsNone(resolve_bq_credentials({}, cfg={"credentials": None}))
        self.assertIsNone(resolve_bq_credentials({"display_name": "example_app"}, cfg={"credentials": {}}))

    def test_falls_back_to_load_config_when_cfg_omitted(self) -> None:
        with patch(
            "crash_trend.bq_credentials.load_config",
            return_value={"credentials": {"bq_service_account": str(self.existing)}},
        ) as m:
            self.assertEqual(resolve_bq_credentials(), self.existing)
        m.assert_called_once()

    # -- 檔案不存在必須大聲失敗 ----------------------------------------------

    def test_missing_global_file_raises(self) -> None:
        cfg = {"credentials": {"bq_service_account": "/nonexistent/global-sa.json"}}
        with self.assertRaises(BQCredentialsError) as ctx:
            resolve_bq_credentials(None, cfg=cfg)
        self.assertIn("credentials.bq_service_account", str(ctx.exception))
        self.assertIn("/nonexistent/global-sa.json", str(ctx.exception))

    def test_missing_per_app_file_raises_and_names_per_app_source(self) -> None:
        cfg = {"credentials": {"bq_service_account": str(self.existing)}}
        with self.assertRaises(BQCredentialsError) as ctx:
            resolve_bq_credentials({"bq_service_account": "/nonexistent/app-sa.json"}, cfg=cfg)
        # 訊息要指出是 per-app 那把壞了，否則使用者會去查錯的設定
        self.assertIn("apps.<app>.bq_service_account", str(ctx.exception))
        self.assertIn("/nonexistent/app-sa.json", str(ctx.exception))

    def test_home_relative_path_is_expanded(self) -> None:
        cfg = {"credentials": {"bq_service_account": "~/nonexistent/sa.json"}}
        with self.assertRaises(BQCredentialsError) as ctx:
            resolve_bq_credentials(None, cfg=cfg)
        self.assertIn(str(Path.home()), str(ctx.exception))
        self.assertNotIn("~", str(ctx.exception))


class TestMakeBQClient(unittest.TestCase):
    """client 工廠：有 SA 就帶 credentials，否則走 ADC。"""

    def setUp(self) -> None:
        self.existing = str(Path(__file__).resolve())

    def test_service_account_path_builds_client_with_credentials(self) -> None:
        fake_creds = object()
        with (
            patch("google.cloud.bigquery.Client") as client_cls,
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                return_value=fake_creds,
            ) as from_file,
        ):
            make_bq_client("my-project-id", {"bq_service_account": self.existing}, cfg={})
        from_file.assert_called_once_with(self.existing)
        client_cls.assert_called_once_with(project="my-project-id", credentials=fake_creds)

    def test_adc_builds_client_without_credentials(self) -> None:
        with patch("google.cloud.bigquery.Client") as client_cls:
            make_bq_client("my-project-id", {"bq_service_account": "adc"}, cfg={})
        client_cls.assert_called_once_with(project="my-project-id")


class TestEntryPointsAgree(unittest.TestCase):
    """同一組設定餵給兩個進入點，必須產出同樣的憑證決策。

    這是 #79 的驗收核心：per-app 覆寫、sentinel、檔案不存在，
    在 Crashlytics 與 Sessions 兩條路徑上都要一致。
    """

    def setUp(self) -> None:
        self.existing = str(Path(__file__).resolve())

    def _call(self, entry: str, project: str, app_cfg: dict | None, cfg: dict):
        """以指定進入點建立 client。

        回傳 (bigquery.Client mock, from_service_account_file mock, 假憑證物件)，
        讓呼叫端可斷言「用了哪把憑證、client 是帶 credentials 還是走 ADC」。
        """
        fake_creds = object()
        with (
            patch("crash_trend.bq_credentials.load_config", return_value=cfg),
            patch("google.cloud.bigquery.Client") as client_cls,
            patch(
                "google.oauth2.service_account.Credentials.from_service_account_file",
                return_value=fake_creds,
            ) as from_file,
        ):
            client_cls.return_value = MagicMock()
            if entry == "crashlytics":
                from crash_trend.fetch_bigquery import make_client

                make_client(project, app_cfg=app_cfg)
            else:
                from crash_trend.fetch_sessions import make_sessions_client

                make_sessions_client(project, app_cfg=app_cfg)
        return client_cls, from_file, fake_creds

    def test_per_app_override_wins_on_both_paths(self) -> None:
        cfg = {"credentials": {"bq_service_account": "/nonexistent/global.json"}}
        for entry in ("crashlytics", "sessions"):
            with self.subTest(entry=entry):
                client_cls, from_file, fake_creds = self._call(
                    entry, "my-project-id", {"bq_service_account": self.existing}, cfg
                )
                from_file.assert_called_once_with(self.existing)
                client_cls.assert_called_once_with(project="my-project-id", credentials=fake_creds)

    def test_global_applies_on_both_paths(self) -> None:
        cfg = {"credentials": {"bq_service_account": self.existing}}
        for entry in ("crashlytics", "sessions"):
            with self.subTest(entry=entry):
                client_cls, from_file, fake_creds = self._call(entry, "my-project-id", {}, cfg)
                from_file.assert_called_once_with(self.existing)
                client_cls.assert_called_once_with(project="my-project-id", credentials=fake_creds)

    def test_sentinels_select_adc_on_both_paths(self) -> None:
        """`adc` 以前在 Sessions 端被當成檔案路徑而炸掉；兩端必須都解讀為 ADC。"""
        for entry in ("crashlytics", "sessions"):
            for raw in ("adc", "none", ""):
                with self.subTest(entry=entry, raw=raw):
                    cfg = {"credentials": {"bq_service_account": raw}}
                    client_cls, from_file, _ = self._call(entry, "my-project-id", None, cfg)
                    from_file.assert_not_called()
                    client_cls.assert_called_once_with(project="my-project-id")

    def test_nothing_configured_selects_adc_on_both_paths(self) -> None:
        for entry in ("crashlytics", "sessions"):
            with self.subTest(entry=entry):
                client_cls, from_file, _ = self._call(entry, "my-project-id", {}, {})
                from_file.assert_not_called()
                client_cls.assert_called_once_with(project="my-project-id")

    def test_missing_file_fails_loudly_on_both_paths(self) -> None:
        """設定了 SA 但檔案不存在時，兩端都不得建立任何 client（尤其不得退回 ADC）。"""
        cfg = {"credentials": {"bq_service_account": "/nonexistent/global-sa.json"}}

        with (
            patch("crash_trend.bq_credentials.load_config", return_value=cfg),
            patch("google.cloud.bigquery.Client") as client_cls,
        ):
            from crash_trend.fetch_bigquery import make_client

            # CLI 進入點以 sys.exit 帶明確訊息中止
            with self.assertRaises(SystemExit) as ctx:
                make_client("my-project-id")
            self.assertIn("/nonexistent/global-sa.json", str(ctx.exception))
            client_cls.assert_not_called()

        with (
            patch("crash_trend.bq_credentials.load_config", return_value=cfg),
            patch("google.cloud.bigquery.Client") as client_cls,
        ):
            from crash_trend.fetch_sessions import make_sessions_client

            # 函式庫進入點拋出可捕捉的例外，呼叫端據此標記 unavailable
            with self.assertRaises(BQCredentialsError) as err:
                make_sessions_client("my-project-id")
            self.assertIn("/nonexistent/global-sa.json", str(err.exception))
            client_cls.assert_not_called()

    def test_missing_per_app_file_fails_loudly_on_both_paths(self) -> None:
        cfg = {"credentials": {"bq_service_account": self.existing}}
        app_cfg = {"bq_service_account": "/nonexistent/app-sa.json"}

        with (
            patch("crash_trend.bq_credentials.load_config", return_value=cfg),
            patch("google.cloud.bigquery.Client") as client_cls,
        ):
            from crash_trend.fetch_bigquery import make_client

            with self.assertRaises(SystemExit):
                make_client("my-project-id", app_cfg=app_cfg)
            client_cls.assert_not_called()

        with (
            patch("crash_trend.bq_credentials.load_config", return_value=cfg),
            patch("google.cloud.bigquery.Client") as client_cls,
        ):
            from crash_trend.fetch_sessions import make_sessions_client

            with self.assertRaises(BQCredentialsError):
                make_sessions_client("my-project-id", app_cfg=app_cfg)
            client_cls.assert_not_called()


class TestSessionsCredentialFailureDegrades(unittest.TestCase):
    """Sessions 端憑證失敗時要降級為 unavailable 並帶原因，而不是產生假的 0%。"""

    def test_missing_sa_marks_sessions_unavailable_with_reason(self) -> None:
        from crash_trend.fetch_sessions import fetch_sessions_data

        cfg = {"credentials": {"bq_service_account": "/nonexistent/global-sa.json"}}
        with patch("crash_trend.bq_credentials.load_config", return_value=cfg):
            res = fetch_sessions_data(project="my-project-id", app_config={})

        self.assertEqual(res["sources"]["status"], "unavailable")
        self.assertIn("/nonexistent/global-sa.json", res["sources"]["error_message"])
        # 不得產生假的 0%：rate/total/crashed 必須是 null
        metric = res["kpi"]["crash_free_sessions"]
        self.assertEqual(metric["status"], "unavailable")
        self.assertIsNone(metric["rate"])
        self.assertIsNone(metric["total"])
        self.assertIn("/nonexistent/global-sa.json", metric["unavailable_reason"])

    def test_per_app_service_account_reaches_sessions_client(self) -> None:
        """回歸守門：fetch_sessions_data 必須把 app_config 傳進 client 工廠，
        否則 per-app 覆寫在 Sessions 端會被靜默忽略（#79 的原始 bug）。"""
        from crash_trend import fetch_sessions

        with patch.object(fetch_sessions, "make_sessions_client") as factory:
            factory.return_value = MagicMock()
            fetch_sessions.fetch_sessions_data(
                project="my-project-id",
                tables=[],
                app_config={"bq_service_account": "/some/app-sa.json"},
            )
        factory.assert_called_once_with(
            "my-project-id", app_cfg={"bq_service_account": "/some/app-sa.json"}
        )


if __name__ == "__main__":
    unittest.main()

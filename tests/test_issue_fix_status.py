"""Tests for issue-centric fix status detection (detect_issue_fix_status).

這組測試守住的是「為什麼需要這個函式」，而不只是它算出什麼：

detect_issue_lifecycle 的 `resolved` 以版本為主體，要求 issue 在緊鄰的前一版
仍然活躍，才算得上是最新版的修復功勞。這個契約對「這一版修好了什麼」是正確的，
但無法回答「這個舊問題現在還在不在」——一個在更早版本就消失的 issue，在那個
契約下永遠是 0。test_complements_lifecycle_resolved_blind_spot 就是釘住這個差異：
若哪天有人把兩者合併或讓 fix_status 去沿用 resolved 的前一版條件，該測試必須紅。

證據強度同樣是business logic而非實作細節：sessions 是曝光量的直接證據，
crash_events 只是間接代理，因此後者判定出的 likely_fixed 信心度必須較低——
否則「無 Sessions 也能給出高信心的已修復」會讓使用者誤信一個無法證實的結論。
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crash_trend.gate.policy import load_gate_policy
from crash_trend.lifecycle import detect_issue_fix_status, detect_issue_lifecycle, enrich_app_data_with_lifecycle

ALL_VERSIONS = ["3.9.2", "3.10.1", "3.10.2", "3.10.3", "3.11.0", "3.11.2"]
LATEST = "3.11.2"

# 後續版本有足夠當機事件量，代表「有人在用」，故「沒再出現」具說服力
BUSY_HEALTH = {
    "3.11.0": {"crash_events": 400},
    "3.11.2": {"crash_events": 9000},
}
# 後續版本幾乎沒有流量，無法區分「修好了」與「沒人用」
IDLE_HEALTH = {
    "3.11.0": {"crash_events": 1},
    "3.11.2": {"crash_events": 2},
}


class TestIssueFixStatus(unittest.TestCase):
    def test_still_present_when_seen_in_latest(self):
        r = detect_issue_fix_status(
            versions_seen=["3.10.1", LATEST],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
        )
        self.assertEqual(r["status"], "still_present")
        self.assertEqual(r["versions_since"], [])

    def test_likely_fixed_with_event_evidence_is_only_medium_confidence(self):
        """無 Sessions 時只能用 crash_events 當曝光量的間接代理，信心度不得標成 high。"""
        r = detect_issue_fix_status(
            versions_seen=["3.9.2", "3.10.1", "3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
        )
        self.assertEqual(r["status"], "likely_fixed")
        self.assertEqual(r["last_seen_version"], "3.10.3")
        self.assertEqual(r["versions_since"], ["3.11.0", LATEST])
        self.assertEqual(r["confidence"], "medium")
        self.assertIsNone(r["evidence_sessions"])

    def test_sessions_evidence_upgrades_confidence_to_high(self):
        """Sessions 是曝光量的直接證據，同一組版本歷史應得到更強的結論。"""
        health = {
            "3.11.0": {"crash_events": 400, "sessions_total": 5000},
            "3.11.2": {"crash_events": 9000, "sessions_total": 120000},
        }
        r = detect_issue_fix_status(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=health,
        )
        self.assertEqual(r["status"], "likely_fixed")
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(r["evidence_sessions"], 125000)

    def test_unproven_when_later_versions_have_no_traffic(self):
        """後續版本沒有流量時必須誠實說不知道，不能報成已修復。"""
        r = detect_issue_fix_status(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=IDLE_HEALTH,
        )
        self.assertEqual(r["status"], "unproven")
        self.assertEqual(r["confidence"], "low")

    def test_unproven_when_no_later_version_exists(self):
        r = detect_issue_fix_status(
            versions_seen=[LATEST],
            all_known_versions=[LATEST],
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
        )
        # 出現在最新版 -> still_present 優先於「無後續版本」
        self.assertEqual(r["status"], "still_present")

    def test_complements_lifecycle_resolved_blind_spot(self):
        """核心：lifecycle 判不出來的舊問題，fix_status 必須答得出來。

        issue 最後出現在 3.10.3，未出現在緊鄰最新版的前一版 3.11.0，
        因此 detect_issue_lifecycle 不會（也不該）把它算成 3.11.2 的 resolved；
        但使用者要問的「這個問題還在不在」有明確答案。兩者若哪天給出同樣結論，
        代表其中一個的契約被改壞了。
        """
        versions_seen = ["3.9.2", "3.10.1", "3.10.2", "3.10.3"]
        lc = detect_issue_lifecycle(
            issue_id="dummy",
            historical_versions=versions_seen,
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            sample_sufficient=True,
        )
        fx = detect_issue_fix_status(
            versions_seen=versions_seen,
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
        )
        self.assertNotEqual(lc["status"], "persistent")
        self.assertEqual(fx["status"], "likely_fixed")
        self.assertEqual(fx["last_seen_version"], "3.10.3")


class TestFixStatusFreshness(unittest.TestCase):
    """資料新鮮度護欄。

    典型情境：一份兩週前的 catalog 把某個 issue 判成「已消失」，但它在 catalog 截止之後
    又發作了。判定本身在資料截止當下是對的，錯在把兩週前的結論當成現況講。這組測試守住「資料越舊越不敢說已修復」這條規則；
    若有人移除 data_age_days 的處理，test_month_old_data_never_claims_fixed 必須紅。
    """

    def _call(self, age, health=BUSY_HEALTH):
        return detect_issue_fix_status(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=health,
            data_age_days=age,
        )

    def test_fresh_data_keeps_confidence(self):
        r = self._call(3)
        self.assertEqual(r["status"], "likely_fixed")
        self.assertEqual(r["confidence"], "medium")
        self.assertEqual(r["data_age_days"], 3)

    def test_week_old_data_downgrades_and_says_so(self):
        """偏舊但仍可用：結論保留，但語氣必須放軟，且理由要講出資料截止多久了。"""
        r = self._call(10)
        self.assertEqual(r["status"], "likely_fixed")
        self.assertEqual(r["confidence"], "low")
        self.assertIn("10 天前", r["reason"])

    def test_sessions_evidence_is_also_downgraded_when_stale(self):
        """即使有 sessions 這種強證據，舊資料也看不到之後的復發——新鮮度與證據強度無關。"""
        health = {
            "3.11.0": {"crash_events": 400, "sessions_total": 5000},
            "3.11.2": {"crash_events": 9000, "sessions_total": 120000},
        }
        self.assertEqual(self._call(3, health)["confidence"], "high")
        self.assertEqual(self._call(10, health)["confidence"], "medium")

    def test_month_old_data_never_claims_fixed(self):
        r = self._call(35)
        self.assertEqual(r["status"], "unproven")
        self.assertIn("35 天前", r["reason"])

    def test_data_as_of_is_carried_for_view_time_check(self):
        """data_age_days 在產生時算好就凍結了；儀表板可能數週後才被打開，必須能用截止時間重算。

        典型情境：產出時資料才 3 天舊，兩週後打開時已經 17 天。
        若結果沒帶 data_as_of，檢視端只能沿用凍結的 3 天，護欄形同虛設。
        """
        r = detect_issue_fix_status(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
            data_age_days=3,
            data_as_of="2026-09-11T23:59:59Z",
        )
        self.assertEqual(r["data_as_of"], "2026-09-11T23:59:59Z")

    def test_still_present_is_not_affected_by_age(self):
        """「看到它發生」是已發生的事實，資料舊不會讓它變假；護欄只約束「已修復」這種否定性結論。"""
        r = detect_issue_fix_status(
            versions_seen=["3.10.3", LATEST],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=BUSY_HEALTH,
            data_age_days=90,
        )
        self.assertEqual(r["status"], "still_present")


class TestFixStatusOwnOccurrenceRate(unittest.TestCase):
    """以 issue 自身發生率判斷曝光量是否足夠。

    版本層級的 crash_events 可能全部來自其他 issue：一個吵鬧的 bug 灌滿 20 次，
    不代表同版本那些冷門 bug 被「觀察過」。真正要問的是——照這個 bug 自己過去的
    發生頻率，這段資料量裡它本應出現幾次？本應出現不到 3 次卻沒出現，什麼都證明不了。
    """

    def test_rare_issue_is_not_proven_fixed_by_other_issues_noise(self):
        """核心：證據全來自其他 issue 時不得判已修復。

        issue 在 3.10.3 只佔 1/1000；之後兩版合計 30 次當機（全是別的 bug）。
        照它自己的頻率本應只出現 0.03 次，沒看到很正常，不能說修好了。
        同一份輸入若不給分版本事件數，舊邏輯會因 30 >= 20 判成 likely_fixed——
        兩者結論必須不同，否則代表發生率檢查被繞過了。
        """
        health = {"3.10.3": {"crash_events": 1000}, "3.11.0": {"crash_events": 10}, "3.11.2": {"crash_events": 20}}
        kwargs = dict(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=health,
        )
        without_rate = detect_issue_fix_status(**kwargs)
        with_rate = detect_issue_fix_status(**kwargs, issue_version_events={"3.10.3": 1})

        self.assertEqual(without_rate["status"], "likely_fixed")
        self.assertEqual(with_rate["status"], "unproven")
        self.assertAlmostEqual(with_rate["expected_occurrences"], 0.03, places=3)

    def test_session_evidence_is_not_vetoed_by_crash_share(self):
        """多給一份資料不得讓結論變弱。

        新版 10 萬 sessions、只剩 2 次當機（當機大幅減少正是修好的樣子）。若用「佔當機比例」
        推算，本應出現次數只有 1 次，會把最強的 sessions 證據否決成 unproven；而同一份資料
        不給分版本次數時卻是 likely_fixed/high。有 sessions 時必須改用每 session 發生率。
        """
        kwargs = dict(
            versions_seen=["2.3.0"],
            all_known_versions=["2.3.0", "2.4.0"],
            latest_version="2.4.0",
            version_health_map={
                "2.3.0": {"crash_events": 10, "sessions_total": 20000},
                "2.4.0": {"crash_events": 2, "sessions_total": 100000},
            },
        )
        without = detect_issue_fix_status(**kwargs)
        with_rate = detect_issue_fix_status(**kwargs, issue_version_events={"2.3.0": 5})

        self.assertEqual((without["status"], without["confidence"]), ("likely_fixed", "high"))
        self.assertEqual((with_rate["status"], with_rate["confidence"]), ("likely_fixed", "high"))
        # 5 / 20000 sessions × 100000 sessions = 25 次
        self.assertAlmostEqual(with_rate["expected_occurrences"], 25.0)

    def test_rare_issue_per_session_rate_stays_unproven(self):
        """sessions 很多也不等於照得出冷門 bug：每百萬 sessions 才 1 次的 bug，10 萬 sessions 本應只出現 0.1 次。"""
        r = detect_issue_fix_status(
            versions_seen=["2.3.0"],
            all_known_versions=["2.3.0", "2.4.0"],
            latest_version="2.4.0",
            version_health_map={
                "2.3.0": {"crash_events": 50, "sessions_total": 1000000},
                "2.4.0": {"crash_events": 40, "sessions_total": 100000},
            },
            issue_version_events={"2.3.0": 1},
        )
        self.assertEqual(r["status"], "unproven")
        self.assertAlmostEqual(r["expected_occurrences"], 0.1)

    def test_direct_session_evidence_used_when_last_version_lacks_sessions(self):
        """最後出現版本沒有 sessions 無法換算發生率時，後續足量的 sessions 本身就是直接證據，不退回較弱的當機佔比。"""
        r = detect_issue_fix_status(
            versions_seen=["2.3.0"],
            all_known_versions=["2.3.0", "2.4.0"],
            latest_version="2.4.0",
            version_health_map={
                "2.3.0": {"crash_events": 10},
                "2.4.0": {"crash_events": 2, "sessions_total": 100000},
            },
            issue_version_events={"2.3.0": 5},
        )
        self.assertEqual((r["status"], r["confidence"]), ("likely_fixed", "high"))

    def test_frequent_issue_can_still_be_proven_fixed(self):
        """發生率檢查不是一律變保守：本來很吵的 bug，後續版本有量卻完全沒出現，仍應判定消失。"""
        health = {"3.10.3": {"crash_events": 40}, "3.11.0": {"crash_events": 400}, "3.11.2": {"crash_events": 9000}}
        r = detect_issue_fix_status(
            versions_seen=["3.10.3"],
            all_known_versions=ALL_VERSIONS,
            latest_version=LATEST,
            version_health_map=health,
            issue_version_events={"3.10.3": 30},
        )
        self.assertEqual(r["status"], "likely_fixed")
        self.assertGreater(r["expected_occurrences"], 3)

    def test_rate_is_capped_when_windows_disagree(self):
        """分版本事件數與版本總量取自不同視窗時佔比可能 >100%（例如 8/6），不得據此高估。"""
        health = {"2.3.0": {"crash_events": 6}, "2.4.0": {"crash_events": 62}}
        r = detect_issue_fix_status(
            versions_seen=["2.3.0"],
            all_known_versions=["2.3.0", "2.4.0"],
            latest_version="2.4.0",
            version_health_map=health,
            issue_version_events={"2.3.0": 8},
        )
        self.assertLessEqual(r["expected_occurrences"], 62)

    def test_noisy_issue_is_held_back_only_by_freshness(self):
        """回歸：舊版很吵的 issue，發生率檢查擋不住，只能靠資料新鮮度把結論壓下來。

        分版本事件數（8）大於版本總量（6），代表兩者視窗不一致、而且它在舊版佔了全部當機。
        以 15 天舊的資料來看，最多只能是低信心；放到一個月後則必須拒答。
        """
        kwargs = dict(
            versions_seen=["2.3.0"],
            all_known_versions=["2.3.0", "2.4.0"],
            latest_version="2.4.0",
            version_health_map={"2.3.0": {"crash_events": 6}, "2.4.0": {"crash_events": 62}},
            issue_version_events={"2.3.0": 8},
        )
        self.assertEqual(detect_issue_fix_status(**kwargs, data_age_days=15)["confidence"], "low")
        self.assertEqual(detect_issue_fix_status(**kwargs, data_age_days=31)["status"], "unproven")


class TestSampleSufficiencyUsesAppPolicy(unittest.TestCase):
    """apps.yaml 的 release_gate 門檻必須真的套用到 lifecycle 判定。

    先前 policy 只傳給 release_catalog，lifecycle 用函式預設值（事件數 20）——
    使用者為小型 App 調低的門檻形同虛設。這裡用同一份資料、兩種 policy 跑，
    結論必須不同；若有人再把 policy 解析搬回 release_catalog 之後，此測試會紅。
    """

    def _app_data(self):
        end = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # 動態日期：避免新鮮度判定隨時間讓測試失效
        return {
            "metadata": {"app_id": "small_app", "display_name": "Small", "firebase_project_id": "p", "platforms": ["ios"]},
            "period": {"days": 30, "start_time": "2026-01-01T00:00:00Z", "end_time": end},
            "sources": {"crashlytics_bq": {"status": "available"}},
            "kpi": {"crash_events": {"value": 15}, "affected_users": {"value": 5}},
            "daily_trend": [],
            "version_health": [
                {"version": "2.3.0", "platform": "ios", "status": "active", "crash_events": 12},
                {"version": "2.4.0", "platform": "ios", "status": "latest", "crash_events": 8},
            ],
            "distributions": {"platform": [], "device_models": [], "os_versions": [], "custom_keys": [],
                              "app_versions": [{"app_version": "2.3.0", "platform": "ios"},
                                               {"app_version": "2.4.0", "platform": "ios"}]},
            "top_issues": [{
                "issue_id": "old_bug", "platform": "ios", "title": "t",
                "first_seen_version": "2.3.0", "last_seen_version": "2.3.0",
                "version_distribution": [{"version": "2.3.0", "events": 12, "users": 4}],
            }],
            "periods": {},
        }

    def test_app_threshold_changes_lifecycle_verdict(self):
        default_policy = load_gate_policy(None)
        small_app_policy = load_gate_policy({"release_gate": {"min_version_events": 5}})

        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            strict = enrich_app_data_with_lifecycle(self._app_data(), app_name="small_app", out_dir=Path(d1), gate_policy=default_policy)
            lenient = enrich_app_data_with_lifecycle(self._app_data(), app_name="small_app", out_dir=Path(d2), gate_policy=small_app_policy)

        # 2.4.0 只有 8 次事件：預設門檻 20 不足以證明，App 自訂門檻 5 則足夠
        self.assertEqual(strict["top_issues"][0]["lifecycle"]["status"], "not_observed_latest")
        self.assertEqual(lenient["top_issues"][0]["lifecycle"]["status"], "resolved")

        # fix_status 必須跟 lifecycle 用同一套門檻：否則同一個 issue 會一邊說「已收斂」、
        # 一邊說「曝光量不足」。先前這個測試只檢查 lifecycle，因此沒抓到 fix_status 仍吃預設值。
        self.assertEqual(strict["top_issues"][0]["fix_status"]["status"], "unproven")
        self.assertEqual(lenient["top_issues"][0]["fix_status"]["status"], "likely_fixed")


class TestFixStatusFilterContract(unittest.TestCase):
    """問題列表的修復狀態篩選，對「尚未判定」的 issue 不得捏造狀態。

    本功能上線前產生的 bundle 已帶 lifecycle、沒有 fix_status，而 renderer 刻意不回填它。
    若篩選把缺值當成 still_present，「仍在發生」會列出全部舊 issue、「無法判定」一筆都沒有——
    等於替從未判定過的 issue 下了結論。
    """

    def test_missing_fix_status_is_not_defaulted_to_still_present(self):
        from crash_trend.dashboard.issues import get_issues_js

        js = get_issues_js()
        self.assertIn('filterFix !== "ALL" && iss.fix_status?.status !== filterFix', js)
        self.assertNotIn('fix_status?.status || "still_present"', js)


if __name__ == "__main__":
    unittest.main()

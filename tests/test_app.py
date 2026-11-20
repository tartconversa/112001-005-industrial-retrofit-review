import io
import json
import tempfile
import unittest
from pathlib import Path

from transitionsvc.app import AppError, Service
from transitionsvc.store import Store


SAMPLE_WORKFORCE = """record_id,person_token,employer_id,period,role_code,employment_status,group_code
W01,P-17,F-A,2026-Q1,press_operator,employed,G1
W02,P-17,TRAIN-4,2026-Q1,process_tech,training,G1
W03,P-42,F-B,2026-Q1,maintenance,at_risk,G2
W04,P-42,F-B,2026-Q2,maintenance,separated,G2
W05,P-77,F-A,2026-Q2,press_operator,at_risk,G2
"""

SAMPLE_COURSES = """course_id,provider_id,skill_code,period,seats,fee_per_seat,employer_job_code,planned_openings,commitment_status
C10,TR-A,process_tech,2026-Q2,2,1250,process_tech,12,planned
C11,TR-B,solar_maintenance,2026-Q2,1,2100,solar_maintenance,6,confirmed
C12,TR-A,process_tech,2026-Q3,1,1300,process_tech,5,withdrawn
C13,TR-B,process_tech,2026-Q2,5,1500,process_tech,4,pending
"""

SAMPLE_OUTCOMES = """outcome_id,person_token,course_id,event_date,event_type,job_code,employer_id,retained_90d
O01,P-17,C10,2026-05-20,completed,process_tech,F-A,
O02,P-17,C10,2026-06-03,started,process_tech,F-A,
O03,P-42,C11,2026-06-10,offer,solar_maintenance,F-C,
O04,P-42,C11,2026-06-20,offer,solar_maintenance,F-C,
O05,P-99,C10,2026-06-25,started,process_tech,F-A,
"""


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workforce.csv").write_text(SAMPLE_WORKFORCE, encoding="utf-8")
        (self.root / "courses_commitments.csv").write_text(SAMPLE_COURSES, encoding="utf-8")
        (self.root / "outcomes.csv").write_text(SAMPLE_OUTCOMES, encoding="utf-8")
        self.store = Store(self.root)
        self.service = Service(self.store)


class SeatAllocationTests(ServiceFixture):
    def test_capacity_never_exceeded_and_reasons_recorded(self):
        result = self.service.allocate_seats("C10", [
            ("P-17", "G1"), ("P-42", "G2"), ("P-77", "G2"), ("P-99", None),
        ])
        self.assertEqual(result["allocated"], ["P-17", "P-42"])
        self.assertEqual(result["waitlisted"], ["P-77", "P-99"])
        self.assertEqual(result["remaining_seats"], 0)
        alloc = self.store.course_seat_allocation("C10")
        self.assertEqual(len(alloc), 2)
        for row in alloc:
            self.assertTrue(row["reason"].strip())
            self.assertEqual(row["status"], "allocated")

    def test_withdrawn_course_cannot_accept_allocations(self):
        with self.assertRaises(AppError):
            self.service.allocate_seats("C12", [("P-17", "G1")])

    def test_capacity_is_per_course_not_global(self):
        first = self.service.allocate_seats("C10", [("P-17", "G1"), ("P-42", "G2")])
        self.assertEqual(first["remaining_seats"], 0)
        second = self.service.allocate_seats("C11", [("P-77", "G2")])
        self.assertEqual(second["allocated"], ["P-77"])
        self.assertEqual(second["remaining_seats"], 0)
        self.assertEqual(self.service.explain_allocation("C10")["allocated_count"], 2)
        self.assertEqual(self.service.explain_allocation("C11")["allocated_count"], 1)

    def test_duplicate_allocation_is_idempotent_not_double_counted(self):
        self.service.allocate_seats("C10", [("P-17", "G1")])
        again = self.service.allocate_seats("C10", [("P-17", "G1")])
        self.assertEqual(again["allocated"], ["P-17"])
        self.assertEqual(len(self.store.course_seat_allocation("C10")), 1)

    def test_confirmed_alias_does_not_get_a_second_seat(self):
        self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        clash = self.service.store.unresolved_clashes()[0]
        self.service.resolve_identity(clash["clash_id"], confirmed_token="P-17")
        first = self.service.allocate_seats("C10", [("P-17", "G1")])
        second = self.service.allocate_seats("C10", [("P-88", "G1")])
        self.assertEqual(first["allocated"], ["P-17"])
        self.assertEqual(second["allocated"], ["P-88"])
        self.assertEqual(second["remaining_seats"], 1)
        self.assertEqual(len(self.store.course_seat_allocation("C10")), 1)

    def test_deallocation_frees_seat_but_keeps_audit(self):
        self.service.allocate_seats("C10", [("P-17", "G1"), ("P-42", "G2")])
        result = self.service.release_seat("C10", "P-17", "参与人已转入其他课程", reason="参与人已转入其他课程")
        self.assertEqual(result["remaining_seats"], 1)
        self.service.allocate_seats("C10", [("P-77", "G2")])
        history = self.store.course_seat_allocation("C10", include_released=True)
        released = [r for r in history if r["status"] == "released"]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["reason"], "参与人已转入其他课程")

    def test_allocation_blocked_when_person_identity_unresolved(self):
        self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="同名不同人，待核")
        with self.assertRaises(AppError):
            self.service.allocate_seats("C10", [("P-17", "G1")])


class OutcomeTests(ServiceFixture):
    def test_planned_offer_started_retained_are_separate(self):
        r = self.service.employment_funnel()
        self.assertEqual(r["planned_openings"], 27)  # 12 + 6 + 4 + 5
        self.assertEqual(r["offers"], 1)  # P-42 / F-C counted once despite two offer rows
        self.assertEqual(r["started"], 2)
        self.assertEqual(r["retained_90d"], 0)

    def test_placement_counts_are_per_person_not_per_record(self):
        # Two source rows already say P-17 and P-99 started C10; a third row for
        # P-17 must not become a third placement.
        self.service.record_outcome(person_token="P-17", course_id="C10",
                                    event_date="2026-07-02", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        funnel = self.service.employment_funnel()
        self.assertEqual(funnel["started"], 2)
        self.assertEqual(funnel["by_course"]["C10"]["started"], 2)

    def test_commitment_counters_cannot_be_reported_as_starts(self):
        with self.assertRaises(AppError):
            self.service.record_outcome(person_token="P-17", course_id="C10",
                                        event_date="2026-07-01", event_type="started",
                                        job_code="process_tech", employer_id="F-A",
                                        as_actual_from_commitment=True)

    def test_duplicate_start_events_do_not_inflate_placements(self):
        self.service.record_outcome(person_token="P-17", course_id="C10",
                                    event_date="2026-07-02", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        self.service.record_outcome(person_token="P-17", course_id="C10",
                                    event_date="2026-07-05", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        funnel = self.service.employment_funnel()
        self.assertEqual(funnel["started"], 2)  # P-17 counted once, P-99 once

    def test_retention_recorded_alongside_start_still_counted(self):
        self.service.record_outcome(person_token="P-17", course_id="C10",
                                    event_date="2026-07-02", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        self.service.record_outcome(person_token="P-17", course_id="C10",
                                    event_date="2026-10-01", event_type="retained_90d",
                                    job_code="process_tech", employer_id="F-A")
        funnel = self.service.employment_funnel()
        self.assertEqual(funnel["started"], 2)
        self.assertEqual(funnel["retained_90d"], 1)
        self.assertEqual(funnel["by_course"]["C10"]["retained_90d"], 1)

    def test_retention_flag_on_start_row_is_not_dropped(self):
        self.service.record_outcome(person_token="P-99", course_id="C10",
                                    event_date="2026-07-10", event_type="retained_90d",
                                    job_code="process_tech", employer_id="F-A")
        self.assertEqual(self.service.employment_funnel()["retained_90d"], 1)

    def test_retained_requires_start(self):
        with self.assertRaises(AppError):
            self.service.record_outcome(person_token="P-88", course_id="C10",
                                        event_date="2026-07-03", event_type="retained_90d",
                                        job_code="process_tech", employer_id="F-A")

    def test_correction_applies_to_recorded_events_too(self):
        event = self.service.record_outcome(person_token="P-77", course_id="C10",
                                           event_date="2026-07-01", event_type="offer",
                                           job_code="process_tech", employer_id="F-A")
        self.assertEqual(self.service.employment_funnel()["offers"], 2)
        self.service.correct_outcome(event["outcome_id"], event_type="declined",
                                     reason="候选人拒绝录用")
        self.assertEqual(self.service.employment_funnel()["offers"], 1)

    def test_correction_preserves_original_and_unknown_stays_unknown(self):
        correction = self.service.correct_outcome(
            "O03", event_type="withdrawn", reason="企业撤回录用", date_unknown=True,
        )
        self.assertEqual(correction["previous_event_type"], "offer")
        self.assertEqual(correction["current_event_type"], "withdrawn")
        self.assertIsNone(correction["new_event_date"])
        row = [r for r in self.store.outcomes() if r["outcome_id"] == "O03"][0]
        self.assertTrue(row["corrected"])
        self.assertEqual(row["original_event_type"], "offer")

    def test_chained_correction_does_not_resurrect_doubted_date(self):
        self.service.correct_outcome("O03", event_type="withdrawn", reason="企业撤回录用",
                                     date_unknown=True)
        second = self.service.correct_outcome("O03", event_type="declined",
                                              reason="候选人确认拒绝")
        self.assertIsNone(second["new_event_date"])
        row = [r for r in self.store.outcomes() if r["outcome_id"] == "O03"][0]
        self.assertEqual(row["event_type"], "declined")
        self.assertEqual(row["event_date"], None)


class SpendAndReversalTests(ServiceFixture):
    def test_seat_spend_is_committed_on_allocation_and_never_erased(self):
        self.service.allocate_seats("C10", [("P-17", "G1"), ("P-42", "G2")])
        spend = self.service.spend_summary()
        self.assertEqual(spend["committed"], 2 * 1250)
        self.service.release_seat("C10", "P-17", "转入其他课程", reason="转入其他课程")
        spend_after = self.service.spend_summary()
        self.assertEqual(spend_after["committed"], 2 * 1250)
        self.assertEqual(spend_after["outstanding"], 1250)

    def test_inactive_commitment_does_not_add_planned_openings(self):
        funnel = self.service.employment_funnel()
        self.assertEqual(funnel["planned_openings"], 27)
        self.assertEqual(funnel["active_planned_openings"], 22)  # withdrawn C12 excluded

    def test_employer_fulfillment_feedback_updates_region(self):
        self.service.record_employer_fulfillment(
            course_id="C10", employer_id="F-A", job_code="process_tech",
            fulfilled_openings=8, report_date="2026-08-01",
            note="首批生产线按时到岗",
        )
        regional = self.service.regional_summary("2026-Q2")
        self.assertEqual(regional["fulfilled_openings"], 8)
        self.assertEqual(regional["planned_openings"], 22)
        self.assertIn("planned_openings", regional)
        self.assertNotEqual(regional["fulfilled_openings"], regional["planned_openings"])


class GapAndGroupTests(ServiceFixture):
    def test_skill_gap_compares_capacity_against_openings(self):
        gap = self.service.skill_gap()
        process = [g for g in gap if g["skill_code"] == "process_tech"][0]
        # capacity 2 + 5, active planned openings 12 + 4
        self.assertEqual(process["active_seats"], 7)
        self.assertEqual(process["active_planned_openings"], 16)
        self.assertEqual(process["seat_gap"], -9)

    def test_group_view_lists_affected_and_training_status(self):
        view = self.service.group_view()
        g2 = [g for g in view if g["group_code"] == "G2"][0]
        self.assertGreaterEqual(g2["affected_persons"], 2)
        self.assertEqual(g2["in_training"], 0)

    def test_person_view_never_merges_unknown_identity(self):
        self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        view = self.service.person_view("P-17")
        self.assertEqual(view["identity_status"], "unresolved")
        self.assertEqual(sorted(view["tokens"]), ["P-17", "P-88"])

    def test_person_view_resolved_after_confirmation(self):
        pending = self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        self.service.resolve_identity(pending["clash_id"], confirmed_token="P-17")
        view = self.service.person_view("P-17")
        self.assertEqual(view["identity_status"], "resolved")
        self.assertEqual(view["tokens"], ["P-17"])


class ExplainAndImpactTests(ServiceFixture):
    def test_allocation_explanation_traces_each_decision(self):
        self.service.allocate_seats("C10", [("P-17", "G1"), ("P-42", "G2"), ("P-77", "G2")])
        explanation = self.service.explain_allocation("C10")
        self.assertEqual(explanation["course_id"], "C10")
        self.assertEqual(explanation["seats"], 2)
        self.assertEqual(explanation["allocated_count"], 2)
        self.assertEqual(explanation["waitlisted_count"], 1)
        for decision in explanation["decisions"]:
            self.assertIn(decision["decision"], ("allocated", "waitlisted"))
            self.assertTrue(decision["reason"])

    def test_impact_preview_reports_unexecuted_support_changes(self):
        self.service.allocate_seats("C10", [("P-17", "G1")])
        impact = self.service.impact_preview("C11")
        self.assertIn("unexecuted_support", impact)
        self.assertIn("exposure", impact)

    def test_informed_support_is_not_counted_as_executed(self):
        self.service.record_support(person_token="P-42", course_id="C11",
                                    action_type="travel_voucher", status="informed")
        self.service.record_support(person_token="P-42", course_id="C11",
                                    action_type="childcare", status="approved")
        status = self.service.support_status("P-42")
        self.assertEqual(len(status["informed_only"]), 1)
        self.assertEqual(len(status["awaiting_execution"]), 2)
        self.assertFalse(any(a["executed"] for a in status["actions"]))

    def test_executed_support_drops_out_of_unexecuted_exposure(self):
        self.service.record_support(person_token="P-42", course_id="C11",
                                    action_type="travel_voucher", status="approved")
        before = self.service.impact_preview("C11")["exposure"]["support_actions_not_executed"]
        self.assertEqual(before, 1)
        self.service.record_support(person_token="P-42", course_id="C11",
                                    action_type="travel_voucher", status="executed")
        after = self.service.impact_preview("C11")["exposure"]["support_actions_not_executed"]
        self.assertEqual(after, 0)
        status = self.service.support_status("P-42")
        action = status["actions"][0]
        self.assertTrue(action["executed"])
        self.assertEqual(
            [entry["status"] for entry in action["history"]], ["approved", "executed"]
        )

    def test_support_status_merges_confirmed_identity(self):
        self.service.record_support(person_token="P-88", course_id="C11",
                                    action_type="travel_voucher", status="informed")
        clash = self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        # While unresolved, the alias's support is not attributed to P-17.
        self.assertEqual(self.service.support_status("P-17")["actions"], [])
        self.service.resolve_identity(clash["clash_id"], confirmed_token="P-17")
        merged = self.service.support_status("P-17")
        self.assertEqual(len(merged["actions"]), 1)
        self.assertEqual(merged["actions"][0]["action_type"], "travel_voucher")
        self.assertEqual(len(merged["awaiting_execution"]), 1)


class JobEquivalenceTests(ServiceFixture):
    def test_pending_alias_is_not_used_for_skill_gap(self):
        self.service.register_job_alias("process_operator", "process_tech", confirmed=False)
        self.service.register_job_alias("solar_tech", "solar_maintenance", confirmed=True)
        skills = {entry["skill_code"] for entry in self.service.skill_gap()}
        self.assertNotIn("solar_tech", skills)
        pending = self.service.job_equivalence()["pending"]
        self.assertEqual([row["alias_code"] for row in pending], ["process_operator"])

    def test_confirmed_alias_merges_skill_capacity(self):
        self.service.register_job_alias("process_etech", "process_tech", confirmed=True)
        skills = {entry["skill_code"] for entry in self.service.skill_gap()}
        self.assertIn("process_tech", skills)
        self.assertNotIn("process_etech", skills)

    def add_course(self):
        (self.root / "courses_commitments.csv").write_text(
            SAMPLE_COURSES
            + "C20,TR-A,process_tech,2026-Q2,4,900,production_associate,3,planned\n",
            encoding="utf-8",
        )
        self.store.reload()

    def test_course_skill_and_employer_job_links_are_reported(self):
        self.add_course()
        links = {item["course_id"]: item for item in self.service.unmapped_jobs()}
        self.assertEqual(links["C10"]["status"], "matched")
        self.assertEqual(links["C20"]["status"], "unresolved")

    def test_alias_makes_employer_job_link_matched(self):
        self.add_course()
        self.service.register_job_alias("production_associate", "process_tech", confirmed=True)
        links = {item["course_id"]: item for item in self.service.unmapped_jobs()}
        self.assertEqual(links["C20"]["status"], "matched")


class IdentityMergeTests(ServiceFixture):
    def test_unresolved_tokens_count_separately_until_confirmed(self):
        # Fixture already has P-17 and P-99 started C10; P-88 is the same human
        # as P-17 but unconfirmed, so it is honestly counted as a third record.
        self.service.record_outcome(person_token="P-88", course_id="C10",
                                    event_date="2026-07-10", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        self.assertEqual(self.service.employment_funnel()["started"], 3)

    def test_confirmed_identity_is_counted_once_across_sources(self):
        self.service.record_outcome(person_token="P-88", course_id="C10",
                                    event_date="2026-07-10", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        clash = self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        self.assertEqual(self.service.employment_funnel()["started"], 3)
        self.service.resolve_identity(clash["clash_id"], confirmed_token="P-17")
        self.assertEqual(self.service.employment_funnel()["started"], 2)

    def test_unresolved_alias_cannot_be_counted_as_new_placement(self):
        clash = self.service.register_identity_clash("P-17", ["P-17", "P-88"], note="待核")
        self.service.record_outcome(person_token="P-88", course_id="C10",
                                    event_date="2026-07-10", event_type="started",
                                    job_code="process_tech", employer_id="F-A")
        funnel = self.service.employment_funnel()
        self.assertEqual(funnel["started"], 3)  # counted separately while unresolved
        status = self.service.person_view("P-88")
        self.assertEqual(status["identity_status"], "unresolved")
        self.assertIn("P-17", status["tokens"])
        # cannot claim the second token as a distinct new placement budget line
        self.service.resolve_identity(clash["clash_id"], confirmed_token="P-17")
        self.assertEqual(self.service.employment_funnel()["started"], 2)


class AppEntryTests(ServiceFixture):
    def test_cli_round_trip(self):
        from transitionsvc.cli import run

        out = io.StringIO()
        code = run(["summary", "--data", str(self.root)], stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("planned_openings", out.getvalue())

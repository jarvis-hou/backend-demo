import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rate(rows):
    return f"{sum(r['outcome'] == '1' for r in rows)}/{len(rows)} ({sum(r['outcome'] == '1' for r in rows)/len(rows):.1%})" if rows else "n/a"


def wilson_interval(successes, total, z=1.96):
    if not total:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def failure_summary(group):
    failures = sum(r["outcome"] == "0" for r in group)
    low, high = wilson_interval(failures, len(group))
    return f"{failures}/{len(group)} ({failures/len(group):.1%}; 95% CI {low:.1%}-{high:.1%})" if group else "n/a"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", default=Path(__file__).with_name("dispatch_logs.csv"), type=Path)
    args = parser.parse_args()
    with args.csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    expected = {"dispatch_id", "dispatched_at", "schedule_synced_at", "region", "service_type", "available_slots", "daily_completed_orders", "is_fulfilled"}
    if not rows or not expected.issubset(rows[0]):
        raise SystemExit("CSV is empty or missing required columns")

    outcomes = Counter(r["is_fulfilled"] or "blank" for r in rows)
    featured = []
    for raw in rows:
        age_minutes = (parse_time(raw["dispatched_at"]) - parse_time(raw["schedule_synced_at"])).total_seconds() / 60
        r = dict(raw)
        r["outcome"] = raw["is_fulfilled"]
        r["age_minutes"] = age_minutes
        r["slots"] = int(raw["available_slots"])
        r["completed"] = int(raw["daily_completed_orders"])
        featured.append(r)
    labeled = [r for r in featured if r["outcome"] in {"0", "1"}]
    blanks = [r for r in featured if r["outcome"] not in {"0", "1"}]

    def bucket(v, bounds, labels):
        for bound, label in zip(bounds, labels):
            if v < bound:
                return label
        return labels[-1]

    freshness = defaultdict(list)
    slot_groups = defaultdict(list)
    workload = defaultdict(list)
    for r in labeled:
        freshness[bucket(r["age_minutes"], [60, 240, 720], ["<1h", "1-4h", "4-12h", ">=12h"])].append(r)
        slot_groups[bucket(r["slots"], [3, 6, 11], ["0-2", "3-5", "6-10", "11+"])].append(r)
        workload[bucket(r["completed"], [40, 60, 70], ["<40", "40-59", "60-69", "70+"])].append(r)

    print(f"Rows: {len(rows)}; labeled: {len(labeled)}; outcomes: {dict(outcomes)}")
    print(f"Observed success rate: {rate(labeled)}")
    for title, groups in (("Schedule age", freshness), ("Available slots", slot_groups), ("Completed orders", workload)):
        print(f"\n{title} (successes / labeled, success rate)")
        for key, group in groups.items():
            print(f"  {key:>6}: {rate(group)}; failure {failure_summary(group)}")

    print("\nFailure rate by region (outcomes 0/1 only; Wilson 95% CI)")
    by_region = defaultdict(list)
    for r in labeled:
        by_region[r["region"]].append(r)
    for key, group in sorted(by_region.items()):
        print(f"  {key}: n={len(group)}, {failure_summary(group)}")

    print("\nFailure rate by service type (outcomes 0/1 only; Wilson 95% CI)")
    by_service = defaultdict(list)
    for r in labeled:
        by_service[r["service_type"]].append(r)
    for key, group in sorted(by_service.items()):
        print(f"  {key}: n={len(group)}, {failure_summary(group)}")

    def print_blank_rates(title, key_fn):
        groups = defaultdict(list)
        for r in featured:
            groups[key_fn(r)].append(r)
        print(f"\nBlank outcome rate by {title} (Wilson 95% CI)")
        for key, group in sorted(groups.items()):
            count = sum(not r["outcome"] for r in group)
            low, high = wilson_interval(count, len(group))
            print(f"  {key}: {count}/{len(group)} ({count/len(group):.1%}; 95% CI {low:.1%}-{high:.1%})")

    print_blank_rates("region", lambda r: r["region"])
    print_blank_rates("service type", lambda r: r["service_type"])
    print_blank_rates("schedule age", lambda r: bucket(r["age_minutes"], [60, 240, 720], ["<1h", "1-4h", "4-12h", ">=12h"]))
    print_blank_rates("available slots", lambda r: bucket(r["slots"], [3, 6, 11], ["0-2", "3-5", "6-10", "11+"]))

    failures = [r for r in labeled if r["outcome"] == "0"]
    eligible_total = len(rows) - len(blanks)
    policies = (
        ("age >=1h OR slots <=2", lambda r: r["age_minutes"] >= 60 or r["slots"] <= 2),
        ("age >=1h OR slots <=2 OR completed >=70", lambda r: r["age_minutes"] >= 60 or r["slots"] <= 2 or r["completed"] >= 70),
    )
    print("\nRevalidation queue hypotheses (outcomes 0/1 only)")
    for name, condition in policies:
        flagged = [r for r in labeled if condition(r)]
        flagged_all = [r for r in featured if condition(r)]
        flagged_failures = [r for r in flagged if r["outcome"] == "0"]
        # $75 is the stated cost of one conflict; $10 per manual revalidation.
        gross_avoided_ceiling = len(flagged_failures) * 75
        review_cost = len(flagged_all) * 10
        print(f"  {name}")
        print(f"    labeled queue: {len(flagged)}/{len(labeled)} ({len(flagged)/len(labeled):.1%}); all-row queue: {len(flagged_all)}/{len(featured)} ({len(flagged_all)/len(featured):.1%}); failures in labeled queue: {len(flagged_failures)}/{len(flagged)} ({len(flagged_failures)/len(flagged):.1%})")
        print(f"    labeled failures captured: {len(flagged_failures)}/{len(failures)} ({len(flagged_failures)/len(failures):.1%})")
        print(f"    gross ceiling if every captured failure were prevented: ${gross_avoided_ceiling}; review cost: ${review_cost}; net ceiling: ${gross_avoided_ceiling-review_cost}")

    baseline_condition = policies[0][1]
    baseline_all = [r for r in featured if baseline_condition(r)]
    baseline_other = [r for r in featured if not baseline_condition(r)]
    baseline_labeled = [r for r in baseline_all if r["outcome"] in {"0", "1"}]
    baseline_failures = sum(r["outcome"] == "0" for r in baseline_labeled)
    blank_in_queue = sum(not r["outcome"] for r in baseline_all)
    blank_outside = sum(not r["outcome"] for r in baseline_other)
    print("\nMissing outcomes and the baseline queue (age >=1h OR slots <=2)")
    print(f"  all-row queue size observable from features: {len(baseline_all)}/{len(featured)} ({len(baseline_all)/len(featured):.1%})")
    print(f"  blank inside queue: {blank_in_queue}/{len(baseline_all)} ({blank_in_queue/len(baseline_all):.1%}); outside: {blank_outside}/{len(baseline_other)} ({blank_outside/len(baseline_other):.1%})")
    print(f"  labeled failures in queue: {baseline_failures}; blank outcomes excluded from economics: {blank_in_queue}")
    outside_labeled = [r for r in baseline_other if r["outcome"] in {"0", "1"}]
    outside_failures = sum(r["outcome"] == "0" for r in outside_labeled)
    queue_rate_bounds = (baseline_failures / len(baseline_all), (baseline_failures + blank_in_queue) / len(baseline_all))
    outside_rate_bounds = (outside_failures / len(baseline_other), (outside_failures + blank_outside) / len(baseline_other))
    overall_rate_bounds = (len(failures) / len(featured), (len(failures) + len(blanks)) / len(featured))
    print(f"  worst-case failure-rate bounds if blanks are all success vs all failure: overall={overall_rate_bounds[0]:.1%}-{overall_rate_bounds[1]:.1%}; queue={queue_rate_bounds[0]:.1%}-{queue_rate_bounds[1]:.1%}; outside={outside_rate_bounds[0]:.1%}-{outside_rate_bounds[1]:.1%}")

    review_cost = len(baseline_all) * 10
    maximum_avoided_loss = baseline_failures * 75
    break_even_share = review_cost / maximum_avoided_loss if maximum_avoided_loss else float("inf")
    print("\nBaseline economic sensitivity (effective share of flagged failures prevented)")
    print(f"  review cost on all {len(baseline_all)} triggered records (including {blank_in_queue} blanks): ${review_cost}; maximum modeled avoided loss from labeled failures: ${maximum_avoided_loss}; break-even effective share: {break_even_share:.1%}")
    for share in (0.0, 0.25, 0.50, break_even_share, 0.60, 0.75, 1.0):
        avoided = maximum_avoided_loss * share
        print(f"  effective prevention {share:.1%}: avoided loss=${avoided:.2f}; net=${avoided-review_cost:.2f}")
    minimum_whole_events = int(review_cost // 75) + 1
    print(f"  whole-event break-even: prevent at least {minimum_whole_events}/{baseline_failures} flagged failures ({minimum_whole_events/baseline_failures:.1%}); modeled net=${minimum_whole_events*75-review_cost}")

    print("\nExploratory threshold sweep (ranked by theoretical net ceiling; same data used to select and score thresholds)")
    sweep = []
    for age_cutoff in (15, 30, 60, 120):
        for slot_cutoff in (0, 1, 2, 3):
            flagged = [r for r in labeled if r["age_minutes"] >= age_cutoff or r["slots"] <= slot_cutoff]
            flagged_all = [r for r in featured if r["age_minutes"] >= age_cutoff or r["slots"] <= slot_cutoff]
            flagged_failures = sum(r["outcome"] == "0" for r in flagged)
            gross_ceiling = flagged_failures * 75
            review_cost = len(flagged_all) * 10
            sweep.append((gross_ceiling - review_cost, age_cutoff, slot_cutoff, len(flagged), len(flagged_all), flagged_failures, gross_ceiling, review_cost))
    for net, age_cutoff, slot_cutoff, queued, all_queued, captured, gross, cost in sorted(sweep, reverse=True):
        capture_rate = captured / len(failures) if failures else 0
        print(f"  age>={age_cutoff}m OR slots<={slot_cutoff}: labeled queue={queued}/{len(labeled)}; all-row queue={all_queued}/{len(featured)}; failures captured={captured}/{len(failures)} ({capture_rate:.1%}); net ceiling=${net} (gross=${gross}, review=${cost})")
    print(f"  blank outcomes excluded from failure-rate evaluation: {len(blanks)}")
    print(f"  unlabeled fraction of rows: {len(blanks)}/{len(rows)} ({len(blanks)/len(rows):.1%}); labeled share of all rows: {eligible_total}/{len(rows)}")


if __name__ == "__main__":
    main()

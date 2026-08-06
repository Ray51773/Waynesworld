"""Verify the scoring engine reproduces actual historical points, to the point.

Runs the engine over every 2025/26 player-gameweek in the vaastav dataset and reports
exact-match rate plus a breakdown of any disagreements. The spec asks for 200
player-gameweeks; running all 29,757 is the same work and a far stronger claim.

    .venv/Scripts/python scripts/verify_scoring.py
"""

from __future__ import annotations

import csv
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fpl.scoring import MatchStats, ScoringRules, normalise_position, score_match  # noqa: E402

CSV_PATH = Path(__file__).parent.parent / "data" / "raw_inspect" / "merged_gw_2025-26.csv"


def build_rules() -> ScoringRules:
    """2026/27 rules from the local store, stepped back to the 2025/26 ruleset."""
    from fpl.config import load_config
    from fpl.db import Database

    config = load_config()
    db = Database(config.db_path, read_only=True)
    try:
        return ScoringRules.from_db(db).for_season_2025_26()
    finally:
        db.close()


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing {CSV_PATH}; download merged_gw.csv for 2025-26 first")
        return 1

    rules = build_rules()
    print(f"ruleset: {rules.season} ({rules.source})")
    print(f"  goals: {rules.goals_scored}")
    print(f"  defcon thresholds: DEF {rules.constants.defcon_threshold_def}, "
          f"MID/FWD {rules.constants.defcon_threshold_mid_fwd} ({rules.constants.source})\n")

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    played = [r for r in rows if int(r["minutes"]) > 0]

    exact = 0
    mismatches = []
    diff_histogram: collections.Counter[int] = collections.Counter()
    by_position: collections.Counter[str] = collections.Counter()
    position_totals: collections.Counter[str] = collections.Counter()

    for row in played:
        position = normalise_position(row["position"])
        stats = MatchStats.from_mapping(row)
        computed = score_match(stats, position, rules).total
        actual = int(row["total_points"])
        position_totals[position] += 1

        if computed == actual:
            exact += 1
        else:
            diff_histogram[computed - actual] += 1
            by_position[position] += 1
            if len(mismatches) < 25:
                mismatches.append((row, computed, actual))

    total = len(played)
    print(f"player-gameweeks with minutes > 0: {total:,}")
    print(f"exact matches: {exact:,}  ({exact / total:.4%})")
    print(f"mismatches:    {total - exact:,}\n")

    if mismatches:
        print("difference histogram (computed - actual):")
        for delta, count in sorted(diff_histogram.items()):
            print(f"  {delta:+3d}  {count:6,}")
        print("\nmismatches by position:")
        for position, count in by_position.most_common():
            share = count / position_totals[position]
            print(f"  {position}  {count:6,} of {position_totals[position]:,}  ({share:.2%})")
        print("\nfirst mismatches:")
        for row, computed, actual in mismatches[:12]:
            breakdown = score_match(MatchStats.from_mapping(row),
                                    normalise_position(row["position"]), rules)
            print(f"  GW{row['GW']:>2} {row['name'][:22]:22s} {row['position']:3s} "
                  f"computed={computed:3d} actual={actual:3d}  "
                  f"mins={row['minutes']:>3s} {breakdown.as_dict()}")

    # Also confirm the reverse-engineered DEFCON formula on per-match data.
    dc_ok = dc_total = 0
    for row in played:
        position = normalise_position(row["position"])
        if position == "GKP":
            continue
        from fpl.scoring import defensive_actions
        computed = defensive_actions(
            position, int(row["tackles"]),
            int(row["clearances_blocks_interceptions"]), int(row["recoveries"]),
        )
        dc_total += 1
        if computed == int(row["defensive_contribution"]):
            dc_ok += 1
    print(f"\nDEFCON formula agreement on per-match data: {dc_ok:,}/{dc_total:,} "
          f"({dc_ok / dc_total:.4%})")

    return 0 if exact == total else 2


if __name__ == "__main__":
    raise SystemExit(main())

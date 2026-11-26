"""Award optimisation for the published tender convention.

The convention (``docs/exchange.md``) fixes both the hard constraints and the
lexicographic objective:

* packages are indivisible bundles of lot quantities;
* packages sharing an ``exclusion`` label are mutually exclusive (at most one
  of the group may be selected, across the whole tender);
* a lot may not exceed its maximum, and -- when partial fulfilment is allowed --
  a non-zero lot must reach its minimum;
* group budgets and the round budget are independent upper bounds;
* after feasibility, compare the progress vector in lot priority order, then
  total traded volume (descending), then total spend (ascending), then the
  lexicographic ordering of the selected bid numbers.

The solver is an exact depth-first branch-and-bound search.  It is exponential
in the worst case (multi-dimensional packing), so the node budget is explicit
and exceeding it is reported rather than silently returning a heuristic plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .errors import EvaluationTooComplex
from .models import Bid, Package, Tender

DEFAULT_MAX_NODES = 2_000_000
MAX_OPTIONS_PER_BID = 4096


@dataclass(frozen=True)
class Option:
    bid_id: str
    package_ids: tuple[str, ...]
    lots: dict[str, int]
    cents: int
    labels: frozenset[str]
    groups: frozenset[str]
    group_cents: dict[str, int]


def allocate_cents(
    lot_map: dict, lots: dict[str, int], cents: int
) -> dict[str, int]:
    """Split a package price across the budget groups its lots belong to.

    The published convention gives each lot a group and each group its own
    ceiling, but does not prescribe how a bundle that spans groups is priced.
    We use the quantity-proportional split with a deterministic largest
    remainder assignment (ties broken by group name).  The split always sums
    exactly to ``cents`` so the round budget is unaffected.
    """
    by_group: dict[str, int] = {}
    for lot_id, qty in lots.items():
        group = lot_map[lot_id].group
        by_group[group] = by_group.get(group, 0) + qty
    total = sum(by_group.values())
    if total <= 0 or not by_group:
        return {}
    if len(by_group) == 1:
        return {next(iter(by_group)): cents}
    exact = {group: cents * qty / total for group, qty in by_group.items()}
    floors = {group: int(value) for group, value in exact.items()}
    remainder = cents - sum(floors.values())
    order = sorted(by_group, key=lambda group: (-(exact[group] - floors[group]), group))
    for group in order[:remainder]:
        floors[group] += 1
    return {group: value for group, value in floors.items() if value}


def _option_from_packages(bid_id: str, packages: Sequence[Package], lot_map: dict) -> Option:
    lots: dict[str, int] = {}
    labels: set[str] = set()
    cents = 0
    for package in packages:
        cents += package.cents
        if package.exclusion:
            labels.add(package.exclusion)
        for lot_id, qty in package.lots.items():
            lots[lot_id] = lots.get(lot_id, 0) + qty
    groups = frozenset(lot_map[lot_id].group for lot_id in lots)
    return Option(
        bid_id=bid_id,
        package_ids=tuple(package.id for package in packages),
        lots=lots,
        cents=cents,
        labels=frozenset(labels),
        groups=groups,
        group_cents=allocate_cents(lot_map, lots, cents),
    )


def bid_options(bid: Bid, tender: Tender) -> list[Option]:
    """Enumerate every selectable package subset of one bid (including none)."""
    lot_map = tender.lot_map
    by_id = bid.package_map()
    empty = Option(bid.id, (), {}, 0, frozenset(), frozenset(), {})
    if bid.whole:
        labels = [p.exclusion for p in bid.packages if p.exclusion]
        if len(labels) != len(set(labels)):
            # The whole bid itself selects two packages of one exclusion group,
            # which the published convention forbids; it has no selectable
            # option, but it is still received and can be explained.
            return [empty]
        option = _option_from_packages(bid.id, list(bid.packages), lot_map)
        return [empty, option]
    label_groups: dict[str, list[Package]] = {}
    independent: list[Package] = []
    for package in bid.packages:
        if package.exclusion:
            label_groups.setdefault(package.exclusion, []).append(package)
        else:
            independent.append(package)
    subsets: list[tuple[Package, ...]] = [()]
    for packages in label_groups.values():
        subsets = subsets + [
            existing + (package,) for existing in subsets for package in packages
        ]
        if len(subsets) > MAX_OPTIONS_PER_BID:
            raise EvaluationTooComplex(
                "报价的可选方案组合过多，无法在限定规模内精确求解",
                bid=bid.id,
                limit=MAX_OPTIONS_PER_BID,
            )
    for package in independent:
        subsets = subsets + [existing + (package,) for existing in subsets]
        if len(subsets) > MAX_OPTIONS_PER_BID:
            raise EvaluationTooComplex(
                "报价的可选方案组合过多，无法在限定规模内精确求解",
                bid=bid.id,
                limit=MAX_OPTIONS_PER_BID,
            )
    options = [_option_from_packages(bid.id, subset, lot_map) for subset in subsets]
    # Stable, useful ordering: richer/high-volume options first so the search
    # finds a strong incumbent early; the empty option is left for last.
    options.sort(
        key=lambda opt: (
            -sum(opt.lots.values()),
            opt.cents,
            opt.package_ids,
        )
    )
    if options and options[-1].package_ids == ():
        options = options[:-1] + [options[-1]]
    return options


@dataclass
class Solution:
    volumes: dict[str, int]
    group_spend: dict[str, int]
    total_cents: int
    total_kwh: int
    choices: dict[str, tuple[str, ...]]
    selected_packages: dict[str, tuple[str, ...]]
    nodes: int
    lots: tuple[str, ...]

    def progress(self) -> tuple[int, ...]:
        return tuple(self.volumes.get(lot, 0) for lot in self.lots)

    def signature(self) -> tuple:
        return tuple(
            sorted(
                (bid_id, package_id)
                for bid_id, package_ids in self.selected_packages.items()
                for package_id in package_ids
            )
        )


def _is_better(
    volumes: tuple[int, ...],
    total_kwh: int,
    total_cents: int,
    signature: tuple,
    best: "Solution",
) -> bool:
    best_progress = best.progress()
    if volumes != best_progress:
        return volumes > best_progress
    if total_kwh != best.total_kwh:
        return total_kwh > best.total_kwh
    if total_cents != best.total_cents:
        return total_cents < best.total_cents
    # Last tie-break is the lexicographic order of the selected bid numbers
    # (then package ids), compared element by element rather than as strings.
    return signature < best.signature()


def solve(
    tender: Tender,
    bids: Iterable[Bid],
    *,
    initial_volumes: dict[str, int] | None = None,
    initial_cents: int = 0,
    initial_group_spend: dict[str, int] | None = None,
    blocked_labels: Iterable[str] = (),
    disabled_packages: Iterable[tuple[str, str]] = (),
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Solution:
    """Return the lexicographically best feasible simultaneously performable plan.

    ``initial_*`` and ``disabled_packages`` exist for standby rounds: scope that
    is already signed or formally offered is fixed and cannot be re-used.
    """
    lot_map = tender.lot_map
    lots = tuple(lot.id for lot in tender.ordered_lots())
    vols = {lot.id: 0 for lot in tender.lots}
    for lot_id, qty in (initial_volumes or {}).items():
        if lot_id not in lot_map:
            raise ValueError(f"unknown lot {lot_id}")
        vols[lot_id] = qty
    group_spend = {group: 0 for group in tender.limits if group != "total"}
    for group, amount in (initial_group_spend or {}).items():
        group_spend[group] = amount
    total_cents = initial_cents
    blocked = set(blocked_labels)
    disabled = set(disabled_packages)

    entries: list[tuple[Bid, list[Option]]] = []
    for bid in sorted(bids, key=lambda item: item.id):
        options = bid_options(bid, tender)
        if disabled:
            # Already-bound packages are unavailable; a bundle that contains
            # one of them is unavailable as a whole.
            options = [
                opt
                for opt in options
                if not any((bid.id, package_id) in disabled for package_id in opt.package_ids)
            ]
        entries.append((bid, options))

    # Optimistic per-lot headroom for the upper bound.
    remaining_max: list[dict[str, int]] = [dict() for _ in range(len(entries) + 1)]
    for index in range(len(entries) - 1, -1, -1):
        _, options = entries[index]
        lot_max: dict[str, int] = {}
        for opt in options:
            for lot_id, qty in opt.lots.items():
                if qty > lot_max.get(lot_id, 0):
                    lot_max[lot_id] = qty
        merged = dict(remaining_max[index + 1])
        for lot_id, qty in lot_max.items():
            merged[lot_id] = merged.get(lot_id, 0) + qty
        remaining_max[index] = merged

    best: Solution | None = None
    nodes = 0
    chosen: list[tuple[str, Option]] = []

    def feasible_volumes() -> bool:
        for lot in tender.lots:
            value = vols[lot.id]
            if not tender.partial:
                # Without partial fulfilment a lot is either awarded in its
                # full acceptable quantity or not awarded at all.
                if value not in (0, lot.max_kwh):
                    return False
                continue
            if value == 0:
                continue
            if value > lot.max_kwh:
                return False
            if value < lot.min_kwh:
                return False
        return True

    def consider() -> None:
        nonlocal best
        if not feasible_volumes():
            return
        if any(group_spend[g] > tender.limits[g] for g in group_spend):
            return
        if total_cents > tender.limits["total"]:
            return
        selected: dict[str, list[str]] = {}
        for bid_id, opt in chosen:
            if opt.package_ids:
                selected.setdefault(bid_id, []).extend(opt.package_ids)
        signature = tuple(
            sorted(
                (bid_id, package_id)
                for bid_id, ids in selected.items()
                for package_id in ids
            )
        )
        progress = tuple(vols[lot] for lot in lots)
        if best is not None and not _is_better(
            progress, sum(progress), total_cents, signature, best
        ):
            return
        best = Solution(
            volumes=dict(vols),
            group_spend=dict(group_spend),
            total_cents=total_cents,
            total_kwh=sum(progress),
            choices={bid_id: opt.package_ids for bid_id, opt in chosen},
            selected_packages={bid_id: tuple(ids) for bid_id, ids in selected.items()},
            nodes=nodes,
            lots=lots,
        )

    def upper_bound_hopeless(index: int) -> bool:
        if best is None:
            return False
        optimistic: list[int] = []
        for lot in tender.ordered_lots():
            headroom = max(0, lot.max_kwh - vols[lot.id])
            extra = min(headroom, remaining_max[index].get(lot.id, 0))
            optimistic.append(vols[lot.id] + extra)
        best_progress = list(best.progress())
        if tuple(optimistic) < tuple(best_progress):
            return True
        if tuple(optimistic) == tuple(best_progress):
            # Volumes can tie; only a strictly smaller spend could improve the
            # (already fixed) total volume.  Without a safe lower bound on the
            # remaining spend we keep searching rather than risk a wrong answer.
            if sum(optimistic) < best.total_kwh:
                return True
        return False

    def search(index: int, used_labels: frozenset[str]) -> None:
        nonlocal nodes, total_cents
        if nodes >= max_nodes:
            raise EvaluationTooComplex(
                "候选组合超出精确求解的节点上限；请缩小报价规模或拆分采购",
                tender=tender.id,
                max_nodes=max_nodes,
            )
        nodes += 1
        if upper_bound_hopeless(index):
            return
        if index == len(entries):
            consider()
            return
        bid, options = entries[index]
        for opt in options:
            if opt.labels & used_labels:
                continue
            if opt.labels and not opt.labels.isdisjoint(blocked):
                continue
            # capacity and budget pruning
            bad = False
            for lot_id, qty in opt.lots.items():
                if vols[lot_id] + qty > lot_map[lot_id].max_kwh:
                    bad = True
                    break
            if bad:
                continue
            if total_cents + opt.cents > tender.limits["total"]:
                continue
            old_groups: dict[str, int] = {}
            for group, amount in opt.group_cents.items():
                current = group_spend.get(group, 0)
                old_groups[group] = current
                group_spend[group] = current + amount
                if group_spend[group] > tender.limits[group]:
                    bad = True
                    break
            if bad:
                for group, value in old_groups.items():
                    group_spend[group] = value
                continue
            old_total = total_cents
            for lot_id, qty in opt.lots.items():
                vols[lot_id] += qty
            total_cents = old_total + opt.cents
            chosen.append((bid.id, opt))
            search(index + 1, used_labels | opt.labels)
            chosen.pop()
            total_cents = old_total
            for group, value in old_groups.items():
                group_spend[group] = value
            for lot_id, qty in opt.lots.items():
                vols[lot_id] -= qty

    search(0, frozenset())
    if best is None:
        # Empty selection is feasible unless initial reserved volumes are
        # themselves infeasible, which the caller prevents.
        best = Solution(
            volumes=dict(vols),
            group_spend=dict(group_spend),
            total_cents=total_cents,
            total_kwh=sum(vols.values()),
            choices={},
            selected_packages={},
            nodes=nodes,
            lots=lots,
        )
    best.nodes = nodes
    return best


@dataclass(frozen=True)
class AwardDraft:
    bid_id: str
    vendor: str
    revision: int
    kind: str  # WHOLE | INDEPENDENT
    package_ids: tuple[str, ...]
    packages: tuple[dict, ...]
    lots: dict[str, int]
    cents: int
    labels: tuple[str, ...]
    groups: dict[str, int]


def draft_awards(tender: Tender, bids: Iterable[Bid], solution: Solution) -> list[AwardDraft]:
    by_id = {bid.id: bid for bid in bids}
    lot_map = tender.lot_map
    drafts: list[AwardDraft] = []
    for bid_id in sorted(solution.selected_packages):
        package_ids = solution.selected_packages[bid_id]
        if not package_ids:
            continue
        bid = by_id[bid_id]
        package_map = bid.package_map()
        selected = [package_map[pid] for pid in package_ids]
        if bid.whole:
            lots: dict[str, int] = {}
            labels: set[str] = set()
            for package in selected:
                if package.exclusion:
                    labels.add(package.exclusion)
                for lot_id, qty in package.lots.items():
                    lots[lot_id] = lots.get(lot_id, 0) + qty
            cents = sum(p.cents for p in bid.packages)
            drafts.append(
                AwardDraft(
                    bid_id=bid.id,
                    vendor=bid.vendor,
                    revision=bid.revision,
                    kind="WHOLE",
                    package_ids=tuple(p.id for p in bid.packages),
                    packages=tuple(p.as_dict() for p in bid.packages),
                    lots=lots,
                    cents=cents,
                    labels=tuple(sorted(labels)),
                    groups=allocate_cents(lot_map, lots, cents),
                )
            )
        else:
            for package in selected:
                drafts.append(
                    AwardDraft(
                        bid_id=bid.id,
                        vendor=bid.vendor,
                        revision=bid.revision,
                        kind="INDEPENDENT",
                        package_ids=(package.id,),
                        packages=(package.as_dict(),),
                        lots=dict(package.lots),
                        cents=package.cents,
                        labels=(package.exclusion,) if package.exclusion else (),
                        groups=allocate_cents(lot_map, dict(package.lots), package.cents),
                    )
                )
    drafts.sort(key=lambda draft: (draft.vendor, draft.bid_id, draft.package_ids))
    return drafts


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------

REASON_TEXT = {
    "INELIGIBLE": "资格核查未通过，未进入本次评审",
    "DISQUALIFIED": "资格异议成立，已取消参与资格",
    "WITHDRAWN": "该版本已在关标前撤回",
    "LATE": "报价未在关标前送达",
    "SUPERSEDED": "该版本已被截止前更晚提交的版本取代",
    "EXPIRED": "报价有效期已届满",
    "EXCLUSION_CONFLICT": "与已选方案共享 exclusion，不能同时成立",
    "LOT_CAPACITY": "加入后超出该时段可接受最大电量",
    "BELOW_MINIMUM": "加入后该时段仍低于最低履约量，该时段不成交",
    "BUDGET_TOTAL": "加入后超出整轮预算上限",
    "BUDGET_GROUP": "加入后超出所属分组预算上限",
    "WHOLE_BID_NOT_SELECTED": "整包报价未被整组选中，包内不可拆分",
    "INTERNAL_EXCLUSION": "整包内方案共享 exclusion，整包在任何组合下都不能成立",
    "NOT_SELECTED": "在硬约束下可行，但按公告排序约定未进入最优组合",
}


def explain_unselected(
    tender: Tender,
    bids: Sequence[Bid],
    solution: Solution,
    *,
    ineligible: dict[str, str] | None = None,
) -> list[dict]:
    """Explain, per unselected package, why it is not in the awarded plan.

    The check is deliberately local ("what if this package were added to the
    winning plan?").  It distinguishes a concrete blocking constraint from the
    ordinary case where a feasible package simply lost on the published
    ordering.
    """
    ineligible = ineligible or {}
    selected = {
        (bid_id, package_id)
        for bid_id, package_ids in solution.selected_packages.items()
        for package_id in package_ids
    }
    selected_labels = set()
    selected_bid_ids = set(solution.selected_packages)
    for bid in bids:
        for package_id in solution.selected_packages.get(bid.id, ()):
            package = bid.package_map()[package_id]
            if package.exclusion:
                selected_labels.add(package.exclusion)
    rows: list[dict] = []
    for bid in sorted(bids, key=lambda item: item.id):
        if bid.id in ineligible:
            status = ineligible[bid.id]
            code = status if status in ("INELIGIBLE", "DISQUALIFIED", "WITHDRAWN", "LATE") else "INELIGIBLE"
            rows.append(
                {
                    "bid": bid.id,
                    "vendor": bid.vendor,
                    "package": None,
                    "reason": code,
                    "detail": REASON_TEXT.get(code, code),
                }
            )
            continue
        whole_blocked = bid.whole and len(
            {p.exclusion for p in bid.packages if p.exclusion}
        ) != len([p.exclusion for p in bid.packages if p.exclusion])
        for package in bid.packages:
            if (bid.id, package.id) in selected:
                continue
            reason = None
            detail = None
            if whole_blocked:
                reason = "INTERNAL_EXCLUSION"
            elif package.exclusion and package.exclusion in selected_labels:
                reason = "EXCLUSION_CONFLICT"
                detail = f"exclusion {package.exclusion} 已被其他已选方案占用"
            elif bid.whole and bid.id not in selected_bid_ids:
                reason = "WHOLE_BID_NOT_SELECTED"
            else:
                over_lot = None
                below_lot = None
                for lot_id, qty in package.lots.items():
                    new_volume = solution.volumes.get(lot_id, 0) + qty
                    if new_volume > tender.lot_map[lot_id].max_kwh:
                        over_lot = lot_id
                        break
                    if tender.partial and new_volume < tender.lot_map[lot_id].min_kwh:
                        below_lot = lot_id
                if over_lot:
                    reason = "LOT_CAPACITY"
                    detail = f"时段 {over_lot} 加入后超过最大电量"
                elif below_lot:
                    reason = "BELOW_MINIMUM"
                    detail = f"时段 {below_lot} 加入后仍不足最低履约量"
                else:
                    new_total = solution.total_cents + package.cents
                    group_cents = allocate_cents(tender.lot_map, package.lots, package.cents)
                    if new_total > tender.limits["total"]:
                        reason = "BUDGET_TOTAL"
                        detail = f"加入后支出 {new_total} 超过整轮预算 {tender.limits['total']}"
                    else:
                        for group, amount in sorted(group_cents.items()):
                            if (
                                solution.group_spend.get(group, 0) + amount
                                > tender.limits[group]
                            ):
                                reason = "BUDGET_GROUP"
                                detail = (
                                    f"分组 {group} 加入后超过预算上限 "
                                    f"{tender.limits[group]}"
                                )
                                break
            if reason is None:
                reason = "NOT_SELECTED"
            rows.append(
                {
                    "bid": bid.id,
                    "vendor": bid.vendor,
                    "package": package.id,
                    "reason": reason,
                    "detail": detail or REASON_TEXT[reason],
                }
            )
    return rows


def ranked_bids(
    tender: Tender,
    bids: Sequence[Bid],
    solution: Solution,
    *,
    ineligible: dict[str, str] | None = None,
) -> list[dict]:
    ineligible = ineligible or {}
    rows = []
    for bid in sorted(bids, key=lambda item: item.id):
        package_ids = solution.selected_packages.get(bid.id, ())
        package_map = bid.package_map()
        lots: dict[str, int] = {}
        cents = 0
        for pid in package_ids:
            package = package_map[pid]
            cents += package.cents
            for lot_id, qty in package.lots.items():
                lots[lot_id] = lots.get(lot_id, 0) + qty
        rows.append(
            {
                "bid": bid.id,
                "vendor": bid.vendor,
                "revision": bid.revision,
                "eligible": bid.id not in ineligible,
                "status": ineligible.get(bid.id),
                "selected_packages": list(package_ids),
                "volumes": {lot: lots.get(lot, 0) for lot in solution.lots},
                "total_kwh": sum(lots.values()),
                "cents": cents,
            }
        )
    return rows

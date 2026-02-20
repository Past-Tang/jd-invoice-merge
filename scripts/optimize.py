"""Optimal invoice merge algorithm.

Goal: same orgId, each invoice >= target amount, maximize number of invoices.
Strategy: each invoice should be as close to target as possible (less waste = more invoices).

Algorithm:
  For each orgId group:
    1. Sort amounts descending
    2. Greedily find tightest combo >= target:
       - Try 2-order combos first (least orders used = most left for other invoices)
       - Then 3, 4, ... up to max_size
       - Pick the combo with smallest sum >= target
       - Tie-break: fewer orders
    3. Remove used orders, repeat
    4. Until no more combos can reach target
"""
import json
import os
from itertools import combinations
from collections import defaultdict
from dataclasses import dataclass, field

from core.config import get_config


@dataclass
class Invoice:
    """One merged invoice."""
    org_id: int
    org_name: str
    order_ids: list[str]
    amounts: list[float]
    total: float

    @property
    def count(self):
        return len(self.order_ids)


@dataclass
class Plan:
    """Full merge plan."""
    invoices: list[Invoice] = field(default_factory=list)
    leftover: dict = field(default_factory=dict)  # orgId -> [orders]

    @property
    def total_invoices(self):
        return len(self.invoices)

    @property
    def total_amount(self):
        return sum(inv.total for inv in self.invoices)

    @property
    def total_orders_used(self):
        return sum(inv.count for inv in self.invoices)

    @property
    def avg_waste(self):
        if not self.invoices:
            return 0
        return sum(inv.total - get_config()["merge"]["target_amount"]
                   for inv in self.invoices) / len(self.invoices)


def find_best_combo(amounts_with_idx, target, max_size=10):
    """Find the combination closest to target (>= target), using fewest orders.

    Uses progressive search: try size 2 first, only go larger if needed.
    Pruning: sorted descending, early termination when partial sum exceeds best.

    Args:
        amounts_with_idx: list of (index, amount) sorted by amount descending
        target: minimum sum required
        max_size: max orders per invoice

    Returns:
        best combo as list of (index, amount), or None
    """
    best = None
    best_sum = float('inf')

    n = len(amounts_with_idx)
    amounts = [a for _, a in amounts_with_idx]

    for size in range(2, min(max_size + 1, n + 1)):
        # Pruning: if the top `size` items can't reach target, skip
        if sum(amounts[:size]) < target:
            continue
        # Pruning: if the bottom `size` items already exceed best_sum, skip
        if sum(amounts[-size:]) >= best_sum:
            continue

        found_at_this_size = False
        for combo in combinations(range(n), size):
            s = sum(amounts[j] for j in combo)
            if s >= target and s < best_sum:
                best_sum = s
                best = [amounts_with_idx[j] for j in combo]
                found_at_this_size = True

        # If we found a tight combo at this size, stop searching larger sizes
        if found_at_this_size and best_sum < target * 1.10:
            break

    return best


def optimize(orders, target=None, max_size=None):
    """Find optimal merge plan.

    Args:
        orders: list of order dicts with 'orgId', 'orderId', 'ivcAmount', 'canHk'
        target: minimum invoice amount (default from config)
        max_size: max orders per invoice (default from config)

    Returns:
        Plan object
    """
    config = get_config()
    if target is None:
        target = config["merge"]["target_amount"]
    if max_size is None:
        max_size = config["merge"]["max_orders_per_invoice"]

    # Filter canHk orders and group by orgId
    hk_orders = [o for o in orders if o.get('canHk')]
    by_org = defaultdict(list)
    for o in hk_orders:
        by_org[o['orgId']].append(o)

    plan = Plan()

    for org_id in sorted(by_org.keys(),
                         key=lambda k: -sum(float(o['ivcAmount']) for o in by_org[k])):
        pool = by_org[org_id]
        org_total = sum(float(o['ivcAmount']) for o in pool)

        if org_total < target:
            plan.leftover[org_id] = pool
            continue

        # Build indexed amount list, sorted descending
        available = [(i, float(pool[i]['ivcAmount'])) for i in range(len(pool))]
        used = set()

        while True:
            remaining = [(i, a) for i, a in available if i not in used]
            remaining_total = sum(a for _, a in remaining)

            if remaining_total < target:
                break

            remaining.sort(key=lambda x: -x[1])
            combo = find_best_combo(remaining, target, max_size)
            if combo is None:
                break

            indices = [idx for idx, _ in combo]
            inv = Invoice(
                org_id=org_id,
                org_name="",
                order_ids=[pool[idx]['orderId'] for idx in indices],
                amounts=[float(pool[idx]['ivcAmount']) for idx in indices],
                total=sum(a for _, a in combo),
            )
            plan.invoices.append(inv)

            for idx in indices:
                used.add(idx)

        leftover_orders = [pool[i] for i in range(len(pool)) if i not in used]
        if leftover_orders:
            plan.leftover[org_id] = leftover_orders

    return plan


def print_plan(plan, target=None):
    """Pretty print the merge plan."""
    if target is None:
        target = get_config()["merge"]["target_amount"]

    print(f"{'='*60}")
    print(f"MERGE PLAN: {plan.total_invoices} invoices, "
          f"{plan.total_orders_used} orders, ¥{plan.total_amount:.2f}")
    print(f"Avg waste per invoice: ¥{plan.avg_waste:.2f}")
    print(f"{'='*60}")

    for i, inv in enumerate(plan.invoices):
        waste = inv.total - target
        print(f"\nInvoice {i+1}: orgId={inv.org_id} | {inv.count} orders | "
              f"¥{inv.total:.2f} (waste ¥{waste:.2f})")
        for oid, amt in zip(inv.order_ids, inv.amounts):
            print(f"    {oid}  ¥{amt:.2f}")

    if plan.leftover:
        print(f"\n--- Leftover (cannot reach ¥{target:.0f}) ---")
        for org_id, orders in plan.leftover.items():
            total = sum(float(o['ivcAmount']) for o in orders)
            print(f"  orgId={org_id}: {len(orders)} orders, ¥{total:.2f}")


def save_plan(plan, path=None):
    """Save plan to JSON file."""
    if path is None:
        path = get_config()["paths"]["merge_plan_file"]

    plan_data = {
        "invoices": [{
            "org_id": inv.org_id,
            "order_ids": inv.order_ids,
            "amounts": inv.amounts,
            "total": inv.total,
        } for inv in plan.invoices],
        "leftover": {str(k): [{
            "orderId": o["orderId"],
            "ivcAmount": o["ivcAmount"],
        } for o in v] for k, v in plan.leftover.items()},
        "summary": {
            "total_invoices": plan.total_invoices,
            "total_orders_used": plan.total_orders_used,
            "total_amount": plan.total_amount,
            "avg_waste": plan.avg_waste,
        }
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan_data, f, ensure_ascii=False, indent=2)
    print(f"\nPlan saved to {path}")


def main():
    config = get_config()
    orders_file = config["paths"]["all_orders_file"]

    with open(orders_file, "r", encoding="utf-8") as f:
        all_orders = json.load(f)

    # Exclude already completed orders if progress file exists
    progress_file = config["paths"]["merge_progress_file"]
    done_ids = set()
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            progress = json.load(f)
        for inv in progress.get("completed", []):
            done_ids.update(inv.get("order_ids", []))

    orders = [o for o in all_orders if o['orderId'] not in done_ids]
    print(f"Total orders: {len(all_orders)}, excluding {len(done_ids)} done = {len(orders)}")

    plan = optimize(orders)
    print_plan(plan)
    save_plan(plan)


if __name__ == "__main__":
    main()

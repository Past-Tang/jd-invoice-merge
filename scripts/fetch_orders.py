"""Fetch all orders from JD invoice center.

Supports two modes:
- 换开/合开 tab: orders eligible for merge (getBatchNextOrderPage API)
- 全部 tab: all orders via InfiniteScroll + goodsCard extraction

JD's API auth goes through native bridge, so we trigger the page's own
JS methods to load data, then extract from Vue component instances.
"""
import asyncio
import json
import os
import random
import time

import websockets

from core.bridge import attach_and_enable_debug
from core.cdp import run_js, setup_port_forward, find_invoice_page, CDP_PORT
from core.config import get_config


async def fetch_hk_tab(ws, mid):
    """Fetch all orders from 换开/合开 tab via CDP JS XHR.

    This tab uses getBatchNextOrderPage.action which returns orders
    with orgId and originalOrderInfo needed for merging.

    Returns:
        List of order dicts.
    """
    # Switch to 换开/合开 tab
    await run_js(ws, r"""
        var tab = document.querySelector('.tab-title-item.change');
        if (tab) tab.click();
    """, mid)
    await asyncio.sleep(2)

    all_data = []
    page = 1

    while True:
        delay = random.uniform(1.0, 2.5)
        await asyncio.sleep(delay)

        result = await run_js(ws, f"""
            new Promise(function(resolve) {{
                var xhr = new XMLHttpRequest();
                xhr.open('GET',
                    'https://myivc.jd.com/newIvc/appFpzz/getBatchNextOrderPage.action?page={page}',
                    true);
                xhr.withCredentials = true;
                xhr.onload = function() {{ resolve(xhr.responseText); }};
                xhr.onerror = function() {{
                    resolve(JSON.stringify({{error: 'xhr_fail', status: xhr.status}}));
                }};
                xhr.send();
            }})
        """, mid, timeout=15)

        if not result:
            print(f"  Page {page}: null response")
            break

        try:
            data = json.loads(result)
        except Exception:
            print(f"  Page {page}: parse error: {result[:100]}")
            break

        if data.get("error"):
            print(f"  Page {page}: {data}")
            break

        items = data.get("data", [])
        if not items:
            print(f"  Page {page}: empty, done!")
            break

        all_data.extend(items)
        print(f"  Page {page}: +{len(items)} (total: {len(all_data)})")
        page += 1

    return all_data


async def fetch_all_tab(ws, mid):
    """Fetch all orders from 全部 tab via InfiniteScroll + goodsCard.

    This tab uses api.m.jd.com which requires native bridge auth,
    so we trigger the page's own loading mechanism.

    Returns:
        List of order dicts.
    """
    # Switch to 全部 tab
    await run_js(ws, r"""
        var tabs = document.querySelectorAll('.tab-title-item');
        for (var i = 0; i < tabs.length; i++) {
            if (tabs[i].textContent.trim() === '全部') { tabs[i].click(); break; }
        }
    """, mid)
    await asyncio.sleep(2)

    # Find InfiniteScroll + OrderList Vue components
    found = await run_js(ws, r"""
        (function() {
            var els = document.querySelectorAll('*');
            for (var i = 0; i < els.length; i++) {
                var vm = els[i].__vue__;
                if (vm && vm.$options.name === 'InfiniteScroll') {
                    var parent = vm.$parent;
                    if (parent && parent.$options.name === 'OrderList') {
                        window.__infiniteVM = vm;
                        window.__orderListVM = parent;
                        return JSON.stringify({ok: true, finished: vm.finished});
                    }
                }
            }
            return JSON.stringify({ok: false});
        })()
    """, mid)
    print(f"  InfiniteScroll: {found}")

    f = json.loads(found) if found else {}
    if not f.get("ok"):
        print("  ERROR: InfiniteScroll not found")
        return []

    # Trigger loading via $emit('load')
    print("  Loading all pages...")
    last_count = 0
    stall = 0

    for rnd in range(1, 200):
        delay = random.uniform(1.0, 2.0)
        await asyncio.sleep(delay)

        result = await run_js(ws, r"""
            (async function() {
                var sv = window.__infiniteVM;
                sv.isLoading = false;
                sv.finished = false;
                sv.$emit('load');
                await new Promise(function(r) { setTimeout(r, 2000); });
                var count = 0;
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    if (els[i].__vue__ && els[i].__vue__.$options.name === 'goodsCard') count++;
                }
                return JSON.stringify({cards: count, finished: sv.finished});
            })()
        """, mid, timeout=15)

        r = json.loads(result) if result else {}
        cur = r.get("cards", 0)
        fin = r.get("finished", False)

        if cur > last_count:
            print(f"  Round {rnd}: {cur} orders (+{cur - last_count})")
            last_count = cur
            stall = 0
        else:
            stall += 1

        if fin or stall >= 3:
            print(f"  Done! finished={fin}, total={cur}")
            break

    # Extract data from goodsCard instances
    print("  Extracting order data...")
    extracted = await run_js(ws, r"""
        (function() {
            var orders = [];
            var seen = {};
            var els = document.querySelectorAll('*');
            for (var i = 0; i < els.length; i++) {
                var vm = els[i].__vue__;
                if (!vm || vm.$options.name !== 'goodsCard') continue;
                var item = vm.item || vm.order || vm.$props.item || vm.$props.order;
                if (!item) {
                    var props = vm.$props || {};
                    for (var pk in props) {
                        if (props[pk] && props[pk].orderId) { item = props[pk]; break; }
                    }
                }
                if (item && item.orderId && !seen[item.orderId]) {
                    seen[item.orderId] = true;
                    orders.push(item);
                }
            }
            return JSON.stringify({count: orders.length, orders: orders});
        })()
    """, mid, timeout=60)

    if extracted:
        ext = json.loads(extracted)
        return ext.get("orders", [])
    return []


async def main_async():
    """Main entry: connect and fetch orders."""
    config = get_config()
    paths = config["paths"]
    os.makedirs(paths["data_dir"], exist_ok=True)

    from core.connection import JDConnection
    async with JDConnection() as conn:
        mid = [0]
        ws = conn.ws

        # Navigate to order list
        url = await run_js(ws, "location.href", mid)
        print(f"Current URL: {url}")
        if 'orderList' not in str(url):
            await run_js(ws, "location.href='https://invoice-m.jd.com/#/orderList?sourceId=0'", mid)
            await asyncio.sleep(3)

        # Fetch 换开 tab orders (with orgId for merging)
        print("\n--- Fetching 换开/合开 tab ---")
        hk_orders = await fetch_hk_tab(ws, mid)
        print(f"  Total: {len(hk_orders)} orders")

        if hk_orders:
            out = paths["all_orders_file"]
            with open(out, "w", encoding="utf-8") as f:
                json.dump(hk_orders, f, ensure_ascii=False, indent=2)
            print(f"  Saved to {out}")

        # Fetch 全部 tab orders
        print("\n--- Fetching 全部 tab ---")
        all_orders = await fetch_all_tab(ws, mid)
        print(f"  Total: {len(all_orders)} orders")

        if all_orders:
            out = paths["all_tab_orders_file"]
            with open(out, "w", encoding="utf-8") as f:
                json.dump(all_orders, f, ensure_ascii=False, indent=2)
            print(f"  Saved to {out}")

        # Summary
        print(f"\n{'='*50}")
        print(f"换开 tab: {len(hk_orders)} orders")
        print(f"全部 tab: {len(all_orders)} orders")
        if hk_orders:
            can_hk = sum(1 for o in hk_orders if o.get("canHk"))
            print(f"canHk=True: {can_hk}/{len(hk_orders)}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

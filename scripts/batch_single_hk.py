"""Batch single invoice 换开 for orders not in the merge tab.

For orders that appear in 全部 tab but not in 换开/合开 tab,
uses goodsCard.jumpToHk() to navigate to individual 换开 form page.
Primarily targets orders >= target amount that don't need merging.
"""
import asyncio
import json
import os
import random
import subprocess
import time

import websockets

from core.cdp import run_js, send_cdp, drain_messages
from core.config import get_config
from core.connection import JDConnection


async def find_goods_card(ws, mid, max_attempts=10):
    """Wait for a goodsCard Vue instance to appear in DOM.

    Returns:
        True if found (stored as window.__anyCard), False otherwise.
    """
    for attempt in range(max_attempts):
        await asyncio.sleep(2)
        subprocess.run(
            ["adb", "shell", "input", "swipe", "540", "1800", "540", "600", "300"],
            capture_output=True
        )
        await asyncio.sleep(1)
        found = await run_js(ws, r"""
            (function() {
                var els = document.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    var vm = els[i].__vue__;
                    if (vm && vm.$options.name === 'goodsCard') {
                        window.__anyCard = vm;
                        return true;
                    }
                }
                return false;
            })()
        """, mid)
        if found:
            print(f"  Found goodsCard after {attempt+1} attempts")
            return True
    return False


async def navigate_to_all_tab(ws, mid):
    """Navigate to 全部 tab on order list page."""
    url = await run_js(ws, "location.href", mid)
    if 'orderList' not in str(url):
        await run_js(ws, "location.href='https://invoice-m.jd.com/#/orderList?sourceId=0'", mid)
        await asyncio.sleep(3)

    await run_js(ws, r"""
        var tabs = document.querySelectorAll('.tab-title-item');
        for (var i = 0; i < tabs.length; i++) {
            if (tabs[i].textContent.trim() === '全部') { tabs[i].click(); break; }
        }
    """, mid)
    await asyncio.sleep(2)


async def work(ws_url):
    """Main work loop for single 换开."""
    config = get_config()
    ivc_cfg = config["invoice"]
    paths = config["paths"]
    mid = [0]

    async with websockets.connect(ws_url, max_size=50_000_000) as ws:

        # 1. Navigate to 全部 tab
        await navigate_to_all_tab(ws, mid)

        # 2. Load target orders from saved data
        print("--- Loading target orders ---")
        with open(paths["all_tab_orders_file"], "r", encoding="utf-8") as f:
            all_tab = json.load(f)
        with open(paths["all_orders_file"], "r", encoding="utf-8") as f:
            hk_orders = json.load(f)

        hk_ids = set(o["orderId"] for o in hk_orders)
        target_amount = config["merge"]["target_amount"]

        targets = [o for o in all_tab
                   if o["orderId"] not in hk_ids
                   and o.get("canHk")
                   and str(o.get("ivcStatus")) == "1"
                   and float(o.get("actualInvoiceAmount") or o.get("ivcAmount") or 0) >= target_amount]
        targets.sort(key=lambda o: -float(o.get("actualInvoiceAmount") or o.get("ivcAmount") or 0))

        print(f"  {len(targets)} orders >= ¥{target_amount} to 换开:")
        total_amt = 0
        for o in targets:
            amt = float(o.get("actualInvoiceAmount") or o.get("ivcAmount") or 0)
            total_amt += amt
            prod = o["products"][0]["name"][:35] if o.get("products") else ""
            print(f"    {o['orderId']} ¥{amt:.2f} | {o.get('ivcTitle','')} | {prod}")
        print(f"  Total: ¥{total_amt:.2f}")

        if not targets:
            print("No eligible orders found!")
            return

        # 3. Wait for goodsCard
        print("\n--- Waiting for goodsCard ---")
        if not await find_goods_card(ws, mid):
            print("  ERROR: No goodsCard found!")
            return

        # 4. Process each order
        print(f"\n--- Processing {len(targets)} orders ---")
        success_count = 0
        fail_count = 0

        for idx, order in enumerate(targets):
            oid = order["orderId"]
            amt = float(order.get("actualInvoiceAmount") or order.get("ivcAmount") or 0)
            print(f"\n{'='*50}")
            print(f"Order {idx+1}/{len(targets)}: {oid} ¥{amt:.2f}")
            print(f"{'='*50}")

            # Ensure on order list page
            cur_url = await run_js(ws, "location.href", mid)
            if 'orderList' not in str(cur_url):
                await navigate_to_all_tab(ws, mid)
                if not await find_goods_card(ws, mid, max_attempts=5):
                    print("  ERROR: Cannot find goodsCard after navigation")
                    fail_count += 1
                    continue

            # Step A: Call jumpToHk
            order_json = json.dumps({
                "orderId": order["orderId"],
                "ivcType": order.get("ivcType", "23"),
                "ivcTitle": order.get("ivcTitle", ""),
                "passKey": order.get("passKey", ""),
                "tagStr": order.get("tagStr", ""),
            }, ensure_ascii=False)

            result = await run_js(ws, f"""
                (async function() {{
                    var card = window.__anyCard;
                    if (!card) return 'no_card';
                    var orderData = {order_json};
                    try {{
                        card.jumpToHk(orderData);
                        return 'called';
                    }} catch(e) {{
                        return 'error: ' + e.message;
                    }}
                }})()
            """, mid)
            print(f"  jumpToHk: {result}")

            if result != 'called':
                print(f"  FAILED: {result}")
                fail_count += 1
                continue

            # Step B: Wait for form page
            await asyncio.sleep(4)
            form_url = await run_js(ws, "location.href", mid)
            print(f"  Form URL: {str(form_url)[:100]}")

            if 'HkAppIvcTitle' not in str(form_url):
                toast = await run_js(ws, "document.body.innerText.substring(0, 200)", mid)
                print(f"  NOT on form page. Body: {toast}")
                fail_count += 1
                await asyncio.sleep(2)
                continue

            # Step C: Find Vue VM on form page
            await asyncio.sleep(2)
            vm_found = await run_js(ws, r"""
                (function() {
                    var els = document.querySelectorAll('*');
                    for (var i = 0; i < els.length; i++) {
                        var vm = els[i].__vue__;
                        if (vm && vm.$options && vm.$options.methods &&
                            (vm.$options.methods.commitHkfpReq ||
                             vm.$options.methods.commitBatchHkfpReq ||
                             vm.$options.methods.submitHkfp)) {
                            window.__formVM = vm;
                            return JSON.stringify({
                                found: true,
                                methods: Object.keys(vm.$options.methods).filter(function(m) {
                                    return m.indexOf('commit') >= 0 || m.indexOf('submit') >= 0;
                                })
                            });
                        }
                    }
                    return JSON.stringify({found: false});
                })()
            """, mid)
            print(f"  VM: {vm_found}")

            vm_info = json.loads(vm_found) if vm_found else {}
            if not vm_info.get("found"):
                print(f"  FAILED: Form VM not found")
                fail_count += 1
                continue

            # Step D: Set form data and submit
            methods = vm_info.get("methods", [])
            if "commitHkfpReq" in methods:
                submit_method = "commitHkfpReq"
            elif "submitHkfp" in methods:
                submit_method = "submitHkfp"
            else:
                submit_method = "commitBatchHkfpReq"

            ivc_title = ivc_cfg["ivc_title"]
            submit_result = await run_js(ws, f"""
                (function() {{
                    var vm = window.__formVM;
                    vm.formData.ivcTitleType = {ivc_cfg['ivc_title_type']};
                    vm.formData.ivcType = {ivc_cfg['ivc_type']};
                    vm.formData.ivcContent = {ivc_cfg['ivc_content']};
                    vm.formData.changeReason = '{ivc_cfg['change_reason']}';
                    if (vm.formData.self) vm.formData.self.ivcTitle = '{ivc_title}';
                    try {{
                        vm.{submit_method}();
                        return 'submitted';
                    }} catch(e) {{
                        return 'error: ' + e.message;
                    }}
                }})()
            """, mid)
            print(f"  Submit ({submit_method}): {submit_result}")

            if submit_result != 'submitted':
                fail_count += 1
                continue

            # Step E: Wait for response
            await asyncio.sleep(5)
            page_text = await run_js(ws, "document.body.innerText.substring(0, 300)", mid)
            if '已申请' in str(page_text) or '成功' in str(page_text):
                print(f"  SUCCESS!")
                success_count += 1
            else:
                print(f"  Result unclear: {str(page_text)[:150]}")
                success_count += 1

            # Random delay
            if idx < len(targets) - 1:
                delay = random.uniform(4, 8)
                print(f"  Waiting {delay:.1f}s...")
                await asyncio.sleep(delay)

        print(f"\n{'='*50}")
        print(f"DONE: {success_count} success, {fail_count} failed out of {len(targets)}")
        print(f"{'='*50}")


def main():
    config = get_config()
    os.makedirs(config["paths"]["data_dir"], exist_ok=True)

    from core.bridge import attach_and_enable_debug
    from core.cdp import setup_port_forward, find_invoice_page
    import time

    device, session, script, pid = attach_and_enable_debug()
    print(f"JD PID: {pid}")
    time.sleep(5)

    setup_port_forward(pid)

    try:
        ws_url = find_invoice_page()
        if not ws_url:
            print("Invoice page not found!")
            return
        asyncio.run(work(ws_url))
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        script.unload()
        session.detach()


if __name__ == "__main__":
    main()
